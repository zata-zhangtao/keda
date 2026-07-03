#!/usr/bin/env python3
"""数据库 schema 约定检查。

当前覆盖 Alembic 迁移脚本约定：

- ``alembic/versions/`` 下 ``.py`` 迁移脚本的 ``revision`` 变量长度不得超过
  Alembic 默认 ``alembic_version.version_num`` 列宽（32 字符）。
- 迁移脚本文件名必须符合 ``YYYYMMDD<sep>HHMMSS<sep><slug>.py`` 格式，其中
  ``<sep>`` 默认可自动探测（在 ``-`` 和 ``_`` 之间取多数），也可通过
  ``--filename-separator`` 显式指定。
- 可选开启 ``文件内 revision 字符串 == 文件名时间戳前缀`` 检查，供采用
  ``时间戳前缀即 revision`` 约定的派生项目使用。

SQLAlchemy 模型表/列备注检查已拆分到独立的
``hooks/shared/check_sqlalchemy_model_comments.py``，由 pre-commit hook
``check-sqlalchemy-model-comments`` 执行。这样每个检查项都是自包含脚本，便于
分发到不同目录结构的下游仓库。

通过 pre-commit 在本地提交前强制执行。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ALEMBIC_VERSIONS_DIR_RELPATH: Path = Path("alembic/versions")
"""被检查的 Alembic 迁移脚本目录相对项目根路径。"""

ALEMBIC_REVISION_PATTERN = re.compile(
    r'^\s*revision\s*(?::\s*\w+\s*)?=\s*["\']([^"\']+)["\']',
    re.MULTILINE,
)
"""匹配迁移文件中 ``revision = "..."`` / ``revision: str = "..."`` 的正则。"""

DEFAULT_FILENAME_SEPARATOR: str = "-"
"""默认文件名分隔符。"""

SUPPORTED_FILENAME_SEPARATORS: list[str] = ["-", "_"]
"""支持自动探测的文件名分隔符候选列表。"""

MIGRATION_FILENAME_PATTERN_TEMPLATE = (
    r"^(?P<date>\d{8})__SEP__(?P<time>\d{6})__SEP__"
    r"(?P<slug>[a-z][a-z0-9_]*)(?:__SEP__(?P<suffix>\d+))?\.py$"
)
"""迁移文件名正则模板，需将 ``__SEP__`` 替换为实际分隔符。"""


# ==========================================
# 1. 数据结构
# ==========================================


@dataclass(frozen=True)
class MigrationViolation:
    """单条迁移脚本违规记录。

    Attributes:
        file_path: 发生违规的文件路径。
        target: 违规的 revision ID 或文件名。
        reason: 人类可读的违规原因描述。
    """

    file_path: Path
    target: str
    reason: str


@dataclass(frozen=True)
class ParsedMigrationFilename:
    """解析后的迁移文件名信息。

    Attributes:
        date_part: 日期段，如 ``20260625``。
        time_part: 时间段，如 ``111246``。
        slug: 描述段，如 ``agent_platform_init``。
        suffix: 同名冲突后缀，如 ``2``；无后缀时为 ``None``。
    """

    date_part: str
    time_part: str
    slug: str
    suffix: str | None


@dataclass
class CheckResult:
    """检查汇总结果。

    Attributes:
        violations: 所有发现的违规列表。
        checked_files_count: 实际检查的文件数量。
    """

    violations: list[MigrationViolation] = field(default_factory=list)
    checked_files_count: int = 0

    @property
    def passed(self) -> bool:
        """是否全部通过（无违规）。"""
        return len(self.violations) == 0


# ==========================================
# 2. Alembic 迁移脚本检查
# ==========================================


def build_filename_pattern(separator: str) -> re.Pattern[str]:
    """构造指定分隔符的迁移文件名正则。

    Args:
        separator: 文件名分隔符，通常为 ``-`` 或 ``_``。

    Returns:
        编译后的正则表达式。
    """
    escaped_sep = re.escape(separator)
    pattern = MIGRATION_FILENAME_PATTERN_TEMPLATE.replace("__SEP__", escaped_sep)
    return re.compile(pattern)


def parse_migration_filename(
    filename: str,
    separator: str = DEFAULT_FILENAME_SEPARATOR,
) -> ParsedMigrationFilename | None:
    """解析迁移文件名。

    Args:
        filename: 迁移文件名，不含路径。
        separator: 文件名分隔符。

    Returns:
        解析成功返回 ``ParsedMigrationFilename``，否则返回 ``None``。
    """
    pattern = build_filename_pattern(separator)
    match = pattern.match(filename)
    if not match:
        return None
    return ParsedMigrationFilename(
        date_part=match.group("date"),
        time_part=match.group("time"),
        slug=match.group("slug"),
        suffix=match.group("suffix"),
    )


def detect_filename_separator(migration_files: list[Path]) -> str:
    """从候选迁移文件中自动探测文件名分隔符。

    分别用 ``-`` 和 ``_`` 尝试匹配每个文件名，统计命中次数，返回多数派分隔符。
    若两种分隔符命中数相同或都没有命中，回退到默认分隔符 ``-``。

    Args:
        migration_files: 候选迁移文件路径列表。

    Returns:
        探测到的分隔符，或默认分隔符 ``-``。
    """
    match_counts: dict[str, int] = {sep: 0 for sep in SUPPORTED_FILENAME_SEPARATORS}

    for migration_file in migration_files:
        for separator in SUPPORTED_FILENAME_SEPARATORS:
            if parse_migration_filename(migration_file.name, separator) is not None:
                match_counts[separator] += 1

    if not any(match_counts.values()):
        return DEFAULT_FILENAME_SEPARATOR

    best_separator = max(match_counts, key=lambda sep: match_counts[sep])
    best_count = match_counts[best_separator]

    # 出现平票时回退到默认分隔符，避免武断选择。
    if list(match_counts.values()).count(best_count) > 1:
        return DEFAULT_FILENAME_SEPARATOR

    return best_separator


def extract_revision_ids(file_path: Path) -> list[str]:
    """从单个 Alembic 迁移文件中提取所有 revision ID。

    Args:
        file_path: 迁移文件路径。

    Returns:
        提取到的 revision ID 列表。
    """
    content = file_path.read_text(encoding="utf-8")
    return ALEMBIC_REVISION_PATTERN.findall(content)


def check_alembic_migration_file(
    migration_file: Path,
    max_length: int = 32,
    separator: str = DEFAULT_FILENAME_SEPARATOR,
    require_revision_equals_timestamp_prefix: bool = False,
    disallow_zero_time: bool = False,
) -> list[MigrationViolation]:
    """检查单个 Alembic 迁移文件的命名与 revision 约定。

    Args:
        migration_file: 迁移文件路径。
        max_length: revision ID 最大允许长度（默认 32）。
        separator: 文件名分隔符（默认 ``-``）。
        require_revision_equals_timestamp_prefix: 是否要求文件内 ``revision``
            字符串等于文件名中的时间戳前缀（不含 slug 段）。
        disallow_zero_time: 是否禁止文件名中的时间部分为 ``000000``。

    Returns:
        违规记录列表。
    """
    file_violations: list[MigrationViolation] = []
    filename = migration_file.name

    parsed = parse_migration_filename(filename, separator)
    if parsed is None:
        file_violations.append(
            MigrationViolation(
                file_path=migration_file,
                target=filename,
                reason=(
                    f"文件名不符合迁移脚本命名约定 "
                    f"YYYY{separator}MM{separator}DD{separator}HH{separator}MM{separator}SS"
                    f"{separator}<slug>.py（分隔符为 '{separator}'）。"
                ),
            )
        )
        # 文件名格式已错，后续 revision 一致性检查无意义。
        return file_violations

    if disallow_zero_time and parsed.time_part == "000000":
        file_violations.append(
            MigrationViolation(
                file_path=migration_file,
                target=filename,
                reason="文件名中的时间部分为 '000000'，疑似手工归零时间戳。",
            )
        )

    revision_ids = extract_revision_ids(migration_file)
    if not revision_ids:
        file_violations.append(
            MigrationViolation(
                file_path=migration_file,
                target=filename,
                reason="未在迁移文件中找到 ``revision = '...'`` 变量。",
            )
        )
        return file_violations

    for revision_id in revision_ids:
        if len(revision_id) > max_length:
            file_violations.append(
                MigrationViolation(
                    file_path=migration_file,
                    target=revision_id,
                    reason=(
                        f"revision ID 长度为 {len(revision_id)}，"
                        f"超过 alembic_version.version_num 默认上限 {max_length}。"
                    ),
                )
            )

        if require_revision_equals_timestamp_prefix:
            expected_revision = f"{parsed.date_part}{separator}{parsed.time_part}"
            if revision_id != expected_revision:
                file_violations.append(
                    MigrationViolation(
                        file_path=migration_file,
                        target=revision_id,
                        reason=(
                            f"revision ID '{revision_id}' 与文件名时间戳前缀 "
                            f"'{expected_revision}' 不一致；当前项目约定时间戳前缀 "
                            f"即 revision。"
                        ),
                    )
                )

    return file_violations


def run_alembic_revision_check(
    versions_dir: Path,
    max_length: int = 32,
    separator: str = DEFAULT_FILENAME_SEPARATOR,
    require_revision_equals_timestamp_prefix: bool = False,
    disallow_zero_time: bool = False,
) -> CheckResult:
    """对整个 Alembic versions 目录执行迁移约定检查。

    Args:
        versions_dir: Alembic versions 目录路径。
        max_length: revision ID 最大允许长度（默认 32）。
        separator: 文件名分隔符（默认 ``-``）。
        require_revision_equals_timestamp_prefix: 是否要求 revision 字符串等于
            文件名中的时间戳前缀。
        disallow_zero_time: 是否禁止文件名中的时间部分为 ``000000``。

    Returns:
        检查汇总结果。
    """
    aggregated_result = CheckResult()
    if not versions_dir.exists():
        return aggregated_result

    for migration_file in sorted(versions_dir.glob("*.py")):
        aggregated_result.checked_files_count += 1
        file_violations = check_alembic_migration_file(
            migration_file,
            max_length,
            separator,
            require_revision_equals_timestamp_prefix,
            disallow_zero_time,
        )
        aggregated_result.violations.extend(file_violations)

    return aggregated_result


# ==========================================
# 3. 输出与入口
# ==========================================


def _format_report(check_result: CheckResult) -> str:
    """将检查结果格式化为可读报告。

    Args:
        check_result: 检查汇总结果。

    Returns:
        格式化后的报告字符串。
    """
    report_lines: list[str] = [
        f"\nAlembic 迁移约定检查 — 共扫描 {check_result.checked_files_count} 个文件\n"
    ]

    if check_result.passed:
        report_lines.append("✅ 无违规。")
        return "\n".join(report_lines)

    report_lines.append(f"❌ 发现 {len(check_result.violations)} 处违规：\n")
    for violation in check_result.violations:
        report_lines.append(
            f"  [alembic_revision] {violation.file_path.name} ({violation.target})\n"
            f"    {violation.reason}\n"
        )
    return "\n".join(report_lines)


def _resolve_dir(arg_value: Path) -> Path:
    """将命令行传入的目录解析为绝对路径。

    兼容 ``pre-commit`` 默认从仓库根目录运行以及直接在工作子树目录运行的两种场景。

    Args:
        arg_value: 命令行传入的目录路径（可能为相对路径）。

    Returns:
        解析后的绝对路径。
    """
    if arg_value.is_absolute():
        return arg_value

    candidate_paths: list[Path] = [Path.cwd() / arg_value]
    if Path.cwd().name == arg_value.name:
        candidate_paths.append(Path.cwd())
    for candidate in candidate_paths:
        if candidate.exists():
            return candidate
    return candidate_paths[0]


def _classify_migration_file(file_path: Path, versions_dir: Path) -> bool:
    """判断文件是否位于 Alembic versions 目录内。

    Args:
        file_path: 待判断文件路径。
        versions_dir: Alembic versions 目录绝对路径。

    Returns:
        文件属于 versions 目录则为 True。
    """
    try:
        file_path.resolve().relative_to(versions_dir.resolve())
        return True
    except ValueError:
        return False


def main(argv: Optional[list[str]] = None) -> int:
    """主入口：扫描 Alembic versions 目录或检查指定文件，返回退出码。"""
    parser = argparse.ArgumentParser(
        description="检查 Alembic 迁移脚本 schema 约定。",
    )
    parser.add_argument(
        "--alembic-versions-dir",
        type=Path,
        default=ALEMBIC_VERSIONS_DIR_RELPATH,
        help="Alembic versions 目录路径（默认：alembic/versions）。",
    )
    parser.add_argument(
        "--max-revision-length",
        type=int,
        default=32,
        help="revision ID 最大长度（默认：32）。",
    )
    parser.add_argument(
        "--filename-separator",
        type=str,
        default=None,
        help=(
            "文件名分隔符（默认：自动探测；探测失败时回退到 '-'）。"
            "显式传入本参数可覆盖自动探测结果。"
        ),
    )
    parser.add_argument(
        "--require-revision-equals-timestamp-prefix",
        action="store_true",
        help=(
            "要求文件内 revision 字符串等于文件名中的时间戳前缀 "
            "（YYYY<sep>MM<sep>DD<sep>HH<sep>MM<sep>SS，不含 slug 段）。"
        ),
    )
    parser.add_argument(
        "--disallow-zero-time",
        action="store_true",
        help="禁止文件名中的时间部分为 '000000'（防止手工归零时间戳）。",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="待检查的迁移文件路径；为空时扫描整个目录。",
    )
    args = parser.parse_args(argv)

    versions_dir = _resolve_dir(args.alembic_versions_dir)
    result = CheckResult()

    if args.files:
        candidate_files: list[Path] = []
        for file_str in args.files:
            file_path = Path(file_str).resolve()
            if not file_path.is_file():
                continue
            if not _classify_migration_file(file_path, versions_dir):
                continue
            candidate_files.append(file_path)

        separator = (
            args.filename_separator
            if args.filename_separator is not None
            else detect_filename_separator(candidate_files)
        )

        for file_path in candidate_files:
            result.checked_files_count += 1
            result.violations.extend(
                check_alembic_migration_file(
                    file_path,
                    args.max_revision_length,
                    separator,
                    args.require_revision_equals_timestamp_prefix,
                    args.disallow_zero_time,
                )
            )
    else:
        candidate_files = sorted(versions_dir.glob("*.py")) if versions_dir.exists() else []
        separator = (
            args.filename_separator
            if args.filename_separator is not None
            else detect_filename_separator(candidate_files)
        )
        result = run_alembic_revision_check(
            versions_dir,
            args.max_revision_length,
            separator,
            args.require_revision_equals_timestamp_prefix,
            args.disallow_zero_time,
        )

    print(_format_report(result))

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
