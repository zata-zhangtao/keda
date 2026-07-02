#!/usr/bin/env python3
"""Check that deliverable PRD acceptance checklists are fully completed."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

ACTIVE_PRD_PATH_RE = re.compile(
    r"^tasks/([^/]+-prd-[^/]+|P[0-3]-[A-Z]+-\d{8}-\d{6}-[^/]+)\.md$"
)
ARCHIVED_PRD_PATH_RE = re.compile(
    r"^tasks/archive/([^/]+-prd-[^/]+|P[0-3]-[A-Z]+-\d{8}-\d{6}-[^/]+)\.md$"
)
ACCEPTANCE_CHECKLIST_HEADING_RE = re.compile(
    r"^##\s+(?:\d+\.\s+)?(?:Acceptance Checklist\b.*|验收清单.*)\s*$"
)
TOP_LEVEL_HEADING_RE = re.compile(r"^##\s+")
CHECKBOX_RE = re.compile(r"^\s*[-*+]\s+\[(?P<mark>[ xX])\]\s*(?P<label>.*)$")
CODE_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def _repo_root(start_path: Path | None = None) -> Path:
    """Return the repository root inferred from cwd or a provided path."""

    start_path = Path.cwd() if start_path is None else start_path
    git_process = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if git_process.returncode == 0:
        return Path(git_process.stdout.strip()).resolve()
    return start_path.resolve()


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
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if git_diff_process.returncode != 0:
        return set()

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
    """Return deliverable PRD paths selected by the lifecycle filter."""

    staged_archive_prd_paths = (
        _staged_archive_prd_paths(repo_root)
        if staged_archive_prd_paths is None
        else staged_archive_prd_paths
    )
    provided_paths_list = list(provided_paths)
    if provided_paths_list:
        candidate_paths: list[Path] = []
        for path in provided_paths_list:
            absolute_path = path if path.is_absolute() else repo_root / path
            relative_path = _relative_path(absolute_path, repo_root)
            if relative_path is None:
                continue
            if _is_active_prd_path(absolute_path, repo_root):
                candidate_paths.append(absolute_path)
                continue
            if (
                _is_archived_prd_path(absolute_path, repo_root)
                and relative_path in staged_archive_prd_paths
            ):
                candidate_paths.append(absolute_path)
        return candidate_paths

    tasks_dir = repo_root / "tasks"
    if not tasks_dir.exists():
        return []

    discovered_paths: list[Path] = []
    for prd_path in sorted(tasks_dir.glob("*.md")):
        if _is_active_prd_path(prd_path, repo_root):
            discovered_paths.append(prd_path)
    for archived_prd_path in sorted(staged_archive_prd_paths):
        discovered_paths.append(repo_root / archived_prd_path)
    return discovered_paths


def _section_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Return the line bounds for the Acceptance Checklist section."""

    start_index: int | None = None
    for line_index, line in enumerate(lines):
        if ACCEPTANCE_CHECKLIST_HEADING_RE.match(line):
            start_index = line_index
            break

    if start_index is None:
        return None

    end_index = len(lines)
    for line_index in range(start_index + 1, len(lines)):
        if TOP_LEVEL_HEADING_RE.match(lines[line_index]):
            end_index = line_index
            break

    return start_index + 1, end_index


def _unchecked_items_in_acceptance_section(file_content: str) -> list[tuple[int, str]]:
    """Return unchecked checklist items found in the acceptance section."""

    lines = file_content.splitlines()
    section_bounds = _section_bounds(lines)
    if section_bounds is None:
        return [(-1, "Missing Acceptance Checklist section")]

    start_index, end_index = section_bounds
    unchecked_items: list[tuple[int, str]] = []
    in_code_block = False

    for line_index in range(start_index, end_index):
        line = lines[line_index]
        if CODE_FENCE_RE.match(line):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        checkbox_match = CHECKBOX_RE.match(line)
        if checkbox_match and checkbox_match.group("mark") == " ":
            unchecked_items.append((line_index + 1, line.rstrip()))

    return unchecked_items


def _validate_file(path: Path) -> list[tuple[int, str]]:
    """Read a PRD file and return any checklist issues."""

    file_content = path.read_text(encoding="utf-8")
    return _unchecked_items_in_acceptance_section(file_content)


def _build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Check deliverable PRD files for an Acceptance Checklist section "
            "with no unchecked items."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root to validate. Defaults to git root from cwd.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Discover active task PRDs instead of relying only on provided paths.",
    )
    parser.add_argument(
        "--check-provided",
        action="store_true",
        help=(
            "Validate explicitly provided paths even if they are pending PRDs "
            "or otherwise outside the default deliverable lifecycle filter."
        ),
    )
    parser.add_argument("paths", nargs="*", type=Path, help="PRD files to validate.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the acceptance checklist validation."""

    parser = _build_parser()
    args = parser.parse_args(argv)

    repo_root = _repo_root(args.repo_root)
    if args.check_provided:
        if not args.paths:
            parser.error("--check-provided requires at least one PRD path")
        candidate_paths = [
            path if path.is_absolute() else repo_root / path for path in args.paths
        ]
        missing_paths = [path for path in candidate_paths if not path.exists()]
        if missing_paths:
            print("Missing PRD path(s):")
            for missing_path in missing_paths:
                print(f"   - {missing_path}")
            return 1
    else:
        provided_paths = [] if args.all else args.paths
        candidate_paths = _candidate_prd_paths(repo_root, provided_paths)

    if not candidate_paths:
        return 0

    has_errors = False
    print("Checking PRD acceptance checklists...\n")

    for prd_path in candidate_paths:
        relative_path = prd_path.resolve().relative_to(repo_root.resolve())
        issues = _validate_file(prd_path)
        if not issues:
            print(f"PASS {relative_path.as_posix()}")
            continue

        has_errors = True
        print(f"FAIL {relative_path.as_posix()}")
        for line_number, issue_text in issues:
            if line_number < 0:
                print(f"   - {issue_text}")
            else:
                print(f"   - L{line_number}: {issue_text}")
        print()

    if has_errors:
        print("One or more deliverable PRD acceptance checklists are incomplete.")
        return 1

    print("\nAll deliverable PRD acceptance checklists are complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
