"""Issue comment event markers for agent runner audit trail."""

from __future__ import annotations

import re

from backend.core.shared.models.agent_runner import ReviewEventMarker


_EVENT_MARKER_PATTERN = re.compile(
    r"<!--\s*iar:event\s+"
    r"version=(?P<version>\d+)\s+"
    r"phase=(?P<phase>[\w_]+)\s+"
    r"cycle=(?P<cycle>\d+)"
    r"(?:\s+head=(?P<head>[a-f0-9]+))?"
    r"(?:\s+base=(?P<base>[a-f0-9]+))?"
    r"(?:\s+pr_branch=(?P<pr_branch>[^\s>]+))?"
    r"(?:\s+action=(?P<action>[\w_]+))?"
    r"(?:\s+checks_state=(?P<checks_state>[^\s>]+))?"
    r"(?:\s+mergeable=(?P<mergeable>true|false))?"
    r"(?:\s+issue_comments_count=(?P<issue_comments_count>\d+))?"
    r"(?:\s+pr_comments_count=(?P<pr_comments_count>\d+))?"
    r"(?:\s+blocked_paths=(?P<blocked_paths>[^\s>]+))?"
    r"\s*-->"
)


_REWORK_COMPLETION_PHASES = {
    "implementation_complete",
    "draft_pr_created",
    "publish_recovered",
    "rebase_repair_complete",
}

# Phase value written by Phase 0 ``process_deliberation_issues`` after it posts
# a structured clarifying-question list as an Issue comment. Paired with the
# ``cycle`` and ``issue_comments_count`` fields in the marker, it lets the
# daemon distinguish "AI has asked and is waiting for the user" from "user has
# replied and it's the AI's turn again".
DELIBERATION_QUESTION_PHASE = "deliberation_question_posted"


def _parse_event_marker(comment_body: str) -> ReviewEventMarker | None:
    """Parse the first iar:event marker from one Issue comment."""
    match = _EVENT_MARKER_PATTERN.search(comment_body)
    if not match:
        return None

    mergeable_raw = match.group("mergeable")
    mergeable = None
    if mergeable_raw == "true":
        mergeable = True
    elif mergeable_raw == "false":
        mergeable = False
    issue_comments_count_raw = match.group("issue_comments_count")
    pr_comments_count_raw = match.group("pr_comments_count")
    blocked_paths_raw = match.group("blocked_paths")
    blocked_paths: tuple[str, ...] = ()
    if blocked_paths_raw is not None:
        blocked_paths = tuple(path.strip() for path in blocked_paths_raw.split(",") if path.strip())
    return ReviewEventMarker(
        version=int(match.group("version")),
        phase=match.group("phase"),
        cycle=int(match.group("cycle")),
        head_sha=match.group("head"),
        base_sha=match.group("base"),
        pr_branch=match.group("pr_branch"),
        action=match.group("action"),
        checks_state=match.group("checks_state"),
        mergeable=mergeable,
        issue_comments_count=int(issue_comments_count_raw)
        if issue_comments_count_raw is not None
        else None,
        pr_comments_count=int(pr_comments_count_raw) if pr_comments_count_raw is not None else None,
        blocked_paths=blocked_paths,
    )


def parse_latest_event_marker(comments: list[str]) -> ReviewEventMarker | None:
    """Parse the latest iar:event marker from Issue comments."""
    for comment_body in reversed(comments):
        marker = _parse_event_marker(comment_body)
        if marker is not None:
            return marker
    return None


def parse_latest_event_marker_for_phases(
    comments: list[str],
    phases: set[str],
) -> ReviewEventMarker | None:
    """Parse the latest iar:event marker whose phase is in ``phases``.

    Used by gates that need the most recent event of a specific kind (for
    example deduplicating ``validation_passed`` audit comments) without
    being masked by unrelated later markers.
    """
    for comment_body in reversed(comments):
        marker = _parse_event_marker(comment_body)
        if marker is not None and marker.phase in phases:
            return marker
    return None


def parse_latest_pending_rework_marker(
    comments: list[str],
) -> ReviewEventMarker | None:
    """Parse the latest unconsumed post-PR rework marker from Issue comments.

    A rework request remains pending until a later lifecycle marker proves that
    the runner moved past it. Normal observer markers such as
    ``post_pr_supervisor`` must not hide a pending repair request.
    """
    rework_has_later_completion = False
    for comment_body in reversed(comments):
        marker = _parse_event_marker(comment_body)
        if marker is None:
            continue
        if marker.phase == "post_pr_rework_requested":
            if rework_has_later_completion:
                return None
            return marker
        if marker.phase in _REWORK_COMPLETION_PHASES:
            rework_has_later_completion = True
    return None


def format_event_marker(
    *,
    phase: str,
    cycle: int,
    head_sha: str | None = None,
    base_sha: str | None = None,
    pr_branch: str | None = None,
    action: str | None = None,
    checks_state: str | None = None,
    mergeable: bool | None = None,
    issue_comments_count: int | None = None,
    pr_comments_count: int | None = None,
    blocked_paths: tuple[str, ...] = (),
) -> str:
    """Format a hidden iar:event marker for Issue comments."""
    parts = [
        "version=1",
        f"phase={phase}",
        f"cycle={cycle}",
    ]
    if head_sha:
        parts.append(f"head={head_sha}")
    if base_sha:
        parts.append(f"base={base_sha}")
    if pr_branch:
        parts.append(f"pr_branch={pr_branch}")
    if action:
        parts.append(f"action={action}")
    if checks_state is not None:
        parts.append(f"checks_state={checks_state}")
    if mergeable is not None:
        parts.append(f"mergeable={'true' if mergeable else 'false'}")
    if issue_comments_count is not None:
        parts.append(f"issue_comments_count={issue_comments_count}")
    if pr_comments_count is not None:
        parts.append(f"pr_comments_count={pr_comments_count}")
    if blocked_paths:
        parts.append(f"blocked_paths={','.join(blocked_paths)}")
    return f"<!-- iar:event {' '.join(parts)} -->"
