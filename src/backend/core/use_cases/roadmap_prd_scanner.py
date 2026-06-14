"""Scan PRD Markdown files and build roadmap model objects.

This module treats PRD files as the single source of truth: it reads
``tasks/pending/`` and ``tasks/archive/`` and reuses existing helpers for
title extraction, issue URL parsing, acceptance checklist progress, and
``Delivery Dependencies`` parsing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from backend.core.shared.models.agent_runner import DeliveryDependencyDeclaration
from backend.core.shared.models.roadmap import (
    RoadmapDependency,
    RoadmapDependencyKind,
    RoadmapPrd,
    RoadmapPrdState,
)
from backend.core.shared.prd_checklist import parse_prd_checklist
from backend.core.use_cases.agent_runner_dependencies import (
    parse_delivery_dependencies,
)
from backend.core.use_cases.create_issue_from_prd import (
    ISSUE_LINK_LINE_RE,
    extract_title,
    parse_issue_number,
)

_logger = logging.getLogger(__name__)

#: PRD filenames usually start with a priority token such as ``P1-FEAT-...``.
_PRIORITY_RE = re.compile(r"^(P\d+)-")

#: Default directories to scan, relative to the repository root.
_DEFAULT_PRD_DIRS = ("tasks/pending", "tasks/archive")


def _resolve_prd_directories(repo_path: Path, dirs: Sequence[str] | None) -> list[Path]:
    """Resolve and filter the PRD directories to scan.

    Args:
        repo_path: Repository root.
        dirs: Optional explicit directory names; defaults to pending/archive.

    Returns:
        Existing PRD directories in deterministic order.
    """
    targets = dirs if dirs is not None else _DEFAULT_PRD_DIRS
    existing: list[Path] = []
    for target in targets:
        target_path = repo_path / target
        if target_path.is_dir():
            existing.append(target_path)
        else:
            _logger.debug("PRD directory not found: %s", target_path)
    return existing


def _extract_priority(filename: str) -> str:
    """Return the P0/P1/P2/P3 token from a PRD filename, or empty string."""
    match = _PRIORITY_RE.match(filename)
    return match.group(1) if match else ""


def _extract_issue_url(prd_text: str) -> str | None:
    """Return the first real GitHub Issue URL found in PRD metadata.

    Placeholder values such as ``(to be created)`` are ignored.
    """
    for line in prd_text.splitlines():
        if ISSUE_LINK_LINE_RE.match(line):
            return line.split(":", 1)[1].strip()
    return None


def _parse_acceptance_progress(prd_text: str) -> tuple[int, int]:
    """Return (checked_count, total_count) for the acceptance checklist.

    The delivery gate uses :func:`parse_prd_checklist` semantics; this helper
    additionally reports how many boxes are already checked so the UI can show
    a progress bar.
    """
    result = parse_prd_checklist(prd_text)
    if not result.section_found:
        return 0, 0
    total = len(result.unchecked_items)
    checked = sum(1 for _, text in result.unchecked_items if text.startswith("[x]"))
    # unchecked_items only contains unchecked items in the current helper, so
    # we re-scan the raw text for a more useful progress metric.
    checked = 0
    total = 0
    in_acceptance = False
    for line in prd_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_acceptance = bool(re.search(r"acceptance|验收", stripped, re.IGNORECASE))
            continue
        if in_acceptance and re.match(r"^- \[[ xX]\]", stripped):
            total += 1
            if re.match(r"^- \[[xX]\]", stripped):
                checked += 1
    return checked, total


def _build_dependencies(
    prd_path: str,
    delivery_decl: DeliveryDependencyDeclaration,
    prd_path_to_issue_number: dict[str, int | None],
) -> tuple[RoadmapDependency, ...]:
    """Convert a ``DeliveryDependencyDeclaration`` into roadmap dependency edges.

    Args:
        prd_path: Source PRD path.
        delivery_decl: Parsed delivery dependency declaration.
        prd_path_to_issue_number: Map of PRD relative paths to issue numbers.

    Returns:
        Frozen dependency edges for the PRD.
    """
    dependencies: list[RoadmapDependency] = []
    for issue_number in delivery_decl.depends_on_issues:
        dependencies.append(
            RoadmapDependency(
                from_path=prd_path,
                to_path=f"#{issue_number}",
                kind=RoadmapDependencyKind.ISSUE,
                detail=f"Issue #{issue_number}",
            )
        )
    for group_name in delivery_decl.depends_on_groups:
        dependencies.append(
            RoadmapDependency(
                from_path=prd_path,
                to_path=f"group:{group_name}",
                kind=RoadmapDependencyKind.GROUP,
                detail=f"Group {group_name}",
            )
        )
    for prd_ref in delivery_decl.depends_on_prds:
        resolved_path = _resolve_prd_ref(prd_ref, prd_path_to_issue_number)
        if resolved_path is None:
            dependencies.append(
                RoadmapDependency(
                    from_path=prd_path,
                    to_path=prd_ref,
                    kind=RoadmapDependencyKind.UNRESOLVED,
                    detail=f"无法解析 PRD 引用: {prd_ref}",
                )
            )
        else:
            dependencies.append(
                RoadmapDependency(
                    from_path=prd_path,
                    to_path=resolved_path,
                    kind=RoadmapDependencyKind.PRD,
                    detail=resolved_path,
                )
            )
    return tuple(dependencies)


def _resolve_prd_ref(
    prd_ref: str, prd_path_to_issue_number: dict[str, int | None]
) -> str | None:
    """Resolve a PRD reference to a known relative PRD path.

    Accepts bare filenames or relative paths under ``tasks/pending/`` or
    ``tasks/archive/``.
    """
    ref_path = Path(prd_ref)
    ref_name = ref_path.name
    for known_path in prd_path_to_issue_number:
        known = Path(known_path)
        if known.name == ref_name or known_path == prd_ref:
            return known_path
    return None


def scan_roadmap_prds(
    repo_path: Path,
    *,
    include_archived: bool = False,
    dirs: Sequence[str] | None = None,
) -> list[RoadmapPrd]:
    """Scan PRD files and return roadmap model objects.

    Args:
        repo_path: Repository root path.
        include_archived: Whether to include ``tasks/archive/`` PRDs.
        dirs: Optional explicit directories to scan.

    Returns:
        List of ``RoadmapPrd`` objects. Issue numbers and state are left as
        parsed from the file; callers should resolve live GitHub state via
        :mod:`roadmap_state_resolver`.
    """
    dependency_index_dirs = _resolve_prd_directories(repo_path, dirs)
    target_dirs = dependency_index_dirs
    if not include_archived:
        target_dirs = [d for d in dependency_index_dirs if "archive" not in d.name]

    # First pass: collect all PRD paths and issue numbers for dependency resolution.
    prd_path_to_issue_number: dict[str, int | None] = {}
    candidate_files: list[tuple[Path, Path]] = []
    for target_dir in dependency_index_dirs:
        for md_path in sorted(target_dir.glob("*.md")):
            relative_path = md_path.relative_to(repo_path).as_posix()
            prd_path_to_issue_number[relative_path] = None
            if target_dir in target_dirs:
                candidate_files.append((target_dir, md_path))

    # Second pass: parse issue numbers so PRD refs can map to issue numbers later.
    for _target_dir, md_path in candidate_files:
        relative_path = md_path.relative_to(repo_path).as_posix()
        try:
            prd_text = md_path.read_text(encoding="utf-8")
        except OSError as exc:
            _logger.warning("Failed to read PRD %s: %s", md_path, exc)
            continue
        issue_url = _extract_issue_url(prd_text)
        if issue_url is not None:
            try:
                prd_path_to_issue_number[relative_path] = parse_issue_number(issue_url)
            except ValueError:
                _logger.warning("Invalid issue URL in %s: %s", relative_path, issue_url)

    # Third pass: build RoadmapPrd objects.
    prds: list[RoadmapPrd] = []
    for target_dir, md_path in candidate_files:
        relative_path = md_path.relative_to(repo_path).as_posix()
        try:
            prd_text = md_path.read_text(encoding="utf-8")
        except OSError as exc:
            _logger.warning("Failed to read PRD %s: %s", md_path, exc)
            continue
        title = extract_title(prd_text, fallback_title=md_path.stem)
        issue_url = _extract_issue_url(prd_text)
        issue_number = prd_path_to_issue_number.get(relative_path)
        acceptance_checked, acceptance_total = _parse_acceptance_progress(prd_text)
        delivery_decl = parse_delivery_dependencies(prd_text)
        dependencies = _build_dependencies(
            relative_path, delivery_decl, prd_path_to_issue_number
        )
        status = "archived" if "archive" in target_dir.name else "pending"
        updated_at = datetime.fromtimestamp(
            md_path.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds")
        prds.append(
            RoadmapPrd(
                prd_path=relative_path,
                title=title,
                status=status,
                priority=_extract_priority(md_path.name),
                issue_url=issue_url,
                issue_number=issue_number,
                state=RoadmapPrdState.ARCHIVED
                if status == "archived"
                else RoadmapPrdState.NOT_STARTED,
                acceptance_total=acceptance_total,
                acceptance_checked=acceptance_checked,
                delivery_dependencies=dependencies,
                updated_at=updated_at,
                block_reason=None,
                next_action=None,
            )
        )

    return prds
