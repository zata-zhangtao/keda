#!/usr/bin/env python3
"""Sync selected skills from a template repository into keda's bundled skills tree.

Default behaviour is a dry-run preview of the bundled skills ``prd`` and
``code-reviewer``. Pass --apply to write files, or use --skills to select other
skill directories.

Example:
    uv run scripts/sync_skills_from_template.py
    uv run scripts/sync_skills_from_template.py --apply
    uv run scripts/sync_skills_from_template.py --skills prd code-reviewer --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


DEFAULT_SOURCE = Path.home() / "code" / "zata_code_template" / "skills"
DEFAULT_TARGET = (
    Path(__file__).resolve().parents[1] / "src" / "backend" / "engines" / "agent_runner" / "skills"
)
DEFAULT_SKILL_NAMES = ("prd", "code-reviewer")


def _is_ignored_path(name: str) -> bool:
    """Return True for path names that should never be copied."""
    if name.startswith("."):
        return True
    if name == "__pycache__":
        return True
    if name.endswith(".pyc"):
        return True
    if name == ".DS_Store":
        return True
    return False


def _iter_sync_files(source_dir: Path) -> list[tuple[Path, Path]]:
    """Return (source_file, relative_path) pairs for files to sync."""
    pairs: list[tuple[Path, Path]] = []
    for source_file in sorted(source_dir.rglob("*")):
        if not source_file.is_file():
            continue
        if any(_is_ignored_path(part) for part in source_file.relative_to(source_dir).parts):
            continue
        pairs.append((source_file, source_file.relative_to(source_dir)))
    return pairs


def _skill_dirs(source_dir: Path, skill_names: tuple[str, ...]) -> list[Path]:
    """Return top-level skill directories from the source, filtered by name."""
    if not source_dir.is_dir():
        return []
    allowed = set(skill_names)
    return sorted(
        entry
        for entry in source_dir.iterdir()
        if entry.is_dir() and not _is_ignored_path(entry.name) and entry.name in allowed
    )


def _sync_skills(*, source: Path, target: Path, skill_names: tuple[str, ...], apply: bool) -> int:
    """Preview or perform the skill sync.

    Returns:
        0 when at least one skill was found and the operation completed (or
        would complete) without errors; 1 if no skills were found or an error
        occurred.
    """
    if not source.is_dir():
        print(f"[red]Source directory does not exist:[/] {source}", file=sys.stderr)
        return 1

    target.mkdir(parents=True, exist_ok=True)

    skill_dirs = _skill_dirs(source, skill_names)
    if not skill_dirs:
        print(
            f"[yellow]No matching skills found in {source} "
            f"(requested: {', '.join(skill_names)})[/]"
        )
        return 1

    action_label = "Would sync" if not apply else "Syncing"
    print(
        f"{action_label} {len(skill_dirs)} skill(s) "
        f"({', '.join(skill_dir.name for skill_dir in skill_dirs)}) "
        f"from {source} -> {target}"
    )

    total_copied = 0
    total_overwritten = 0
    total_unchanged = 0
    total_removed = 0

    expected_target_skills = {skill_dir.name for skill_dir in skill_dirs}

    # Remove target skills that no longer exist in source when applying.
    for existing_target in sorted(target.iterdir()):
        if not existing_target.is_dir():
            continue
        if _is_ignored_path(existing_target.name):
            continue
        if existing_target.name not in expected_target_skills:
            print(f"  [red]remove[/] {existing_target.name}")
            total_removed += 1
            if apply:
                shutil.rmtree(existing_target)

    for skill_dir in skill_dirs:
        skill_name = skill_dir.name
        target_skill_dir = target / skill_name
        print(f"\n  skill: {skill_name}")

        for source_file, relative_path in _iter_sync_files(skill_dir):
            target_file = target_skill_dir / relative_path
            target_file.parent.mkdir(parents=True, exist_ok=True)

            if target_file.exists():
                if target_file.read_bytes() == source_file.read_bytes():
                    total_unchanged += 1
                    continue
                status = "[yellow]overwrite[/]"
                total_overwritten += 1
            else:
                status = "[green]copy[/]"
                total_copied += 1

            print(f"    {status} {relative_path.as_posix()}")
            if apply:
                shutil.copy2(source_file, target_file)

        # Clean up target files that are not present in source for this skill.
        if target_skill_dir.exists():
            for target_file in sorted(target_skill_dir.rglob("*")):
                if not target_file.is_file():
                    continue
                relative_path = target_file.relative_to(target_skill_dir)
                source_file = skill_dir / relative_path
                if not source_file.exists() or any(
                    _is_ignored_path(part) for part in relative_path.parts
                ):
                    print(f"    [red]delete[/] {relative_path.as_posix()}")
                    total_removed += 1
                    if apply:
                        target_file.unlink()

    print("\nSummary:")
    print(f"  copied:     {total_copied}")
    print(f"  overwritten:{total_overwritten}")
    print(f"  unchanged:  {total_unchanged}")
    print(f"  removed:    {total_removed}")

    if not apply:
        print("\nDry-run complete. Pass --apply to write changes.")

    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the sync."""
    parser = argparse.ArgumentParser(
        description="Sync skills from a template repository into keda's bundled skills tree."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="Source skills directory (default: ~/code/zata_code_template/skills).",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help="Target bundled skills directory in keda.",
    )
    parser.add_argument(
        "--skills",
        nargs="+",
        default=list(DEFAULT_SKILL_NAMES),
        help="Skill names to sync (default: prd code-reviewer).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually copy files. Without this flag the script only previews changes.",
    )
    args = parser.parse_args(argv)

    return _sync_skills(
        source=args.source,
        target=args.target,
        skill_names=tuple(args.skills),
        apply=args.apply,
    )


if __name__ == "__main__":
    raise SystemExit(main())
