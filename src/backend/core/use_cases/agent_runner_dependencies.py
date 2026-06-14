"""Issue dependency gate for agent runner.

本模块实现 Issue 依赖门禁的解析、判定与等待副作用：

- ``parse_dependency_marker``：解析 Issue body 中的 ``iar:depends-on`` marker。
- ``parse_delivery_dependencies``：解析 PRD 中工具无关的 ``Delivery Dependencies`` 小节。
- ``evaluate_dependencies``：查询 GitHub 实时状态，判定依赖是否满足。
- ``mark_dependency_waiting``：为未满足依赖的 Issue 添加 ``agent/waiting`` label
  并在阻塞原因变化时写去重 comment。
"""

from __future__ import annotations

import hashlib
import logging
import re

from backend.core.shared.interfaces.agent_runner import IGitHubClient
from backend.core.shared.models.agent_runner import (
    DependencyBlocker,
    DependencyDeclaration,
    DependencyVerdict,
    DeliveryDependencyDeclaration,
    IssueSummary,
    LabelConfig,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Marker regexes (same style as agent_runner_events.py)
# ---------------------------------------------------------------------------

_DEPENDS_ON_MARKER_PATTERN = re.compile(
    r"<!--\s*iar:depends-on\s+" r"(?P<body>[^>]+?)" r"\s*-->"
)

_DEPENDENCY_WAIT_MARKER_PATTERN = re.compile(
    r"<!--\s*iar:dependency-wait\s+" r"blockers=(?P<blockers>[^\s>]+)" r"\s*-->"
)

# ---------------------------------------------------------------------------
# PRD "Delivery Dependencies" section parsing
# ---------------------------------------------------------------------------

_DELIVERY_DEPENDENCIES_HEADER_RE = re.compile(
    r"^#{2,4}\s+(?:\d+\.\s+)?Delivery Dependencies\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_DELIVERY_FIELD_RE = re.compile(r"^-\s+(?P<key>[A-Za-z/\s]+?)\s*:\s*(?P<value>.*?)\s*$")
_DELIVERY_LIST_ITEM_RE = re.compile(r"^\s+-\s+(?P<value>.*?)\s*$")
_DELIVERY_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_DELIVERY_ISSUE_PREFIX_RE = re.compile(r"#?\d+(?=$|[\s,;，；()（）])")
_DELIVERY_ISSUE_TOKEN_RE = re.compile(r"#?\d+")
_DELIVERY_PRD_STEM_RE = re.compile(r"P\d+-[A-Za-z0-9_.-]+")
_DELIVERY_PRD_PATH_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.md)" r"(?=$|[\s,;，；()（）])"
)
_NONE_DEPENDENCY_VALUES = {"", "none", "n/a", "na", "-"}


