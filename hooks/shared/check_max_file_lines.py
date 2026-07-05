#!/usr/bin/env python3
"""检查单文件非空行数是否超过阈值。

通过 pre-commit 调用，防止超大文件进入仓库。
统计范围仅排除纯空行（含仅含空白字符的行），保留注释与文档字符串。
支持传入目录，目录会按可选的 --glob 模式递归展开为文件列表。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def count_non_empty_lines(file_path: Path) -> int:
    """返回文件中非空行的数量。"""
    content = file_path.read_text(encoding="utf-8")
    return sum(1 for line in content.splitlines() if line.strip())


def _expand_paths(raw_paths: list[str], glob_pattern: str | None) -> tuple[list[Path], list[str]]:
    """把位置参数展开为待检查文件列表。

    Args:
        raw_paths: 命令行传入的文件或目录路径。
        glob_pattern: 可选的 glob 过滤模式，仅在路径为目录时生效。

    Returns:
        (有效文件路径列表, 错误信息列表)。目录会递归展开；文件直接保留；
        不存在或无法访问的路径会生成错误信息。
    """
    files_to_check: list[Path] = []
    errors: list[str] = []

    for raw_path in raw_paths:
        path = Path(raw_path)
        if path.is_file():
            files_to_check.append(path)
        elif path.is_dir():
            for child_path in path.rglob("*"):
                if not child_path.is_file():
                    continue
                if glob_pattern is not None and not child_path.match(glob_pattern):
                    continue
                files_to_check.append(child_path)
        else:
            errors.append(f"Path does not exist or is not a regular file/directory: {raw_path}")

    # 去重同时保持顺序。
    seen: set[Path] = set()
    deduplicated_files: list[Path] = []
    for file_path in files_to_check:
        resolved_path = file_path.resolve()
        if resolved_path in seen:
            continue
        seen.add(resolved_path)
        deduplicated_files.append(file_path)

    return deduplicated_files, errors


def main(argv: list[str] | None = None) -> int:
    """入口函数，返回退出码。"""
    parser = argparse.ArgumentParser(
        description="检查文件非空行数是否超过阈值。",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=1000,
        help="非空行数上限（默认：1000）。",
    )
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="仅输出警告，不返回非零退出码。",
    )
    parser.add_argument(
        "--glob",
        default=None,
        help="目录递归时的 glob 过滤模式（例如 '*.py'）。",
    )
    parser.add_argument(
        "--allow-list-file",
        default=None,
        help=(
            "可选的白名单文件路径；每行一个相对仓库根的文件路径。"
            "命中白名单的文件即使超过 --max-lines 也只产生 WARNING，不会"
            "导致非零退出码。用于标记存量超额文件同时仍对新文件保持硬门禁。"
        ),
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="待检查的文件或目录路径列表。",
    )
    args = parser.parse_args(argv)

    files_to_check, errors = _expand_paths(args.files, args.glob)

    for error_message in errors:
        print(f"[ERROR] {error_message}")

    allowed_paths: set[str] = set()
    if args.allow_list_file is not None:
        allow_list_path = Path(args.allow_list_file)
        if not allow_list_path.is_file():
            print(
                f"[ERROR] Allow-list file does not exist: {allow_list_path}",
            )
            errors.append(f"Allow-list file does not exist: {allow_list_path}")
        else:
            for raw_line in allow_list_path.read_text(encoding="utf-8").splitlines():
                stripped_line = raw_line.strip()
                if not stripped_line or stripped_line.startswith("#"):
                    continue
                allowed_paths.add(stripped_line)

    violations: list[tuple[Path, int]] = []
    whitelisted_violations: list[tuple[Path, int]] = []
    for file_path in files_to_check:
        line_count = count_non_empty_lines(file_path)
        if line_count <= args.max_lines:
            continue
        try:
            relative_label = file_path.resolve().relative_to(Path.cwd()).as_posix()
        except ValueError:
            relative_label = file_path.as_posix()
        if relative_label in allowed_paths:
            whitelisted_violations.append((file_path, line_count))
        else:
            violations.append((file_path, line_count))

    if violations:
        level = "WARNING" if args.warn_only else "ERROR"
        for file_path, line_count in violations:
            print(
                f"[{level}] {file_path}: {line_count} 非空行，" f"超过上限 {args.max_lines} 行。",
            )
    if whitelisted_violations:
        for file_path, line_count in whitelisted_violations:
            print(
                f"[WARNING] {file_path}: {line_count} 非空行，"
                f"超过上限 {args.max_lines} 行 (allow-listed)。",
            )

    if errors and not args.warn_only:
        return 1
    if violations:
        return 0 if args.warn_only else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
