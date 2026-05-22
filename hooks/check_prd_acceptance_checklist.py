#!/usr/bin/env python3
"""Check that active PRD acceptance checklists are fully completed."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from backend.core.shared.prd_checklist import parse_prd_checklist


ACTIVE_PRD_PATH_RE = re.compile(r"^tasks/[^/]+-prd-[^/]+\.md$")
ARCHIVED_PRD_PATH_RE = re.compile(r"^tasks/archive/[^/]+-prd-[^/]+\.md$")


def _repo_root() -> Path:
    """Return the repository root inferred from this file location."""

    return Path(__file__).resolve().parents[1]


def _relative_path(path: Path, repo_root: Path) -> Path | None:
    """Return a repository-relative path when the file is inside the repo."""

    try:
        return path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None


def _is_active_prd_path(path: Path, repo_root: Path) -> bool:
    """Return whether a path is an active root-level PRD markdown file."""

    relative_path = _relative_path(path, repo_root)
    if relative_path is None:
        return False

    if relative_path.parent != Path("tasks"):
        return False
    return bool(ACTIVE_PRD_PATH_RE.match(relative_path.as_posix()))


def _is_archived_prd_path(path: Path, repo_root: Path) -> bool:
    """Return whether a path is an archived PRD markdown file."""

    relative_path = _relative_path(path, repo_root)
    if relative_path is None:
        return False

    return bool(ARCHIVED_PRD_PATH_RE.match(relative_path.as_posix()))


def _staged_archive_prd_paths(repo_root: Path) -> set[Path]:
    """Return PRDs newly added, copied, or renamed into the archive in git index."""

    git_diff_process = subprocess.run(
        [
            "git",
            "diff",
            "--cached",
            "--name-status",
            "--diff-filter=ACR",
            "--",
            "tasks/archive",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    staged_archive_paths: set[Path] = set()
    for raw_status_line in git_diff_process.stdout.splitlines():
        status_parts = raw_status_line.split("\t")
        if not status_parts:
            continue

        staged_relative_path_text = status_parts[-1].strip()
        if not staged_relative_path_text:
            continue

        staged_relative_path = Path(staged_relative_path_text)
        if ARCHIVED_PRD_PATH_RE.match(staged_relative_path.as_posix()):
            staged_archive_paths.add(staged_relative_path)

    return staged_archive_paths


def _candidate_prd_paths(
    repo_root: Path,
    provided_paths: Iterable[Path],
    staged_archive_prd_paths: set[Path] | None = None,
) -> list[Path]:
    """Return active PRD paths to validate."""

    staged_archive_prd_paths = (
        _staged_archive_prd_paths(repo_root)
        if staged_archive_prd_paths is None
        else staged_archive_prd_paths
    )
    provided_paths_list = list(provided_paths)
    if provided_paths_list:
        candidate_paths: list[Path] = []
        for path in provided_paths_list:
            relative_path = _relative_path(path, repo_root)
            if relative_path is None:
                continue
            if _is_active_prd_path(path, repo_root):
                candidate_paths.append(path)
                continue
            if (
                _is_archived_prd_path(path, repo_root)
                and relative_path in staged_archive_prd_paths
            ):
                candidate_paths.append(path)
        return candidate_paths

    tasks_dir = repo_root / "tasks"
    if not tasks_dir.exists():
        return []

    discovered_paths: list[Path] = []
    for prd_path in sorted(tasks_dir.glob("*-prd-*.md")):
        if _is_active_prd_path(prd_path, repo_root):
            discovered_paths.append(prd_path)
    for archived_prd_path in sorted(staged_archive_prd_paths):
        discovered_paths.append(repo_root / archived_prd_path)
    return discovered_paths


def _validate_file(path: Path) -> list[tuple[int, str]]:
    """Read a PRD file and return any checklist issues."""

    file_content = path.read_text(encoding="utf-8")
    result = parse_prd_checklist(file_content)
    if not result.section_found:
        return [(-1, "Missing Acceptance Checklist section")]
    return result.unchecked_items


def main() -> int:
    """Run the acceptance checklist validation."""

    repo_root = _repo_root()
    provided_paths = [repo_root / Path(argument) for argument in sys.argv[1:]]
    candidate_paths = _candidate_prd_paths(repo_root, provided_paths)

    if not candidate_paths:
        return 0

    has_errors = False
    print("🔍 Checking PRD acceptance checklists...\n")

    for prd_path in candidate_paths:
        relative_path = prd_path.resolve().relative_to(repo_root.resolve())
        issues = _validate_file(prd_path)
        if not issues:
            print(f"✅ {relative_path.as_posix()}")
            continue

        has_errors = True
        print(f"❌ {relative_path.as_posix()}")
        for line_number, issue_text in issues:
            if line_number < 0:
                print(f"   - {issue_text}")
            else:
                print(f"   - L{line_number}: {issue_text}")
        print()

    if has_errors:
        print(
            "⚠️  One or more active PRD acceptance checklists still contain unchecked items."
        )
        return 1

    print("\n🎉 All active PRD acceptance checklists are complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
