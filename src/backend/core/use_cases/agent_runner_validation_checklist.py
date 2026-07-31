"""PR body 里的 Realistic Validation 勾选清单区块。

从 :mod:`agent_runner_validation` 拆出的第三块（"gate" 的输入侧）：构造、定位、
读取与重置 PR 描述中的勾选清单。软门禁本身（label 维护、head 漂移重置）在
:mod:`agent_runner_validation_gate`,它消费这里解析出的
:class:`ValidationChecklistState`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CHECKLIST_START_PATTERN = re.compile(
    r"<!--\s*iar:realistic-validation\s+version=(?P<version>\d+)\s+total=(?P<total>\d+)\s*-->"
)
_CHECKLIST_END_MARKER = "<!-- iar:realistic-validation-end -->"
_CHECKED_ITEM_PATTERN = re.compile(r"^\s*[-*] \[[xX]\] ")
_UNCHECKED_ITEM_PATTERN = re.compile(r"^\s*[-*] \[ \] ")


@dataclass(frozen=True)
class ValidationChecklistState:
    """Parsed state of the PR body Realistic Validation checklist."""

    total: int
    checked_count: int
    unchecked_count: int


# ---------------------------------------------------------------------------
# PR body 勾选清单区块
# ---------------------------------------------------------------------------


def build_validation_checklist_block(checklist_items: list[str]) -> str:
    """Build the marker-wrapped human sign-off checklist for a PR body."""
    return "\n".join(
        [
            f"<!-- iar:realistic-validation version=1 total={len(checklist_items)} -->",
            "## Realistic Validation (human sign-off required)",
            "",
            "Review the evidence comment on this PR, then tick each item "
            "once you verified it against the evidence:",
            "",
            *checklist_items,
            "",
            _CHECKLIST_END_MARKER,
        ]
    )


def _find_checklist_block(pr_body: str) -> tuple[int, int, int] | None:
    """Locate the checklist block. Returns (start, end, declared_total)."""
    start_match = _CHECKLIST_START_PATTERN.search(pr_body)
    if not start_match:
        return None
    end_index = pr_body.find(_CHECKLIST_END_MARKER, start_match.end())
    if end_index == -1:
        end_index = len(pr_body)
    return start_match.start(), end_index, int(start_match.group("total"))


def parse_validation_checklist_state(pr_body: str) -> ValidationChecklistState | None:
    """Parse checkbox state inside the marker-wrapped PR body block."""
    block_location = _find_checklist_block(pr_body)
    if block_location is None:
        return None
    block_start, block_end, declared_total = block_location
    block_text = pr_body[block_start:block_end]
    checked_count = 0
    unchecked_count = 0
    for block_line in block_text.splitlines():
        if _CHECKED_ITEM_PATTERN.match(block_line):
            checked_count += 1
        elif _UNCHECKED_ITEM_PATTERN.match(block_line):
            unchecked_count += 1
    return ValidationChecklistState(
        total=max(declared_total, checked_count + unchecked_count),
        checked_count=checked_count,
        unchecked_count=unchecked_count,
    )


def reset_validation_checklist(pr_body: str) -> str:
    """Return the PR body with all block checkboxes reset to unchecked."""
    block_location = _find_checklist_block(pr_body)
    if block_location is None:
        return pr_body
    block_start, block_end, _declared_total = block_location
    block_text = pr_body[block_start:block_end]
    reset_lines = [
        re.sub(r"^(\s*[-*] )\[[xX]\] ", r"\1[ ] ", block_line)
        for block_line in block_text.splitlines()
    ]
    return pr_body[:block_start] + "\n".join(reset_lines) + pr_body[block_end:]
