#!/usr/bin/env python3
"""数据库 schema 约定检查。

覆盖两类文件：

1. SQLAlchemy 模型（``src/backend/infrastructure/persistence/models/`` 下 ``.py``）：
   - 含 ``__tablename__`` 属性的类必须通过 ``__table_args__`` 提供非空 ``comment`` 字符串（表备注）。
   - 每个 ``Column(...)`` / ``mapped_column(...)`` 调用必须显式带 ``comment="..."`` 关键字参数（字段备注）。

2. Alembic 迁移脚本（``alembic/versions/`` 下 ``.py``）：
   - ``revision`` 变量长度不得超过 Alembic 默认 ``alembic_version.version_num`` 列宽（32 字符）。

通过 pre-commit 在本地提交前强制执行。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


MODELS_DIR_RELPATH: Path = Path("src/backend/infrastructure/persistence/models")
"""被检查的模型目录相对项目根路径。"""

ALEMBIC_VERSIONS_DIR_RELPATH: Path = Path("alembic/versions")
"""被检查的 Alembic 迁移脚本目录相对项目根路径。"""

ALEMBIC_REVISION_PATTERN = re.compile(
    r'^\s*revision\s*(?::\s*\w+\s*)?=\s*["\']([^"\']+)["\']',
    re.MULTILINE,
)
"""匹配迁移文件中 ``revision = "..."`` / ``revision: str = "..."`` 的正则。"""


# ==========================================
# 1. 数据结构
# ==========================================


@dataclass(frozen=True)
class ModelViolation:
    """单条模型备注违规记录。

    Attributes:
        file_path: 发生违规的文件路径。
        line_number: 违规位置所在行号。
        kind: 违规类型 (``table`` 或 ``column``)。
        target: 违规所在类名或文件名。
        reason: 人类可读的违规原因描述。
    """

    file_path: Path
    line_number: int
    kind: str
    target: str
    reason: str


@dataclass
class CheckResult:
    """检查汇总结果。

    Attributes:
        violations: 所有发现的违规列表。
        checked_files_count: 实际检查的文件数量。
    """

    violations: list[ModelViolation] = field(default_factory=list)
    checked_files_count: int = 0

    @property
    def passed(self) -> bool:
        """是否全部通过（无违规）。"""
        return len(self.violations) == 0


# ==========================================
# 2. AST 工具方法
# ==========================================


def _is_column_call(call_node: ast.Call) -> Optional[str]:
    """检查 ``call_node`` 是否为 ``Column(...)`` / ``mapped_column(...)`` 调用。

    Args:
        call_node: AST 调用节点。

    Returns:
        匹配的函数名字符串，未匹配则返回 ``None``。
    """
    func_expr: ast.AST = call_node.func
    if isinstance(func_expr, ast.Name) and func_expr.id in {"Column", "mapped_column"}:
        return func_expr.id
    return None


def _call_has_comment_kwarg(call_node: ast.Call) -> bool:
    """检查 ``Call`` 节点是否带 ``comment="..."`` 关键字参数。

    Args:
        call_node: AST 调用节点。

    Returns:
        存在 ``comment`` kwarg 则为 True。
    """
    for keyword in call_node.keywords:
        if keyword.arg == "comment":
            return True
    return False


def _class_has_tablename(class_node: ast.ClassDef) -> bool:
    """检查类是否定义了 ``__tablename__`` 属性。

    Args:
        class_node: AST 类定义节点。

    Returns:
        含 ``__tablename__`` 属性则为 True。
    """
    for stmt in class_node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "__tablename__":
                    return True
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id == "__tablename__":
                return True
    return False


def _extract_table_args_dict(class_node: ast.ClassDef) -> Optional[ast.Dict]:
    """从 ``__table_args__`` 中提取最外层的 ``ast.Dict`` 节点。

    支持两种常见写法：

    - ``__table_args__ = {"comment": "..."}``
    - ``__table_args__ = ({"comment": "..."}, Index(...))``

    Args:
        class_node: AST 类定义节点。

    Returns:
        提取到的 ``ast.Dict`` 节点，找不到则返回 ``None``。
    """
    for stmt in class_node.body:
        is_target_match = False
        if isinstance(stmt, ast.Assign):
            is_target_match = any(
                isinstance(target, ast.Name) and target.id == "__table_args__"
                for target in stmt.targets
            )
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            is_target_match = stmt.target.id == "__table_args__"
        if not is_target_match:
            continue

        rhs_node: ast.AST = stmt.value
        if isinstance(rhs_node, ast.Dict):
            return rhs_node
        if isinstance(rhs_node, (ast.Tuple, ast.List)) and rhs_node.elts:
            for element in rhs_node.elts:
                if isinstance(element, ast.Dict):
                    return element
    return None


def _table_args_has_comment(class_node: ast.ClassDef) -> bool:
    """检查 ``__table_args__`` 是否提供非空 ``comment`` 字符串。

    Args:
        class_node: AST 类定义节点。

    Returns:
        ``__table_args__["comment"]`` 为非空字符串则为 True。
    """
    args_dict_node = _extract_table_args_dict(class_node)
    if args_dict_node is None:
        return False
    for key_node, value_node in zip(args_dict_node.keys, args_dict_node.values):
        if (
            isinstance(key_node, ast.Constant)
            and key_node.value == "comment"
            and isinstance(value_node, ast.Constant)
            and isinstance(value_node.value, str)
            and value_node.value.strip()
        ):
            return True
    return False


def _iter_column_calls_in_class(class_node: ast.ClassDef):
    """迭代类体内所有 ``Column(...)`` / ``mapped_column(...)`` 调用。

    Args:
        class_node: AST 类定义节点。

    Yields:
        ``(call_node, line_number)`` 元组。
    """
    for sub_node in ast.walk(class_node):
        if isinstance(sub_node, ast.Call):
            func_name = _is_column_call(sub_node)
            if func_name is not None:
                yield sub_node, sub_node.lineno


# ==========================================
# 3. 核心检查逻辑
# ==========================================


def check_python_file(python_file: Path) -> list[ModelViolation]:
    """对单个模型文件执行 AST 静态检查。

    Args:
        python_file: 待检查的 Python 文件路径。

    Returns:
        该文件中所有违规记录列表。
    """
    raw_source_text: str = python_file.read_text(encoding="utf-8")
    try:
        parsed_tree: ast.Module = ast.parse(raw_source_text, filename=str(python_file))
    except SyntaxError as exc:
        return [
            ModelViolation(
                file_path=python_file,
                line_number=exc.lineno or 0,
                kind="syntax",
                target=python_file.name,
                reason=f"无法解析 Python 源文件：{exc.msg}",
            )
        ]

    file_violations: list[ModelViolation] = []
    for class_node in (
        node for node in ast.walk(parsed_tree) if isinstance(node, ast.ClassDef)
    ):
        # 列级检查：对所有 Column / mapped_column 调用都要求 comment=
        for call_node, line_number in _iter_column_calls_in_class(class_node):
            if not _call_has_comment_kwarg(call_node):
                file_violations.append(
                    ModelViolation(
                        file_path=python_file,
                        line_number=line_number,
                        kind="column",
                        target=class_node.name,
                        reason='Column / mapped_column 缺少 comment="..." 关键字参数。',
                    )
                )

        # 表级检查：只有含 __tablename__ 的类才算数据表
        if _class_has_tablename(class_node) and not _table_args_has_comment(class_node):
            file_violations.append(
                ModelViolation(
                    file_path=python_file,
                    line_number=class_node.lineno,
                    kind="table",
                    target=class_node.name,
                    reason=(
                        "模型类缺少表备注：请在 __table_args__ 中提供"
                        ' comment="..." 字段。'
                    ),
                )
            )

    return file_violations


def run_models_comment_check(models_dir: Path) -> CheckResult:
    """对整个 models 目录执行检查。

    Args:
        models_dir: 模型目录路径。

    Returns:
        检查汇总结果。
    """
    aggregated_result = CheckResult()
    if not models_dir.exists():
        return aggregated_result

    for python_file in sorted(models_dir.rglob("*.py")):
        aggregated_result.checked_files_count += 1
        file_violations = check_python_file(python_file)
        aggregated_result.violations.extend(file_violations)

    return aggregated_result


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
) -> list[ModelViolation]:
    """检查单个 Alembic 迁移文件的 revision ID 长度。

    Args:
        migration_file: 迁移文件路径。
        max_length: revision ID 最大允许长度（默认 32）。

    Returns:
        违规记录列表。
    """
    file_violations: list[ModelViolation] = []
    for revision_id in extract_revision_ids(migration_file):
        if len(revision_id) > max_length:
            file_violations.append(
                ModelViolation(
                    file_path=migration_file,
                    line_number=0,
                    kind="alembic_revision",
                    target=revision_id,
                    reason=(
                        f"revision ID 长度为 {len(revision_id)}，"
                        f"超过 alembic_version.version_num 默认上限 {max_length}。"
                    ),
                )
            )
    return file_violations


def run_alembic_revision_check(
    versions_dir: Path,
    max_length: int = 32,
) -> CheckResult:
    """对整个 Alembic versions 目录执行 revision ID 长度检查。

    Args:
        versions_dir: Alembic versions 目录路径。
        max_length: revision ID 最大允许长度（默认 32）。

    Returns:
        检查汇总结果。
    """
    aggregated_result = CheckResult()
    if not versions_dir.exists():
        return aggregated_result

    for migration_file in sorted(versions_dir.glob("*.py")):
        aggregated_result.checked_files_count += 1
        file_violations = check_alembic_migration_file(migration_file, max_length)
        aggregated_result.violations.extend(file_violations)

    return aggregated_result


# ==========================================
# 4. 输出与入口
# ==========================================


def _format_report(
    check_result: CheckResult,
    scope_name: str,
) -> str:
    """将检查结果格式化为可读报告。

    Args:
        check_result: 检查汇总结果。
        scope_name: 检查范围名称，用于报告标题。

    Returns:
        格式化后的报告字符串。
    """
    report_lines: list[str] = [
        f"\n{scope_name} — 共扫描 {check_result.checked_files_count} 个文件\n"
    ]

    if check_result.passed:
        report_lines.append("✅ 无违规。")
        return "\n".join(report_lines)

    report_lines.append(f"❌ 发现 {len(check_result.violations)} 处违规：\n")
    for violation in check_result.violations:
        location = (
            f"{violation.file_path.name}:{violation.line_number}"
            if violation.line_number
            else violation.file_path.name
        )
        report_lines.append(
            f"  [{violation.kind}] {location} ({violation.target})\n"
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


def _classify_file(file_path: Path, models_dir: Path, versions_dir: Path) -> str | None:
    """根据路径判断文件属于哪类检查对象。

    Args:
        file_path: 待分类文件路径。
        models_dir: 模型目录绝对路径。
        versions_dir: Alembic versions 目录绝对路径。

    Returns:
        ``"model"``、``"alembic"`` 或 ``None``（不属于任何检查范围）。
    """
    resolved_file = file_path.resolve()
    resolved_models_dir = models_dir.resolve()
    resolved_versions_dir = versions_dir.resolve()

    try:
        resolved_file.relative_to(resolved_models_dir)
        return "model"
    except ValueError:
        pass

    try:
        resolved_file.relative_to(resolved_versions_dir)
        return "alembic"
    except ValueError:
        pass

    return None


def main(argv: Optional[list[str]] = None) -> int:
    """主入口：扫描目录或检查指定文件，返回退出码。"""
    parser = argparse.ArgumentParser(
        description="检查数据库 schema 约定。",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=MODELS_DIR_RELPATH,
        help="模型目录路径（默认：src/backend/infrastructure/persistence/models）。",
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
        "files",
        nargs="*",
        help="待检查的模型或迁移文件路径；为空时扫描整个目录。",
    )
    args = parser.parse_args(argv)

    models_dir = _resolve_dir(args.models_dir)
    versions_dir = _resolve_dir(args.alembic_versions_dir)

    all_violations: list[ModelViolation] = []
    checked_files_count = 0

    if args.files:
        for file_str in args.files:
            file_path = Path(file_str).resolve()
            if not file_path.is_file():
                continue
            checked_files_count += 1
            kind = _classify_file(file_path, models_dir, versions_dir)
            if kind == "model":
                all_violations.extend(check_python_file(file_path))
            elif kind == "alembic":
                all_violations.extend(
                    check_alembic_migration_file(file_path, args.max_revision_length)
                )
    else:
        models_result = run_models_comment_check(models_dir)
        alembic_result = run_alembic_revision_check(
            versions_dir, args.max_revision_length
        )
        all_violations.extend(models_result.violations)
        all_violations.extend(alembic_result.violations)
        checked_files_count = (
            models_result.checked_files_count + alembic_result.checked_files_count
        )

    result = CheckResult(
        violations=all_violations, checked_files_count=checked_files_count
    )
    print(_format_report(result, "数据库 schema 约定检查"))

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