def parse_delivery_dependencies(prd_text: str) -> DeliveryDependencyDeclaration:
    """Parse the structured ``Delivery Dependencies`` section from a PRD.

    Only structured fields are accepted; free-form prose is ignored.

    Args:
        prd_text: Full PRD Markdown text.

    Returns:
        Parsed delivery dependency declaration.
    """
    match = _DELIVERY_DEPENDENCIES_HEADER_RE.search(prd_text)
    if not match:
        return DeliveryDependencyDeclaration()

    section_start = match.end()
    # Section ends at the next header of same or higher level
    next_header_match = re.search(
        r"^#{1,4}\s+",
        prd_text[section_start:],
        re.MULTILINE,
    )
    if next_header_match:
        section_text = prd_text[
            section_start : section_start + next_header_match.start()
        ]
    else:
        section_text = prd_text[section_start:]

    field_values: dict[str, list[str]] = {
        "group": [],
        "depends_on_groups": [],
        "depends_on_issues": [],
        "gate_type": [],
        "notes": [],
    }
    current_key = ""

    for line in section_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        field_match = _DELIVERY_FIELD_RE.match(stripped)
        if field_match:
            raw_key = field_match.group("key").strip()
            key = _normalize_delivery_field_key(raw_key)
            current_key = key
            value = field_match.group("value").strip()
            if value:
                field_values[key].append(value)
            continue

        list_item_match = _DELIVERY_LIST_ITEM_RE.match(line)
        if list_item_match and current_key in (
            "depends_on_groups",
            "depends_on_issues",
            "notes",
        ):
            value = list_item_match.group("value").strip()
            if value:
                field_values[current_key].append(value)

    group = _parse_optional_scalar(field_values["group"])
    depends_on_groups = _parse_group_names(field_values["depends_on_groups"])
    depends_on_issues, depends_on_prds = _parse_issue_or_prd_refs(
        field_values["depends_on_issues"]
    )
    gate_type = _parse_optional_scalar(field_values["gate_type"]) or "none"
    notes = " ".join(field_values["notes"]).strip()

    normalized_gate = gate_type.lower()
    if normalized_gate and normalized_gate not in ("none", "soft", "hard"):
        raise ValueError(
            f"Invalid 'Gate type' in Delivery Dependencies: {gate_type!r}. "
            "Expected one of: none, soft, hard."
        )

    return DeliveryDependencyDeclaration(
        group=group,
        depends_on_groups=tuple(depends_on_groups),
        depends_on_issues=tuple(depends_on_issues),
        depends_on_prds=tuple(depends_on_prds),
        gate_type=normalized_gate or "none",
        notes=notes,
    )


def _normalize_delivery_field_key(raw_key: str) -> str:
    """Normalize a structured Delivery Dependencies field name."""
    key = raw_key.strip().lower().replace(" ", "_")
    if key == "group":
        return "group"
    if key in ("depends_on_groups", "depends_on_group"):
        return "depends_on_groups"
    if key in (
        "depends_on_tasks/issues",
        "depends_on_tasks",
        "depends_on_issues",
    ):
        return "depends_on_issues"
    if key in ("gate_type", "gate"):
        return "gate_type"
    if key in ("notes", "note"):
        return "notes"
    raise ValueError(
        f"Unknown field in Delivery Dependencies: {raw_key!r}. "
        "Expected one of: Group, Depends on groups, "
        "Depends on tasks/issues, Gate type, Notes."
    )


def _parse_optional_scalar(values: list[str]) -> str:
    """Return the first non-empty non-placeholder scalar value."""
    for value in values:
        item = value.strip()
        if item.lower() in _NONE_DEPENDENCY_VALUES:
            continue
        return item
    return ""


def _split_dependency_values(values: list[str]) -> list[str]:
    """Split comma/semicolon fields and Markdown list values."""
    items: list[str] = []
    for value in values:
        for item in re.split(r"[,;]", value):
            normalized_item = item.strip()
            if normalized_item.lower() in _NONE_DEPENDENCY_VALUES:
                continue
            items.append(normalized_item)
    return items


def _parse_group_names(values: list[str]) -> list[str]:
    """Parse dependency group names from scalar or Markdown list values."""
    return _split_dependency_values(values)


def _parse_issue_or_prd_refs(values: list[str]) -> tuple[list[int], list[str]]:
    """Extract issue numbers and PRD references from dependency values.

    Accepts ``#42`` or plain ``42`` as GitHub Issue references. Other non-empty
    values are preserved as PRD path/name references for Issue creation time,
    where they can be resolved with repository context and actionable errors.
    """
    numbers: list[int] = []
    prd_refs: list[str] = []
    for raw_dependency_value in values:
        for dependency_ref in _extract_dependency_reference_tokens(
            raw_dependency_value
        ):
            if re.fullmatch(r"#?\d+", dependency_ref):
                numbers.append(int(dependency_ref.lstrip("#")))
                continue
            prd_refs.append(dependency_ref)
    return numbers, prd_refs


