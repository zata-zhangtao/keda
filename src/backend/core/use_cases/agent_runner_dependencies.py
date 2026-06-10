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
    r"^#{2,4}\s+Delivery Dependencies\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_DELIVERY_FIELD_RE = re.compile(r"^-\s+(?P<key>[A-Za-z/\s]+?)\s*:\s*(?P<value>.*?)\s*$")


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

    group = ""
    depends_on_groups: list[str] = []
    depends_on_issues: list[int] = []
    gate_type = "none"
    notes = ""

    for line in section_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        field_match = _DELIVERY_FIELD_RE.match(stripped)
        if not field_match:
            continue
        key = field_match.group("key").strip().lower().replace(" ", "_")
        value = field_match.group("value").strip()

        if key in ("group",):
            group = value
        elif key in ("depends_on_groups", "depends_on_group"):
            depends_on_groups = [
                item.strip() for item in re.split(r"[,;]", value) if item.strip()
            ]
        elif key in (
            "depends_on_tasks/issues",
            "depends_on_tasks",
            "depends_on_issues",
        ):
            depends_on_issues = _parse_issue_numbers(value)
        elif key in ("gate_type", "gate"):
            gate_type = value.lower()
        elif key in ("notes", "note"):
            notes = value
        else:
            raw_key = field_match.group("key").strip()
            raise ValueError(
                f"Unknown field in Delivery Dependencies: {raw_key!r}. "
                "Expected one of: Group, Depends on groups, "
                "Depends on tasks/issues, Gate type, Notes."
            )

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
        gate_type=normalized_gate or "none",
        notes=notes,
    )


def _parse_issue_numbers(value: str) -> list[int]:
    """Extract issue numbers from a comma/semicolon-separated string.

    Accepts ``#42`` or plain ``42``. Raises ``ValueError`` for non-empty
    items that do not contain a digit, satisfying the fail-fast requirement.
    """
    numbers: list[int] = []
    for item in re.split(r"[,;]", value):
        item = item.strip()
        if not item:
            continue
        num_match = re.search(r"\d+", item)
        if num_match:
            numbers.append(int(num_match.group()))
        else:
            raise ValueError(
                f"Invalid issue reference in 'Depends on tasks/issues': {item!r}"
            )
    return numbers


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
