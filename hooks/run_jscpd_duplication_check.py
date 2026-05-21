#!/usr/bin/env python3
"""Run jscpd and fail only when a candidate file participates in duplication."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from duplication_check_utils import (
    path_is_within_any_root,
    repo_root_from_hook,
    select_incremental_paths,
)

SUPPORTED_SUFFIXES: set[str] = {".py", ".js", ".jsx", ".ts", ".tsx"}
REPO_CODE_ROOTS: list[Path] = [Path("src/backend"), Path("frontend")]
JSCPD_CORPUS_ROOTS: list[Path] = [Path("src/backend"), Path("frontend")]
JSCPD_FORMATS = "python,javascript,jsx,typescript,tsx"
JSCPD_MIN_LINES = "5"
JSCPD_MIN_TOKENS = "50"


def _build_scan_targets(repo_root: Path, candidate_paths: list[Path]) -> list[str]:
    """Return the scan targets passed to jscpd."""

    scan_targets: list[str] = []
    seen_target_texts: set[str] = set()

    for root_path in JSCPD_CORPUS_ROOTS:
        absolute_root_path = repo_root / root_path
        if not absolute_root_path.exists():
            continue
        root_text = root_path.as_posix()
        if root_text in seen_target_texts:
            continue
        seen_target_texts.add(root_text)
        scan_targets.append(root_text)

    for candidate_path in candidate_paths:
        if path_is_within_any_root(candidate_path, JSCPD_CORPUS_ROOTS):
            continue
        candidate_text = candidate_path.as_posix()
        if candidate_text in seen_target_texts:
            continue
        seen_target_texts.add(candidate_text)
        scan_targets.append(candidate_text)

    return scan_targets


def _parse_jscpd_report(report_path: Path) -> list[dict[str, object]]:
    """Read and parse the jscpd JSON report."""

    report_text = report_path.read_text(encoding="utf-8")
    report_obj = json.loads(report_text)
    duplicates = report_obj.get("duplicates", [])
    if not isinstance(duplicates, list):
        return []
    return [duplicate for duplicate in duplicates if isinstance(duplicate, dict)]


def _duplicate_touches_candidate(
    duplicate_entry: dict[str, object], candidate_paths: set[Path]
) -> bool:
    """Return whether a duplicate entry involves any candidate file."""

    first_file_entry = duplicate_entry.get("firstFile")
    second_file_entry = duplicate_entry.get("secondFile")
    file_entries = [first_file_entry, second_file_entry]

    for file_entry in file_entries:
        if not isinstance(file_entry, dict):
            continue
        raw_name = file_entry.get("name")
        if not isinstance(raw_name, str):
            continue
        if Path(raw_name) in candidate_paths:
            return True
    return False


def _format_duplicate_message(
    duplicate_entry: dict[str, object], repo_root: Path
) -> str:
    """Format one duplicate entry for human-readable output."""

    first_file_entry = duplicate_entry.get("firstFile")
    second_file_entry = duplicate_entry.get("secondFile")
    lines_value = duplicate_entry.get("lines")
    tokens_value = duplicate_entry.get("tokens")

    def _format_file_entry(file_entry: object) -> str:
        if not isinstance(file_entry, dict):
            return "<unknown>"
        raw_name = file_entry.get("name")
        start_line = file_entry.get("start")
        end_line = file_entry.get("end")
        if not isinstance(raw_name, str):
            return "<unknown>"
        relative_file_path = Path(raw_name)
        display_path = relative_file_path.as_posix()
        if (repo_root / relative_file_path).exists():
            display_path = relative_file_path.as_posix()
        if isinstance(start_line, int) and isinstance(end_line, int):
            return f"{display_path}:{start_line}-{end_line}"
        return display_path

    return (
        f"- {_format_file_entry(first_file_entry)} <-> "
        f"{_format_file_entry(second_file_entry)} "
        f"({lines_value} lines, {tokens_value} tokens)"
    )


def main(argv: list[str] | None = None) -> int:
    """Run jscpd for the relevant candidate files."""

    repo_root = repo_root_from_hook(Path(__file__))
    raw_path_texts = sys.argv[1:] if argv is None else argv
    candidate_paths = select_incremental_paths(
        raw_path_texts=raw_path_texts,
        repo_root=repo_root,
        allowed_suffixes=SUPPORTED_SUFFIXES,
        allowed_root_paths=REPO_CODE_ROOTS,
    )

    if not candidate_paths:
        print("No candidate files detected for jscpd; skipping.")
        return 0

    scan_targets = _build_scan_targets(repo_root, candidate_paths)
    if not scan_targets:
        print("No jscpd scan targets resolved; skipping.")
        return 0

    candidate_path_set = set(candidate_paths)

    with tempfile.TemporaryDirectory(prefix="jscpd-") as temp_dir_name:
        report_dir_path = Path(temp_dir_name)
        report_path = report_dir_path / "jscpd-report.json"
        try:
            jscpd_process = subprocess.run(
                [
                    "jscpd",
                    "--min-lines",
                    JSCPD_MIN_LINES,
                    "--min-tokens",
                    JSCPD_MIN_TOKENS,
                    "--reporters",
                    "json",
                    "--output",
                    report_dir_path.as_posix(),
                    "--format",
                    JSCPD_FORMATS,
                    "--exitCode",
                    "0",
                    "--noTips",
                    *scan_targets,
                ],
                cwd=repo_root,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except FileNotFoundError:
            print(
                "jscpd executable was not found. Run through pre-commit so "
                "the pinned node dependency is installed.",
                file=sys.stderr,
            )
            return 1

        if jscpd_process.returncode not in (0,):
            sys.stdout.write(jscpd_process.stdout)
            sys.stderr.write(jscpd_process.stderr)
            return jscpd_process.returncode

        if not report_path.exists():
            if jscpd_process.stdout:
                sys.stdout.write(jscpd_process.stdout)
            if jscpd_process.stderr:
                sys.stderr.write(jscpd_process.stderr)
            print("jscpd did not produce a JSON report.", file=sys.stderr)
            return 1

        duplicate_entries = _parse_jscpd_report(report_path)
        relevant_duplicate_messages = [
            duplicate_entry
            for duplicate_entry in duplicate_entries
            if _duplicate_touches_candidate(duplicate_entry, candidate_path_set)
        ]

        if not relevant_duplicate_messages:
            print("jscpd found no candidate-touching duplicates.")
            return 0

        print("jscpd found duplication that touches candidate files:")
        for duplicate_entry in relevant_duplicate_messages:
            print(_format_duplicate_message(duplicate_entry, repo_root))
        return 1


if __name__ == "__main__":
    sys.exit(main())
