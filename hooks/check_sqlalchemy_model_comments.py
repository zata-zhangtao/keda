#!/usr/bin/env python3
"""SQLAlchemy 模型表/列备注完整性检查。

扫描 ``src/backend/infrastructure/persistence/models/`` 下所有 ``.py`` 文件，校验：

- 含 ``__tablename__`` 属性的类（即数据表类）必须通过 ``__table_args__``
  提供非空 ``comment`` 字符串（表备注）。
- 每个 ``Column(...)`` 或 ``mapped_column(...)`` 调用都必须显式带
  ``comment="..."`` 关键字参数（字段备注）。

通过 pre-commit 在本地提交前强制执行，避免缺备注的模型被合入。
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


MODELS_DIR_RELPATH: Path = Path("src/backend/infrastructure/persistence/models")
"""被检查的模型目录相对项目根路径。"""


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
            first_element = rhs_node.elts[0]
            if isinstance(first_element, ast.Dict):
                return first_element
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


# ==========================================
# 4. 输出与入口
# ==========================================


def _format_report(check_result: CheckResult, models_dir: Path) -> str:
    """将检查结果格式化为可读报告。

    Args:
        check_result: 检查汇总结果。
        models_dir: 被扫描的模型目录。

    Returns:
        格式化后的报告字符串。
    """
    report_lines: list[str] = [
        f"\nSQLAlchemy 模型备注检查 — 共扫描 "
        f"{check_result.checked_files_count} 个文件 (目录: {models_dir})\n"
    ]

    if check_result.passed:
        report_lines.append("✅ 所有模型均带有表备注与列备注，无违规。")
        return "\n".join(report_lines)

    report_lines.append(f"❌ 发现 {len(check_result.violations)} 处违规：\n")
    for violation in check_result.violations:
        relative_path: str = violation.file_path.name
        report_lines.append(
            f"  [{violation.kind}] {relative_path}:{violation.line_number} "
            f"({violation.target})\n"
            f"    {violation.reason}\n"
        )
    report_lines.append("参考规范：docs/ai-standards/architecture.md (持久化层)")
    return "\n".join(report_lines)


def _resolve_models_dir(arg_value: Path) -> Path:
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


def main(argv: Optional[list[str]] = None) -> int:
    """主入口：扫描 models 目录，返回退出码。"""
    parser = argparse.ArgumentParser(
        description="检查 SQLAlchemy 模型是否带表备注与列备注。",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=MODELS_DIR_RELPATH,
        help="模型目录路径（默认：src/backend/infrastructure/persistence/models）。",
    )
    args = parser.parse_args(argv)

    models_dir = _resolve_models_dir(args.models_dir)
    final_result = run_models_comment_check(models_dir)
    report_text = _format_report(final_result, models_dir)
    print(report_text)

    return 0 if final_result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
