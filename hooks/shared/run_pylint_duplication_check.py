#!/usr/bin/env python3
"""Run pylint duplicate-code and fail only on candidate-touching messages."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from duplication_check_utils import (
    path_is_within_any_root,
    repo_root_from_hook,
    select_incremental_paths,
)

PYTHON_SUFFIXES: set[str] = {".py"}
BACKEND_ROOTS: list[Path] = [Path("src/backend")]
PYTHON_CORPUS_ROOTS: list[Path] = [Path("src/backend")]
PYLINT_DUPLICATE_REF_RE = re.compile(
    r"==(?P<module>[A-Za-z_][A-Za-z0-9_.]*):\[(?P<start>\d+):(?P<end>\d+)\]"
)


def _build_scan_targets(candidate_paths: list[Path]) -> list[str]:
    """Return paths passed to pylint."""

    scan_targets: list[str] = []
    seen_target_texts: set[str] = set()

    for root_path in PYTHON_CORPUS_ROOTS:
        root_text = root_path.as_posix()
        seen_target_texts.add(root_text)
        scan_targets.append(root_text)

    for candidate_path in candidate_paths:
        if path_is_within_any_root(candidate_path, PYTHON_CORPUS_ROOTS):
            continue
        candidate_text = candidate_path.as_posix()
        if candidate_text in seen_target_texts:
            continue
        seen_target_texts.add(candidate_text)
        scan_targets.append(candidate_text)

    return scan_targets


def _module_reference_to_path(module_reference: str) -> Path:
    """Convert a pylint duplicate module reference to a Python file path."""

    return Path(*module_reference.split(".")).with_suffix(".py")


def _referenced_paths_from_message(message_text: str) -> list[Path]:
    """Extract repository-relative file paths from a pylint message."""

    referenced_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for duplicate_match in PYLINT_DUPLICATE_REF_RE.finditer(message_text):
        referenced_path = _module_reference_to_path(duplicate_match.group("module"))
        if referenced_path in seen_paths:
            continue
        seen_paths.add(referenced_path)
        referenced_paths.append(referenced_path)
    return referenced_paths


def _issue_touches_candidate(
    issue_entry: dict[str, object], candidate_paths: set[Path]
) -> bool:
    """Return whether a pylint JSON issue references any candidate file."""

    referenced_paths = _referenced_paths_from_message(
        str(issue_entry.get("message", ""))
    )
    if referenced_paths:
        return any(
            referenced_path in candidate_paths for referenced_path in referenced_paths
        )

    raw_issue_path = issue_entry.get("path")
    if isinstance(raw_issue_path, str):
        referenced_paths.append(Path(raw_issue_path))

    return any(
        referenced_path in candidate_paths for referenced_path in referenced_paths
    )


def _format_issue_message(issue_entry: dict[str, object]) -> str:
    """Format one filtered pylint duplicate-code issue."""

    message_text = str(issue_entry.get("message", "")).strip()
    referenced_paths = _referenced_paths_from_message(message_text)
    if referenced_paths:
        display_refs = ", ".join(path.as_posix() for path in referenced_paths)
        return f"- {display_refs}"

    raw_issue_path = issue_entry.get("path")
    if isinstance(raw_issue_path, str):
        return f"- {raw_issue_path}"
    return "- <unknown duplicate-code issue>"


def _parse_pylint_json(stdout_text: str) -> list[dict[str, object]]:
    """Parse pylint JSON output."""

    if not stdout_text.strip():
        return []

    parsed_obj = json.loads(stdout_text)
    if not isinstance(parsed_obj, list):
        return []
    return [issue for issue in parsed_obj if isinstance(issue, dict)]


def main(argv: list[str] | None = None) -> int:
    """Run pylint duplicate-code for relevant Python candidates."""

    repo_root = repo_root_from_hook(Path(__file__))
    raw_path_texts = sys.argv[1:] if argv is None else argv
    candidate_paths = select_incremental_paths(
        raw_path_texts=raw_path_texts,
        repo_root=repo_root,
        allowed_suffixes=PYTHON_SUFFIXES,
        allowed_root_paths=BACKEND_ROOTS,
    )

    if not candidate_paths:
        print("No candidate Python files detected for pylint duplicate-code; skipping.")
        return 0

    scan_targets = _build_scan_targets(candidate_paths)
    if not scan_targets:
        print("No pylint scan targets resolved; skipping.")
        return 0

    pylint_process = subprocess.run(
        [
            "uv",
            "run",
            "pylint",
            "--disable=all",
            "--enable=duplicate-code",
            "--output-format=json",
            "--reports=n",
            "--score=n",
            *scan_targets,
        ],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    try:
        issue_entries = _parse_pylint_json(pylint_process.stdout)
    except json.JSONDecodeError:
        sys.stdout.write(pylint_process.stdout)
        sys.stderr.write(pylint_process.stderr)
        return pylint_process.returncode or 1

    if pylint_process.returncode not in (0, 8):
        sys.stdout.write(pylint_process.stdout)
        sys.stderr.write(pylint_process.stderr)
        return pylint_process.returncode

    candidate_path_set = set(candidate_paths)
    relevant_issue_entries = [
        issue_entry
        for issue_entry in issue_entries
        if issue_entry.get("symbol") == "duplicate-code"
        and _issue_touches_candidate(issue_entry, candidate_path_set)
    ]

    if not relevant_issue_entries:
        print("pylint duplicate-code found no candidate-touching duplicates.")
        return 0

    print("pylint duplicate-code found duplication touching candidate files:")
    for issue_entry in relevant_issue_entries:
        print(_format_issue_message(issue_entry))
    return 1


if __name__ == "__main__":
    sys.exit(main())
