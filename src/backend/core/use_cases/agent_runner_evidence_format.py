"""Evidence format matching for Realistic Validation checklist items."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

IMAGE_EVIDENCE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

_EVIDENCE_ITEM_FILE_PATTERN = re.compile(r"^rv-(?P<item>\d+)[-.]", re.IGNORECASE)
_MAX_ITEM_SUMMARY_CHARS = 120
_EVIDENCE_FORMAT_MARKER_PATTERN = re.compile(
    r"<!--\s*iar:evidence-format\s+item=(?P<item>\d+)\s+kind=(?P<kind>[a-z]+)\s*-->",
    re.IGNORECASE,
)
_KNOWN_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {"screenshot", "pdf", "txt", "word", "excel", "csv", "video", "none"}
)


@dataclass(frozen=True)
class EvidenceKindRule:
    """A demanded evidence format with trigger keywords and accepted suffixes."""

    label: str
    requirement_pattern: re.Pattern[str]
    accepted_suffixes: frozenset[str]


_EVIDENCE_KIND_RULES: tuple[EvidenceKindRule, ...] = (
    EvidenceKindRule(
        label="screenshot/image",
        requirement_pattern=re.compile(
            r"截图|screen\s*shot|screenshot|\bpng\b|\bjpe?g\b", re.IGNORECASE
        ),
        accepted_suffixes=IMAGE_EVIDENCE_SUFFIXES,
    ),
    EvidenceKindRule(
        label="PDF",
        requirement_pattern=re.compile(r"\bpdf\b", re.IGNORECASE),
        accepted_suffixes=frozenset({".pdf"}),
    ),
    EvidenceKindRule(
        label="plain-text capture (.txt/.log)",
        requirement_pattern=re.compile(r"\btxt\b|\blog\b|日志", re.IGNORECASE),
        accepted_suffixes=frozenset({".txt", ".log"}),
    ),
    EvidenceKindRule(
        label="Word document",
        requirement_pattern=re.compile(r"\bdocx\b|\bword\s*文档|\bword\s+document", re.IGNORECASE),
        accepted_suffixes=frozenset({".doc", ".docx"}),
    ),
    EvidenceKindRule(
        label="Excel spreadsheet",
        requirement_pattern=re.compile(r"\bxlsx?\b|\bexcel\b", re.IGNORECASE),
        accepted_suffixes=frozenset({".xls", ".xlsx"}),
    ),
    EvidenceKindRule(
        label="CSV",
        requirement_pattern=re.compile(r"\bcsv\b", re.IGNORECASE),
        accepted_suffixes=frozenset({".csv"}),
    ),
    EvidenceKindRule(
        label="video/screen recording",
        requirement_pattern=re.compile(
            r"录屏|视频|\bvideo\b|\bmp4\b|\bscreen\s*recording", re.IGNORECASE
        ),
        accepted_suffixes=frozenset({".mp4", ".mov", ".webm", ".gif"}),
    ),
)


def extract_evidence_format_markers(issue_body: str) -> dict[int, str]:
    """Parse ``iar:evidence-format`` markers from an Issue body.

    Args:
        issue_body: Markdown body containing optional hidden markers.

    Returns:
        Mapping from checklist item number to demanded evidence kind. Unknown
        kinds are ignored so future extensions do not break current validation.
    """
    markers: dict[int, str] = {}
    for marker_match in _EVIDENCE_FORMAT_MARKER_PATTERN.finditer(issue_body):
        item_number = int(marker_match.group("item"))
        evidence_kind = marker_match.group("kind").lower()
        if evidence_kind in _KNOWN_EVIDENCE_KINDS and evidence_kind != "none":
            markers[item_number] = evidence_kind
    return markers


def _kind_to_evidence_rules(kind: str) -> list[EvidenceKindRule]:
    """Map a marker kind string to the corresponding evidence format rules."""
    for evidence_rule in _EVIDENCE_KIND_RULES:
        if kind in evidence_rule.label.lower():
            return [evidence_rule]
    kind_lower = kind.lower()
    for evidence_rule in _EVIDENCE_KIND_RULES:
        if kind_lower in evidence_rule.label.lower():
            return [evidence_rule]
    return []


def demanded_evidence_kinds(
    item_text: str,
    *,
    issue_body: str | None = None,
    item_number: int | None = None,
) -> list[EvidenceKindRule]:
    """Return the evidence formats a checklist item explicitly names.

    Args:
        item_text: Checklist item text.
        issue_body: Optional Issue body with ``iar:evidence-format`` markers.
        item_number: Optional checklist item number for marker lookup.

    Returns:
        Evidence format rules demanded by the marker or by item text keywords.
    """
    if issue_body is not None and item_number is not None:
        markers = extract_evidence_format_markers(issue_body)
        if item_number in markers:
            return _kind_to_evidence_rules(markers[item_number])
    return [
        evidence_rule
        for evidence_rule in _EVIDENCE_KIND_RULES
        if evidence_rule.requirement_pattern.search(item_text)
    ]


def _summarize_checklist_item(item_text: str) -> str:
    """Strip the checkbox prefix and truncate the item for error messages."""
    item_summary = re.sub(r"^[-*]\s*\[[ xX]\]\s*", "", item_text.strip())
    if len(item_summary) > _MAX_ITEM_SUMMARY_CHARS:
        item_summary = item_summary[:_MAX_ITEM_SUMMARY_CHARS] + "\u2026"
    return item_summary


def collect_evidence_coverage_problems(
    checklist_items: list[str],
    evidence_files: list[Path],
    issue_body: str | None = None,
) -> list[str]:
    """Validate that every checklist item has matching evidence files.

    Args:
        checklist_items: Realistic Validation checklist items.
        evidence_files: Files present in the validation evidence directory.
        issue_body: Optional Issue body with evidence format markers.

    Returns:
        Human-readable coverage problems. Empty means all items are covered.
    """
    files_by_item_number: dict[int, list[Path]] = {}
    for evidence_file in evidence_files:
        file_match = _EVIDENCE_ITEM_FILE_PATTERN.match(evidence_file.name)
        if file_match:
            files_by_item_number.setdefault(int(file_match.group("item")), []).append(evidence_file)

    coverage_problems: list[str] = []
    for item_number, item_text in enumerate(checklist_items, start=1):
        item_evidence_files = files_by_item_number.get(item_number, [])
        item_summary = _summarize_checklist_item(item_text)
        if not item_evidence_files:
            coverage_problems.append(
                f"Checklist item {item_number} has no evidence file named "
                f"`rv-{item_number}-<slug>.<ext>`: {item_summary}"
            )
            continue
        for demanded_kind in demanded_evidence_kinds(
            item_text, issue_body=issue_body, item_number=item_number
        ):
            if any(
                item_file.suffix.lower() in demanded_kind.accepted_suffixes
                for item_file in item_evidence_files
            ):
                continue
            accepted_suffixes_text = "/".join(sorted(demanded_kind.accepted_suffixes))
            coverage_problems.append(
                f"Checklist item {item_number} explicitly demands "
                f"{demanded_kind.label} evidence, but its `rv-{item_number}-*` "
                f"files contain no such file ({accepted_suffixes_text}): "
                f"{item_summary}"
            )
    return coverage_problems
