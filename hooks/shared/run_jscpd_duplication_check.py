#!/usr/bin/env python3
"""Run jscpd and fail only when a candidate file's changed lines participate in duplication."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from duplication_check_utils import (
    changed_line_ranges,
    path_is_within_any_root,
    repo_root_from_hook,
    select_incremental_paths,
)

SUPPORTED_SUFFIXES: set[str] = {".py", ".js", ".jsx", ".ts", ".tsx"}
REPO_CODE_ROOTS: list[Path] = [
    Path("src/backend"),
    Path("frontend-admin"),
    Path("frontend-public"),
]
JSCPD_CORPUS_ROOTS: list[Path] = [
    Path("src/backend"),
    Path("frontend-admin"),
    Path("frontend-public"),
]
JSCPD_FORMATS = "python,javascript,jsx,typescript,tsx"
JSCPD_MIN_LINES = "5"
JSCPD_MIN_TOKENS = "50"
# jscpd 默认没有任何忽略规则（也不读取 .gitignore），必须显式排除依赖与构建产物目录
JSCPD_IGNORE_GLOBS = "**/node_modules/**,**/dist/**,**/build/**,**/.next/**,**/coverage/**"


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


def _file_entry_line_span(
    file_entry: dict[str, object], duplicate_lines: object
) -> tuple[int, int] | None:
    """返回某个重复片段在该文件中的行区间（1-based 闭区间）。

    jscpd 部分版本的 ``end`` / ``endLoc`` 会给出反向或错误的行号（实测 secondFile
    出现 start=29、end=13 的反区间），因此改用可靠的起始行加顶层匹配行数
    （``duplicate_lines``）推算区间，避免 (start > end) 造成空区间而漏判。
    """

    start_value = file_entry.get("start")
    if not isinstance(start_value, int):
        start_location = file_entry.get("startLoc")
        if isinstance(start_location, dict) and isinstance(start_location.get("line"), int):
            start_value = start_location["line"]
    if not isinstance(start_value, int):
        return None
    line_count = duplicate_lines if isinstance(duplicate_lines, int) and duplicate_lines > 0 else 1
    return (start_value, start_value + line_count - 1)


def _ranges_intersect(span: tuple[int, int], ranges: list[tuple[int, int]]) -> bool:
    """判断单个区间是否与区间列表中的任一区间相交（均为闭区间）。"""

    span_start, span_end = span
    return any(
        span_start <= range_end and range_start <= span_end for range_start, range_end in ranges
    )


def _duplicate_touches_changed_lines(
    duplicate_entry: dict[str, object],
    candidate_changed_ranges: dict[Path, list[tuple[int, int]]],
) -> bool:
    """判断重复是否落在候选文件“本次实际改动的行”上。

    仅当重复片段与候选文件的改动行区间相交时才算命中，从而放过“改动没碰到的
    历史骨架重复”（如各页面共用的 Next.js 页头样板），只拦截本次变更真正引入或
    触达的重复。候选文件解析不出行级差异（空区间，通常是全新文件）时保持严格，
    整文件视为改动。
    """

    duplicate_lines = duplicate_entry.get("lines")
    file_entries = [duplicate_entry.get("firstFile"), duplicate_entry.get("secondFile")]
    for file_entry in file_entries:
        if not isinstance(file_entry, dict):
            continue
        raw_name = file_entry.get("name")
        if not isinstance(raw_name, str):
            continue
        candidate_path = Path(raw_name)
        if candidate_path not in candidate_changed_ranges:
            continue
        changed_ranges = candidate_changed_ranges[candidate_path]
        if not changed_ranges:
            return True
        duplicate_span = _file_entry_line_span(file_entry, duplicate_lines)
        if duplicate_span is None:
            return True
        if _ranges_intersect(duplicate_span, changed_ranges):
            return True
    return False


def _format_duplicate_message(duplicate_entry: dict[str, object]) -> str:
    """Format one duplicate entry for human-readable output."""

    first_file_entry = duplicate_entry.get("firstFile")
    second_file_entry = duplicate_entry.get("secondFile")
    lines_value = duplicate_entry.get("lines")
    tokens_value = duplicate_entry.get("tokens")

    def _format_file_entry(file_entry: object) -> str:
        if not isinstance(file_entry, dict):
            return "<unknown>"
        raw_name = file_entry.get("name")
        if not isinstance(raw_name, str):
            return "<unknown>"
        display_path = Path(raw_name).as_posix()
        # 用可靠的起始行 + 匹配行数推算显示区间,避免 jscpd 反向 end 造成的
        # "29-13" 之类误导性输出
        line_span = _file_entry_line_span(file_entry, lines_value)
        if line_span is not None:
            return f"{display_path}:{line_span[0]}-{line_span[1]}"
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

    candidate_changed_ranges = {
        candidate_path: changed_line_ranges(repo_root, candidate_path)
        for candidate_path in candidate_paths
    }

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
                    "--ignore",
                    JSCPD_IGNORE_GLOBS,
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
            if _duplicate_touches_changed_lines(duplicate_entry, candidate_changed_ranges)
        ]

        if not relevant_duplicate_messages:
            print("jscpd found no duplication on changed lines of candidate files.")
            return 0

        print("jscpd found duplication that touches changed lines of candidate files:")
        for duplicate_entry in relevant_duplicate_messages:
            print(_format_duplicate_message(duplicate_entry))
        return 1


if __name__ == "__main__":
    sys.exit(main())
