"""Tests for the roadmap PRD scanner."""

from __future__ import annotations

from pathlib import Path

from backend.core.shared.models.roadmap import RoadmapDependencyKind, RoadmapPrdState
from backend.core.use_cases.roadmap_prd_scanner import scan_roadmap_prds


def _write_prd(repo_path: Path, relative_path: str, content: str) -> Path:
    """Write a PRD file and return its absolute path."""
    prd_path = repo_path / relative_path
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(content, encoding="utf-8")
    return prd_path


def test_scan_finds_pending_and_archived_prds(tmp_path: Path) -> None:
    """Scanner should discover PRDs in both directories."""
    _write_prd(
        tmp_path,
        "tasks/pending/P1-FEAT-20260101-test-pending.md",
        "# PRD: Pending Feature\n\n## Acceptance Checklist\n- [ ] item 1\n- [x] item 2\n",
    )
    _write_prd(
        tmp_path,
        "tasks/archive/P2-BUG-20260101-test-archived.md",
        "# PRD: Archived Bug\n\n## Acceptance Checklist\n- [x] item 1\n",
    )

    prds = scan_roadmap_prds(tmp_path, include_archived=False)
    assert len(prds) == 1
    assert prds[0].status == "pending"

    all_prds = scan_roadmap_prds(tmp_path, include_archived=True)
    assert len(all_prds) == 2
    archived = next(p for p in all_prds if p.status == "archived")
    assert archived.state == RoadmapPrdState.ARCHIVED


def test_extracts_title_and_priority(tmp_path: Path) -> None:
    """Scanner should extract H1 title and priority token."""
    _write_prd(
        tmp_path,
        "tasks/pending/P0-FEAT-20260101-critical.md",
        "# PRD: Critical Feature\n\n## Acceptance Checklist\n- [ ] a\n",
    )
    prds = scan_roadmap_prds(tmp_path)
    assert len(prds) == 1
    assert prds[0].title == "Critical Feature"
    assert prds[0].priority == "P0"


def test_extracts_issue_url_and_number(tmp_path: Path) -> None:
    """Scanner should parse a real GitHub Issue URL from the PRD."""
    _write_prd(
        tmp_path,
        "tasks/pending/P1-FEAT-20260101-with-issue.md",
        (
            "# PRD: With Issue\n\n"
            "- GitHub Issue: https://github.com/org/repo/issues/42\n\n"
            "## Acceptance Checklist\n- [ ] a\n"
        ),
    )
    prds = scan_roadmap_prds(tmp_path)
    assert len(prds) == 1
    assert prds[0].issue_number == 42
    assert prds[0].issue_url == "https://github.com/org/repo/issues/42"


def test_placeholder_issue_is_ignored(tmp_path: Path) -> None:
    """Placeholder Issue lines should not be treated as real issues."""
    _write_prd(
        tmp_path,
        "tasks/pending/P1-FEAT-20260101-placeholder.md",
        (
            "# PRD: Placeholder\n\n"
            "- GitHub Issue: (to be created)\n\n"
            "## Acceptance Checklist\n- [ ] a\n"
        ),
    )
    prds = scan_roadmap_prds(tmp_path)
    assert len(prds) == 1
    assert prds[0].issue_number is None
    assert prds[0].issue_url is None


def test_counts_acceptance_progress(tmp_path: Path) -> None:
    """Scanner should count checked vs total acceptance items."""
    _write_prd(
        tmp_path,
        "tasks/pending/P1-FEAT-20260101-progress.md",
        (
            "# PRD: Progress\n\n"
            "## Acceptance Checklist\n"
            "- [x] done\n"
            "- [ ] todo\n"
            "- [X] also done\n"
        ),
    )
    prds = scan_roadmap_prds(tmp_path)
    assert len(prds) == 1
    assert prds[0].acceptance_total == 3
    assert prds[0].acceptance_checked == 2


def test_parses_delivery_dependencies(tmp_path: Path) -> None:
    """Scanner should build dependency edges from Delivery Dependencies."""
    _write_prd(
        tmp_path,
        "tasks/pending/P1-FEAT-20260101-deps.md",
        (
            "# PRD: Deps\n\n"
            "## Delivery Dependencies\n"
            "- Depends on tasks/issues: #7, tasks/pending/P1-FEAT-20260101-upstream.md\n"
            "- Depends on groups: infra\n"
            "- Gate type: soft\n"
        ),
    )
    _write_prd(
        tmp_path,
        "tasks/pending/P1-FEAT-20260101-upstream.md",
        "# PRD: Upstream\n\n## Acceptance Checklist\n- [ ] a\n",
    )
    prds = scan_roadmap_prds(tmp_path)
    prd = next(p for p in prds if p.prd_path == "tasks/pending/P1-FEAT-20260101-deps.md")
    kinds = {dep.kind for dep in prd.delivery_dependencies}
    assert RoadmapDependencyKind.ISSUE in kinds
    assert RoadmapDependencyKind.PRD in kinds
    assert RoadmapDependencyKind.GROUP in kinds


def test_unresolved_prd_dependency_is_marked(tmp_path: Path) -> None:
    """Missing PRD refs should be flagged as unresolved."""
    _write_prd(
        tmp_path,
        "tasks/pending/P1-FEAT-20260101-missing.md",
        (
            "# PRD: Missing\n\n"
            "## Delivery Dependencies\n"
            "- Depends on tasks/issues: tasks/pending/P1-FEAT-20260101-does-not-exist.md\n"
        ),
    )
    prds = scan_roadmap_prds(tmp_path)
    assert len(prds) == 1
    assert all(
        dep.kind is RoadmapDependencyKind.UNRESOLVED for dep in prds[0].delivery_dependencies
    )


def test_archived_prd_dependency_is_resolved_even_when_hidden(tmp_path: Path) -> None:
    """Archived PRD refs should resolve from pending scans."""
    _write_prd(
        tmp_path,
        "tasks/archive/P1-FEAT-20260614-200054-frontend-prd-roadmap.md",
        (
            "# PRD: Roadmap\n\n"
            "- GitHub Issue: https://github.com/org/repo/issues/42\n\n"
            "## Acceptance Checklist\n- [x] a\n"
        ),
    )
    _write_prd(
        tmp_path,
        "tasks/pending/P1-FEAT-20260614-203810-frontend-idea-inbox-cross-platform.md",
        (
            "# PRD: Idea Inbox\n\n"
            "## Delivery Dependencies\n"
            "- Depends on tasks/issues: `tasks/archive/P1-FEAT-20260614-200054-frontend-prd-roadmap.md`（已完成）\n"
        ),
    )

    pending_only_prds = scan_roadmap_prds(tmp_path, include_archived=False)
    pending_prd = next(
        prd
        for prd in pending_only_prds
        if prd.prd_path
        == "tasks/pending/P1-FEAT-20260614-203810-frontend-idea-inbox-cross-platform.md"
    )
    assert pending_prd.delivery_dependencies
    assert pending_prd.delivery_dependencies[0].kind is RoadmapDependencyKind.PRD
    assert pending_prd.delivery_dependencies[0].to_path == (
        "tasks/archive/P1-FEAT-20260614-200054-frontend-prd-roadmap.md"
    )
