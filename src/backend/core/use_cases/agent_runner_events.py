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
    r"\s*-->"
)


def parse_latest_event_marker(comments: list[str]) -> ReviewEventMarker | None:
    """Parse the latest iar:event marker from Issue comments."""
    for comment_body in reversed(comments):
        match = _EVENT_MARKER_PATTERN.search(comment_body)
        if match:
            mergeable_raw = match.group("mergeable")
            mergeable = None
            if mergeable_raw == "true":
                mergeable = True
            elif mergeable_raw == "false":
                mergeable = False
            issue_comments_count_raw = match.group("issue_comments_count")
            pr_comments_count_raw = match.group("pr_comments_count")
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
                pr_comments_count=int(pr_comments_count_raw)
                if pr_comments_count_raw is not None
                else None,
            )
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
    return f"<!-- iar:event {' '.join(parts)} -->"
