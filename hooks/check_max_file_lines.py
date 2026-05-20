#!/usr/bin/env python3
"""检查单文件非空行数是否超过阈值。

通过 pre-commit 调用，防止超大文件进入仓库。
统计范围仅排除纯空行（含仅含空白字符的行），保留注释与文档字符串。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def count_non_empty_lines(file_path: Path) -> int:
    """返回文件中非空行的数量。"""
    content = file_path.read_text(encoding="utf-8")
    return sum(1 for line in content.splitlines() if line.strip())


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
        "files",
        nargs="+",
        help="待检查的文件路径列表。",
    )
    args = parser.parse_args(argv)

    violations: list[tuple[Path, int]] = []
    for file_str in args.files:
        file_path = Path(file_str)
        if not file_path.is_file():
            continue
        line_count = count_non_empty_lines(file_path)
        if line_count > args.max_lines:
            violations.append((file_path, line_count))

    if not violations:
        return 0

    level = "WARNING" if args.warn_only else "ERROR"
    for file_path, line_count in violations:
        print(
            f"[{level}] {file_path}: {line_count} 非空行，"
            f"超过上限 {args.max_lines} 行。"
        )
    return 0 if args.warn_only else 1


if __name__ == "__main__":
    sys.exit(main())
