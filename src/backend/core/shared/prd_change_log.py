"""解析并校验 PRD 中与验收状态分离的结构化变更记录。"""

from __future__ import annotations

import re
from dataclasses import dataclass


CHANGE_LOG_HEADING_RE = re.compile(
    r"^##\s+(?:\d+\.\s+)?(?:Change Log\b.*|变更记录.*)\s*$",
    re.IGNORECASE,
)
TOP_LEVEL_HEADING_RE = re.compile(r"^##\s+")
CHANGE_ENTRY_HEADING_RE = re.compile(r"^###\s+.+")
REQUIRED_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "类型": re.compile(r"^\s*[-*+]\s+(?:类型|Type)\s*[:：]", re.IGNORECASE),
    "原文": re.compile(r"^\s*[-*+]\s+(?:原文|Before)\s*[:：]", re.IGNORECASE),
    "变更后": re.compile(r"^\s*[-*+]\s+(?:变更后|After)\s*[:：]", re.IGNORECASE),
    "原因": re.compile(r"^\s*[-*+]\s+(?:原因|Reason)\s*[:：]", re.IGNORECASE),
    "影响": re.compile(r"^\s*[-*+]\s+(?:影响|Impact)\s*[:：]", re.IGNORECASE),
    "审核": re.compile(r"^\s*[-*+]\s+(?:审核|Review)\s*[:：]", re.IGNORECASE),
}


@dataclass(frozen=True)
class PrdChangeLogResult:
    """PRD Change Log 的解析结果。

    Attributes:
        section_found: 是否存在顶级 Change Log/变更记录章节。
        entry_count: 章节中记录到的变更条数。
        incomplete_entry_fields: 每条不完整记录缺少的字段，键为 1-based 条目序号。
    """

    section_found: bool
    entry_count: int
    incomplete_entry_fields: dict[int, tuple[str, ...]]

    @property
    def is_complete(self) -> bool:
        """返回 Change Log 是否包含至少一条完整记录。"""
        return self.section_found and self.entry_count > 0 and not self.incomplete_entry_fields


def parse_prd_change_log(file_content: str) -> PrdChangeLogResult:
    """解析 PRD 的结构化 Change Log。

    Change Log 与 Acceptance Checklist 有不同职责：前者记录需求本身为何
    演进，后者只记录已经完成的验收项。每条 Change Log 必须包含类型、原文、
    变更后、原因、影响和审核字段。

    Args:
        file_content: PRD Markdown 原文。

    Returns:
        Change Log 的章节、条目数和字段完整性信息。
    """
    prd_lines = file_content.splitlines()
    section_start_index = next(
        (
            line_index
            for line_index, line in enumerate(prd_lines)
            if CHANGE_LOG_HEADING_RE.match(line)
        ),
        None,
    )
    if section_start_index is None:
        return PrdChangeLogResult(False, 0, {})

    section_end_index = next(
        (
            line_index
            for line_index in range(section_start_index + 1, len(prd_lines))
            if TOP_LEVEL_HEADING_RE.match(prd_lines[line_index])
        ),
        len(prd_lines),
    )
    change_entries: list[list[str]] = []
    active_entry_lines: list[str] | None = None
    for line in prd_lines[section_start_index + 1 : section_end_index]:
        if CHANGE_ENTRY_HEADING_RE.match(line):
            active_entry_lines = []
            change_entries.append(active_entry_lines)
            continue
        if active_entry_lines is not None:
            active_entry_lines.append(line)

    incomplete_entry_fields: dict[int, tuple[str, ...]] = {}
    for entry_number, entry_lines in enumerate(change_entries, start=1):
        missing_fields = tuple(
            field_name
            for field_name, field_pattern in REQUIRED_FIELD_PATTERNS.items()
            if not any(field_pattern.match(entry_line) for entry_line in entry_lines)
        )
        if missing_fields:
            incomplete_entry_fields[entry_number] = missing_fields
    return PrdChangeLogResult(
        section_found=True,
        entry_count=len(change_entries),
        incomplete_entry_fields=incomplete_entry_fields,
    )


def extract_prd_change_log_entry_count(file_content: str) -> int:
    """返回 PRD Change Log 的条目数量，供 runner 检查本轮是否追加记录。

    Args:
        file_content: PRD Markdown 原文。

    Returns:
        ``## Change Log`` 中的三级标题条目数；没有章节时返回 0。
    """
    return parse_prd_change_log(file_content).entry_count