def _extract_dependency_reference_tokens(raw_dependency_value: str) -> list[str]:
    """Extract structured dependency references from one field/list value."""
    stripped_dependency_value = raw_dependency_value.strip()
    if stripped_dependency_value.lower() in _NONE_DEPENDENCY_VALUES:
        return []

    code_span_refs = [
        match.group(1).strip()
        for match in _DELIVERY_CODE_SPAN_RE.finditer(stripped_dependency_value)
        if match.group(1).strip()
    ]
    dependency_code_span_refs = [
        code_span_ref
        for code_span_ref in code_span_refs
        if _looks_like_issue_or_prd_ref(code_span_ref)
    ]
    value_without_code_spans = _DELIVERY_CODE_SPAN_RE.sub(
        " ", stripped_dependency_value
    )

    structured_refs = list(dependency_code_span_refs)
    fallback_refs: list[str] = []
    for dependency_segment in re.split(r"[,;，；]", value_without_code_spans):
        stripped_segment = dependency_segment.strip().strip("`").strip()
        if stripped_segment.lower() in _NONE_DEPENDENCY_VALUES:
            continue

        issue_match = _DELIVERY_ISSUE_PREFIX_RE.match(stripped_segment)
        if issue_match:
            structured_refs.append(issue_match.group(0))
            continue

        prd_path_match = _DELIVERY_PRD_PATH_RE.search(stripped_segment)
        if prd_path_match:
            structured_refs.append(prd_path_match.group("path"))
            continue

        fallback_ref = re.split(r"\s+|[（(]", stripped_segment, maxsplit=1)[0]
        normalized_fallback_ref = fallback_ref.strip("`").strip()
        if normalized_fallback_ref.lower() not in _NONE_DEPENDENCY_VALUES:
            fallback_refs.append(normalized_fallback_ref)

    if structured_refs:
        return structured_refs
    if code_span_refs and not value_without_code_spans.strip():
        return code_span_refs
    return fallback_refs


def _looks_like_issue_or_prd_ref(candidate_ref: str) -> bool:
    """Return whether a code span looks like a dependency reference."""
    stripped_candidate_ref = candidate_ref.strip()
    if _DELIVERY_ISSUE_TOKEN_RE.fullmatch(stripped_candidate_ref):
        return True
    if _DELIVERY_PRD_PATH_RE.search(stripped_candidate_ref):
        return True
    if stripped_candidate_ref.startswith("tasks/"):
        return True
    return bool(_DELIVERY_PRD_STEM_RE.fullmatch(stripped_candidate_ref))


# ---------------------------------------------------------------------------
# Issue body marker parsing / formatting
# ---------------------------------------------------------------------------


def parse_dependency_marker(issue_body: str) -> DependencyDeclaration | None:
    """Parse ``iar:depends-on`` hidden markers from an Issue body.

    Args:
        issue_body: Full Issue body Markdown.

    Returns:
        Parsed dependency declaration, or ``None`` if no markers found.
    """
    issue_numbers: list[int] = []
    groups: list[str] = []
    for match in _DEPENDS_ON_MARKER_PATTERN.finditer(issue_body):
        body = match.group("body")
        # Issue references: #N
        for num_match in re.finditer(r"#(\d+)", body):
            issue_numbers.append(int(num_match.group(1)))
        # Group references: group:X
        for group_match in re.finditer(r"group:([^\s,;]+)", body):
            groups.append(group_match.group(1).strip())
    if not issue_numbers and not groups:
        return None
    return DependencyDeclaration(
        issue_numbers=tuple(sorted(set(issue_numbers))),
        groups=tuple(sorted(set(groups))),
    )


def format_dependency_marker(
    *,
    issue_numbers: tuple[int, ...] = (),
    groups: tuple[str, ...] = (),
) -> str:
    """Format a materialised ``iar:depends-on`` hidden marker.

    Args:
        issue_numbers: Upstream Issue numbers.
        groups: Upstream group names.

    Returns:
        Hidden HTML comment marker string.
    """
    parts: list[str] = []
    for number in issue_numbers:
        parts.append(f"#{number}")
    for group in groups:
        parts.append(f"group:{group}")
    if not parts:
        return ""
    return f"<!-- iar:depends-on {' '.join(parts)} -->"


