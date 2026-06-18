"""Init-flow extensions for issue-agent-runner.

This module hosts the engine-layer helper that copies bundled Skills
(``prd``, ``code-reviewer``) from the installed wheel into a freshly
initialised repository's ``.claude/skills/`` tree. The skill copy is part
of the ``iar init`` command so that new users get a working set of
Claude/Codex skills out of the box without having to fetch them
separately.

The implementation intentionally lives next to ``repository_local`` so the
init command in ``backend.api.cli`` can keep a single engine-layer call
site. It uses ``importlib.resources`` to locate the bundled skill tree
inside the wheel — matching the pattern already established for
``workflow_install.py``.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Iterable

from backend.infrastructure.logging.logger import Logger


SKILL_PACKAGE_NAME = "backend.engines.agent_runner.skills"
"""Package that holds the bundled skill directories; see ``pyproject.toml``."""

# Skill directories shipped in the wheel. Order is preserved in user-facing
# output so the dry-run plan reads top-to-bottom in the order skills are
# copied.
DEFAULT_BUNDLED_SKILL_NAMES: tuple[str, ...] = ("prd", "code-reviewer")

# Destination layout inside the initialised repository.
TARGET_SKILLS_DIRNAME = ".claude"
TARGET_SKILLS_SUBDIR = "skills"


logger = Logger()


@dataclass(frozen=True)
class BundledSkillCopyOptions:
    """Options controlling the bundled-skill copy step.

    Attributes:
        repo_root_path: Initialised repository root. Skill files land under
            ``<repo>/.claude/skills/<name>/``.
        force: Overwrite existing skill files whose SHA256 differs from the
            bundled copy. When ``False``, divergent files are left in place
            and only a warning is logged.
        dry_run: If ``True``, return plans without writing anything to disk.
        skip: If ``True``, skip the whole step (used by ``--skip-skills``).
        skill_names: Optional override of the skills to copy; defaults to
            :data:`DEFAULT_BUNDLED_SKILL_NAMES`. Tests use this to limit the
            surface area.
    """

    repo_root_path: Path
    force: bool = False
    dry_run: bool = False
    skip: bool = False
    skill_names: tuple[str, ...] = DEFAULT_BUNDLED_SKILL_NAMES


class UnknownBundledSkillError(ValueError):
    """Raised when a requested skill name has no bundled directory."""


class BundledSkillConflictError(RuntimeError):
    """Raised when a divergent skill directory exists and ``force`` is unset.

    This exception is intentionally fatal — unlike :class:`InitStepOutcome`
    warnings — so callers can decide whether to fail the whole init
    (currently we do not, but the type is left in for symmetry with
    ``workflow_install``).
    """

    def __init__(self, skill_name: str, target_path: Path) -> None:
        self.skill_name = skill_name
        self.target_path = target_path
        super().__init__(
            f"Refusing to overwrite existing skill '{skill_name}' at "
            f"{target_path} (use --force to override)"
        )


@dataclass(frozen=True)
class SkillFilePlan:
    """Per-file copy plan emitted by :func:`plan_skill_copy`."""

    skill_name: str
    relative_path: str  # POSIX, relative to the skill root.
    source_bytes: int
    target_path: Path
    exists_on_disk: bool


@dataclass(frozen=True)
class BundledSkillCopyResult:
    """Aggregate result of :func:`copy_bundled_skills`."""

    repo_root_path: Path
    target_skills_root: Path
    dry_run: bool
    skipped: bool
    copied_skills: tuple[str, ...] = field(default_factory=tuple)
    skipped_identical_skills: tuple[str, ...] = field(default_factory=tuple)
    overwritten_skills: tuple[str, ...] = field(default_factory=tuple)
    diverged_skills: tuple[str, ...] = field(default_factory=tuple)
    missing_skills: tuple[str, ...] = field(default_factory=tuple)
    file_plans: tuple[SkillFilePlan, ...] = field(default_factory=tuple)


def resolve_target_skills_root(repo_root_path: Path) -> Path:
    """Return the absolute path of the ``.claude/skills`` directory."""
    return (repo_root_path / TARGET_SKILLS_DIRNAME / TARGET_SKILLS_SUBDIR).resolve()


def list_bundled_skill_names() -> tuple[str, ...]:
    """Return the bundled skill names that actually exist in the wheel.

    Hidden / partial wheels (e.g. editable installs where the skills
    directory was removed) are reported as an empty tuple. Callers should
    treat an empty result as "nothing to copy" rather than an error.
    """
    try:
        skills_root = files(SKILL_PACKAGE_NAME)
    except (ModuleNotFoundError, FileNotFoundError):
        return ()
    names: list[str] = []
    for entry in sorted(skills_root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        if entry.name.startswith((".", "__")):
            continue
        names.append(entry.name)
    return tuple(names)


def plan_skill_copy(
    *,
    repo_root_path: Path,
    skill_name: str,
) -> tuple[tuple[SkillFilePlan, ...], bytes]:
    """Build per-file copy plans for a single bundled skill.

    Args:
        repo_root_path: Initialised repository root.
        skill_name: Name of the bundled skill (subdirectory under
            :data:`SKILL_PACKAGE_NAME`).

    Returns:
        A tuple of (file plans, aggregated SHA256 of bundled files).
        The aggregated hash is computed in sorted relative-path order so
        callers can compare a directory against the wheel copy byte-by-byte.

    Raises:
        UnknownBundledSkillError: When ``skill_name`` does not match a
            bundled directory.
    """
    skill_root = _resolve_skill_root(skill_name)
    target_skill_root = (
        repo_root_path / TARGET_SKILLS_DIRNAME / TARGET_SKILLS_SUBDIR / skill_name
    )
    plans: list[SkillFilePlan] = []
    aggregated_hash = hashlib.sha256()
    for relative_posix_path, traversable in _iter_skill_files(skill_root):
        file_bytes = traversable.read_bytes()
        aggregated_hash.update(relative_posix_path.encode("utf-8"))
        aggregated_hash.update(b"\x00")
        aggregated_hash.update(file_bytes)
        target_path = target_skill_root / relative_posix_path
        plans.append(
            SkillFilePlan(
                skill_name=skill_name,
                relative_path=relative_posix_path,
                source_bytes=len(file_bytes),
                target_path=target_path,
                exists_on_disk=target_path.exists(),
            )
        )
    return tuple(plans), aggregated_hash.digest()


def compute_target_skill_directory_hash(target_skill_root: Path) -> bytes | None:
    """Compute the aggregated SHA256 for an on-disk skill directory.

    Returns ``None`` when the directory does not exist. The hash uses the
    same ordering rule as :func:`plan_skill_copy` (sorted relative POSIX
    paths, null-byte separator) so a direct comparison is meaningful.
    """
    if not target_skill_root.is_dir():
        return None
    aggregated_hash = hashlib.sha256()
    for relative_posix_path in sorted(
        p.relative_to(target_skill_root).as_posix()
        for p in target_skill_root.rglob("*")
        if p.is_file()
    ):
        file_bytes = (target_skill_root / relative_posix_path).read_bytes()
        aggregated_hash.update(relative_posix_path.encode("utf-8"))
        aggregated_hash.update(b"\x00")
        aggregated_hash.update(file_bytes)
    return aggregated_hash.digest()


def copy_bundled_skills(
    options: BundledSkillCopyOptions,
) -> BundledSkillCopyResult:
    """Copy bundled skills into the initialised repository.

    The copy respects the following SHA256-driven policy:

    * Target directory missing → copy every file.
    * Target directory exists and aggregated hash matches → leave untouched
      (``exists-identical``).
    * Target directory exists, hashes differ, ``force`` is set → overwrite
      (``overwritten``).
    * Target directory exists, hashes differ, ``force`` is unset → leave
      existing files in place and record a ``diverged`` warning; the init
      flow still succeeds.

    Args:
        options: Step options (repo root, force, dry-run, skip, names).

    Returns:
        Aggregate result describing which skills were copied, skipped,
        overwritten, diverged or missing, plus per-file plans for
        diagnostic / dry-run output.

    Raises:
        UnknownBundledSkillError: When an explicit ``skill_names`` entry
            does not match a bundled directory.
    """
    target_skills_root = resolve_target_skills_root(options.repo_root_path)
    if options.skip:
        return BundledSkillCopyResult(
            repo_root_path=options.repo_root_path,
            target_skills_root=target_skills_root,
            dry_run=options.dry_run,
            skipped=True,
        )

    bundled_names = list_bundled_skill_names()
    missing_skills: list[str] = []
    requested_names = list(options.skill_names)
    for skill_name in requested_names:
        if skill_name not in bundled_names:
            raise UnknownBundledSkillError(
                f"Bundled skill '{skill_name}' not found in " f"{SKILL_PACKAGE_NAME}"
            )

    effective_names = tuple(requested_names)

    copied_skills: list[str] = []
    skipped_identical_skills: list[str] = []
    overwritten_skills: list[str] = []
    diverged_skills: list[str] = []
    all_plans: list[SkillFilePlan] = []

    for skill_name in effective_names:
        plans, bundled_hash = plan_skill_copy(
            repo_root_path=options.repo_root_path,
            skill_name=skill_name,
        )
        target_skill_root = (
            options.repo_root_path
            / TARGET_SKILLS_DIRNAME
            / TARGET_SKILLS_SUBDIR
            / skill_name
        )
        existing_hash = compute_target_skill_directory_hash(target_skill_root)
        all_plans.extend(plans)

        if existing_hash is None:
            if not options.dry_run:
                _apply_skill_writes(plans, options.repo_root_path, skill_name)
            copied_skills.append(skill_name)
            continue

        if existing_hash == bundled_hash:
            skipped_identical_skills.append(skill_name)
            continue

        if options.force:
            if not options.dry_run:
                _apply_skill_writes(plans, options.repo_root_path, skill_name)
            overwritten_skills.append(skill_name)
            continue

        diverged_skills.append(skill_name)
        logger.warning(
            "Skill '%s' at %s differs from bundled copy; "
            "keeping local version (use --force to overwrite).",
            skill_name,
            target_skill_root,
        )

    return BundledSkillCopyResult(
        repo_root_path=options.repo_root_path,
        target_skills_root=target_skills_root,
        dry_run=options.dry_run,
        skipped=False,
        copied_skills=tuple(copied_skills),
        skipped_identical_skills=tuple(skipped_identical_skills),
        overwritten_skills=tuple(overwritten_skills),
        diverged_skills=tuple(diverged_skills),
        missing_skills=tuple(missing_skills),
        file_plans=tuple(all_plans),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_skill_root(skill_name: str):
    """Return the importlib Traversable for a bundled skill directory."""
    skill_root = files(SKILL_PACKAGE_NAME).joinpath(skill_name)
    if not skill_root.is_dir():
        raise UnknownBundledSkillError(
            f"Bundled skill '{skill_name}' not found in {SKILL_PACKAGE_NAME}"
        )
    return skill_root


def _iter_skill_files(skill_root) -> Iterable[tuple[str, object]]:
    """Yield ``(relative_posix_path, traversable)`` for every file."""
    for entry in sorted(skill_root.rglob("*"), key=lambda p: str(p)):
        if not entry.is_file():
            continue
        relative_path = entry.relative_to(skill_root).as_posix()
        yield relative_path, entry


def _apply_skill_writes(
    plans: Iterable[SkillFilePlan],
    repo_root_path: Path,
    skill_name: str,
) -> None:
    """Materialise the planned file writes for a single skill.

    Existing target directories are wiped before re-population so a
    divergent (force) overwrites cleanly removes files that the bundled
    copy no longer ships.
    """
    plans_list = list(plans)
    if not plans_list:
        return
    target_skill_root = (
        repo_root_path / TARGET_SKILLS_DIRNAME / TARGET_SKILLS_SUBDIR / skill_name
    )
    if target_skill_root.exists():
        shutil.rmtree(target_skill_root)
    target_skill_root.mkdir(parents=True, exist_ok=True)

    for plan in plans_list:
        skill_root = _resolve_skill_root(skill_name)
        traversable = skill_root.joinpath(plan.relative_path)
        file_bytes = traversable.read_bytes()
        plan.target_path.parent.mkdir(parents=True, exist_ok=True)
        plan.target_path.write_bytes(file_bytes)


# ---------------------------------------------------------------------------
# User-facing formatting helpers (kept here so they live next to the data
# they describe; the CLI layer only re-exports them through Rich).
# ---------------------------------------------------------------------------


def format_skill_copy_plan(repo_root_path: Path) -> str:
    """Return a human-readable ``iar init --dry-run`` plan line."""
    bundled_names = list_bundled_skill_names()
    target_root = resolve_target_skills_root(repo_root_path)
    if not bundled_names:
        return f"will copy skills: (none bundled in this wheel) -> {target_root}"
    joined = ", ".join(bundled_names)
    return f"will copy skills: {joined} -> {target_root}"


def format_skill_copy_summary(skill_result: BundledSkillCopyResult) -> list[str]:
    """Return a list of human-readable lines describing the copy result."""
    if skill_result.skipped:
        return []
    lines: list[str] = []
    target_root = skill_result.target_skills_root
    for skill_name in skill_result.copied_skills:
        lines.append(f"Copied skill: {skill_name} -> {target_root / skill_name}")
    for skill_name in skill_result.skipped_identical_skills:
        lines.append(f"Skill already up to date: {skill_name}")
    for skill_name in skill_result.overwritten_skills:
        lines.append(f"Overwrote skill: {skill_name} -> {target_root / skill_name}")
    for skill_name in skill_result.diverged_skills:
        lines.append(
            f"Skill diverged; kept local copy: {skill_name} "
            "(use --force to overwrite)"
        )
    return lines


__all__ = [
    "BundledSkillConflictError",
    "BundledSkillCopyOptions",
    "BundledSkillCopyResult",
    "DEFAULT_BUNDLED_SKILL_NAMES",
    "SKILL_PACKAGE_NAME",
    "SkillFilePlan",
    "TARGET_SKILLS_DIRNAME",
    "TARGET_SKILLS_SUBDIR",
    "UnknownBundledSkillError",
    "compute_target_skill_directory_hash",
    "copy_bundled_skills",
    "format_skill_copy_plan",
    "format_skill_copy_summary",
    "list_bundled_skill_names",
    "plan_skill_copy",
    "resolve_target_skills_root",
]
