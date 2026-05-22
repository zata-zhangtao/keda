"""Shared pure helper for parsing PRD Acceptance Checklist state."""

from __future__ import annotations

import re
from dataclasses import dataclass


ACCEPTANCE_CHECKLIST_HEADING_RE = re.compile(
    r"^##\s+(?:\d+\.\s+)?(?:Acceptance Checklist\b.*|验收清单.*)\s*$"
)
TOP_LEVEL_HEADING_RE = re.compile(r"^##\s+")
CHECKBOX_RE = re.compile(r"^\s*[-*+]\s+\[(?P<mark>[ xX])\]\s*(?P<label>.*)$")
CODE_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


@dataclass(frozen=True)
class PrdChecklistResult:
    """Result of parsing a PRD's Acceptance Checklist section.

    Attributes:
        section_found: Whether an Acceptance Checklist section was located.
        unchecked_items: List of unchecked items as (1-based line number, line text).
    """

    section_found: bool
    unchecked_items: list[tuple[int, str]]

    @property
    def is_complete(self) -> bool:
        """Return True when the section exists and all items are checked."""
        return self.section_found and not self.unchecked_items


def parse_prd_checklist(file_content: str) -> PrdChecklistResult:
    """Parse a PRD markdown string and return its Acceptance Checklist state.

    Only checkboxes inside the Acceptance Checklist section are considered.
    Checkboxes inside fenced code blocks are ignored.  The section ends at
    the next top-level ``##`` heading or end of file.

    Args:
        file_content: Raw markdown content of the PRD file.

    Returns:
        PrdChecklistResult with section_found and unchecked_items.
    """
    lines = file_content.splitlines()

    start_index: int | None = None
    for line_index, line in enumerate(lines):
        if ACCEPTANCE_CHECKLIST_HEADING_RE.match(line):
            start_index = line_index
            break

    if start_index is None:
        return PrdChecklistResult(section_found=False, unchecked_items=[])

    end_index = len(lines)
    for line_index in range(start_index + 1, len(lines)):
        if TOP_LEVEL_HEADING_RE.match(lines[line_index]):
            end_index = line_index
            break

    unchecked_items: list[tuple[int, str]] = []
    in_code_block = False

    for line_index in range(start_index + 1, end_index):
        line = lines[line_index]
        if CODE_FENCE_RE.match(line):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        checkbox_match = CHECKBOX_RE.match(line)
        if checkbox_match and checkbox_match.group("mark") == " ":
            unchecked_items.append((line_index + 1, line.rstrip()))

    return PrdChecklistResult(section_found=True, unchecked_items=unchecked_items)
