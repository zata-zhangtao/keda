"""Tests for structured PRD Change Log parsing."""

from backend.core.shared.prd_change_log import (
    extract_prd_change_log_entry_count,
    parse_prd_change_log,
)


def test_parse_prd_change_log_accepts_complete_entry() -> None:
    """A complete structured entry should satisfy the parser."""
    prd_content = """# PRD

## Change Log

### 2026-07-14 · Agent proposal
- 类型：验证方案调整
- 原文：真实应用截图
- 变更后：真实 Playwright 截图
- 原因：使步骤可重复执行
- 影响：用户可见目标不变
- 审核：待独立 reviewer 确认

## Acceptance Checklist
- [ ] 真实验证
"""

    change_log_result = parse_prd_change_log(prd_content)

    assert change_log_result.is_complete
    assert change_log_result.entry_count == 1


def test_parse_prd_change_log_reports_missing_fields() -> None:
    """An entry missing governance fields must not look complete."""
    prd_content = """# PRD

## Change Log

### 2026-07-14 · Agent proposal
- 类型：范围调整
- 原文：原始范围

## Acceptance Checklist
- [ ] 真实验证
"""

    change_log_result = parse_prd_change_log(prd_content)

    assert not change_log_result.is_complete
    assert change_log_result.incomplete_entry_fields[1] == (
        "变更后",
        "原因",
        "影响",
        "审核",
    )


def test_extract_prd_change_log_entry_count_returns_zero_without_section() -> None:
    """没有 Change Log 的 PRD 不应被视为已有变更记录。"""
    assert extract_prd_change_log_entry_count("# PRD\n") == 0