def format_dependency_wait_marker(blockers: tuple[DependencyBlocker, ...]) -> str:
    """Format a hidden ``iar:dependency-wait`` marker for comment deduplication.

    Args:
        blockers: Current blocker list.

    Returns:
        Hidden HTML comment marker string.
    """
    canonical = _canonical_blockers_hash(blockers)
    return f"<!-- iar:dependency-wait blockers={canonical} -->"


def _canonical_blockers_hash(blockers: tuple[DependencyBlocker, ...]) -> str:
    """Return a short hash representing the blocker set."""
    text = "\n".join(f"{b.blocker_type}:{b.target}:{b.current_state}" for b in blockers)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parse_latest_dependency_wait_marker(comments: list[str]) -> str | None:
    """Parse the latest ``iar:dependency-wait`` marker from Issue comments.

    Args:
        comments: Comment body texts, oldest first.

    Returns:
        The blockers hash from the latest marker, or ``None``.
    """
    for comment_body in reversed(comments):
        match = _DEPENDENCY_WAIT_MARKER_PATTERN.search(comment_body)
        if match:
            return match.group("blockers")
    return None


# ---------------------------------------------------------------------------
# Dependency evaluation
# ---------------------------------------------------------------------------


def evaluate_dependencies(
    declaration: DependencyDeclaration,
    github_client: IGitHubClient,
    labels_config: LabelConfig,
) -> DependencyVerdict:
    """Evaluate whether all dependencies in ``declaration`` are satisfied.

    An Issue dependency is satisfied when the target Issue is closed.
    A group dependency is satisfied when all Issues with that group label are
    closed **and** the group has at least one member.

    Args:
        declaration: Materialised dependency declaration from Issue body.
        github_client: GitHub client for live queries.
        labels_config: Label configuration (for group prefix).

    Returns:
        Verdict including satisfaction flag and blocker details.
    """
    blockers: list[DependencyBlocker] = []
    has_failed_or_blocked = False
    empty_group_names: list[str] = []

    # Issue dependencies
    for issue_number in declaration.issue_numbers:
        try:
            upstream = github_client.get_issue(issue_number)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Failed to query Issue #%d: %s", issue_number, exc)
            blockers.append(
                DependencyBlocker(
                    blocker_type="issue",
                    target=str(issue_number),
                    current_state="unknown",
                )
            )
            continue
        state_upper = upstream.state.upper()
        if state_upper != "CLOSED":
            blockers.append(
                DependencyBlocker(
                    blocker_type="issue",
                    target=str(issue_number),
                    current_state=state_upper,
                )
            )
        if any(
            label in upstream.labels
            for label in (labels_config.failed, labels_config.blocked)
        ):
            has_failed_or_blocked = True

    # Group dependencies
    for group in declaration.groups:
        group_label = f"{labels_config.group_prefix}{group}"
        try:
            members = github_client.list_issues_by_label(
                group_label, limit=1000, state="all"
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Failed to query group %s: %s", group_label, exc)
            blockers.append(
                DependencyBlocker(
                    blocker_type="group",
                    target=group,
                    current_state="unknown",
                )
            )
            continue
        if not members:
            empty_group_names.append(group)
            blockers.append(
                DependencyBlocker(
                    blocker_type="group",
                    target=group,
                    current_state="empty",
                )
            )
            continue
        open_members = [m for m in members if m.state.upper() != "CLOSED"]
        if open_members:
            blockers.append(
                DependencyBlocker(
                    blocker_type="group",
                    target=group,
                    current_state=f"{len(open_members)} open",
                )
            )
        for member in members:
            if any(
                label in member.labels
                for label in (labels_config.failed, labels_config.blocked)
            ):
                has_failed_or_blocked = True

    return DependencyVerdict(
        satisfied=not blockers,
        blockers=tuple(blockers),
        has_failed_or_blocked_upstream=has_failed_or_blocked,
        empty_group_names=tuple(empty_group_names),
    )


# ---------------------------------------------------------------------------
# Waiting side-effects
# ---------------------------------------------------------------------------


def build_waiting_comment(
    verdict: DependencyVerdict,
    issue_number: int,
    labels_config: LabelConfig,
) -> str:
    """Build a Markdown comment explaining why an Issue is waiting.

    Args:
        verdict: Dependency evaluation result.
        issue_number: The waiting Issue number (for logging context).
        labels_config: Label configuration.

    Returns:
        Markdown comment body with embedded deduplication marker.
    """
    lines: list[str] = [
        "**Dependency Gate — Waiting**",
        "",
        "This Issue cannot be picked up because the following dependencies are not yet satisfied:",
        "",
    ]
    for blocker in verdict.blockers:
        if blocker.blocker_type == "issue":
            if blocker.current_state in ("unknown",):
                lines.append(f"- Issue #{blocker.target}: unable to determine state")
            else:
                state_emoji = (
                    "❌" if blocker.current_state.upper() != "CLOSED" else "✅"
                )
                lines.append(
                    f"- Issue #{blocker.target}: {state_emoji} {blocker.current_state}"
                )
        elif blocker.blocker_type == "group":
            if blocker.current_state == "empty":
                lines.append(
                    f"- Group ``{blocker.target}``: ⚠️ empty group "
                    f"(possible typo in ``{labels_config.group_prefix}{blocker.target}`` label)"
                )
            else:
                lines.append(
                    f"- Group ``{blocker.target}``: ❌ {blocker.current_state}"
                )

    if verdict.has_failed_or_blocked_upstream:
        lines.extend(
            [
                "",
                "⚠️ **Upstream failure detected**: one or more dependencies carries "
                f"``{labels_config.failed}`` or ``{labels_config.blocked}``. "
                "Operator intervention may be required.",
            ]
        )

    lines.append("")
    lines.append(format_dependency_wait_marker(verdict.blockers))
    return "\n".join(lines)


def mark_dependency_waiting(
    *,
    issue: IssueSummary,
    verdict: DependencyVerdict,
    github_client: IGitHubClient,
    labels_config: LabelConfig,
    dry_run: bool,
) -> None:
    """Ensure ``agent/waiting`` label exists and post a comment if blockers changed.

    Args:
        issue: The waiting Issue.
        verdict: Dependency evaluation result.
        github_client: GitHub client.
        labels_config: Label configuration.
        dry_run: If ``True``, only log; do not write to GitHub.
    """
    if dry_run:
        _logger.info(
            "DRY RUN: would mark Issue #%d as waiting (blockers: %s)",
            issue.number,
            ", ".join(f"{b.blocker_type}:{b.target}" for b in verdict.blockers),
        )
        return

    # Ensure waiting label is present
    if labels_config.waiting not in issue.labels:
        github_client.edit_issue_labels(issue.number, add=[labels_config.waiting])

    # Check whether we need a new comment
    comments = github_client.list_issue_comments(issue.number)
    latest_marker = parse_latest_dependency_wait_marker(comments)
    current_hash = _canonical_blockers_hash(verdict.blockers)
    if latest_marker == current_hash:
        _logger.debug(
            "Issue #%d blockers unchanged (%s), skipping comment.",
            issue.number,
            current_hash,
        )
        return

    comment_body = build_waiting_comment(verdict, issue.number, labels_config)
    github_client.comment_issue(issue.number, comment_body)
    _logger.info(
        "Posted dependency-wait comment on Issue #%d (blockers hash %s).",
        issue.number,
        current_hash,
    )


def clear_dependency_waiting(
    *,
    issue: IssueSummary,
    github_client: IGitHubClient,
    labels_config: LabelConfig,
    dry_run: bool,
) -> None:
    """Remove ``agent/waiting`` label if present.

    Args:
        issue: The Issue whose dependencies are now satisfied.
        github_client: GitHub client.
        labels_config: Label configuration.
        dry_run: If ``True``, only log; do not write to GitHub.
    """
    if labels_config.waiting not in issue.labels:
        return
    if dry_run:
        _logger.info(
            "DRY RUN: would remove waiting label from Issue #%d",
            issue.number,
        )
        return
    github_client.edit_issue_labels(issue.number, remove=[labels_config.waiting])
    _logger.info("Removed waiting label from Issue #%d.", issue.number)
