"""Pure helpers for the GitHub CLI client.

Module-level functions that don't depend on the :class:`GitHubCliClient`
instance — body sanitisation, check rollup parsing, PR state normalisation
— live here so the client class can stay focused on ``gh`` invocation.

All helpers are re-exported from :mod:`backend.infrastructure.github_client`
for backward compatibility with existing import paths.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from backend.infrastructure.github_models import (
    PullRequestSummary,
    _BODY_TRUNCATION_MARKER,
    _MAX_GITHUB_BODY_LENGTH,
)

if TYPE_CHECKING:
    pass


# C0 control characters (except tab/newline/carriage-return) and DEL trip
# GitHub's request validation, which rejects the POST with a generic
# ``400 Bad Request`` ("Whoa there!") page. Agent CLI output forwarded into
# failure comments (e.g. ``claude --output-format stream-json --verbose``) can
# embed these raw bytes, so every body is scrubbed before it reaches ``gh``.
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Patterns matched against combined stdout/stderr of a failed ``gh`` call to
# decide whether the failure is likely transient and worth retrying.
_RETRYABLE_GH_ERROR_PATTERNS = (
    re.compile(r"TLS handshake timeout", re.IGNORECASE),
    re.compile(r"connection timed out", re.IGNORECASE),
    re.compile(r"i/o timeout", re.IGNORECASE),
    re.compile(r"no such host", re.IGNORECASE),
    re.compile(r"temporary failure in name resolution", re.IGNORECASE),
    re.compile(r"500\s+Internal Server Error", re.IGNORECASE),
    re.compile(r"502\s+Bad Gateway", re.IGNORECASE),
    re.compile(r"503\s+Service Unavailable", re.IGNORECASE),
    re.compile(r"504\s+Gateway Timeout", re.IGNORECASE),
    # HTTP 499：客户端/边缘代理提前断开连接，纯网络层瞬时故障，与请求内容无关。
    re.compile(r"non-200 OK status code:\s*499\b", re.IGNORECASE),
    # GitHub 边缘节点的 "Whoa there!" 通用 400 页面：可能是瞬时的滥用检测误判，
    # 也可能是 sanitize_github_body 没兜住的内容问题——重试只帮前者，但对后者
    # 也无害（重试耗尽后会落回原有 agent/failed + recover 提示路径）。
    # 只匹配这个具体 HTML 页面签名，不匹配普通结构化 400（如校验失败），避免
    # 掩盖真正的请求错误。
    re.compile(r"non-200 OK status code:\s*400.*Whoa there!", re.IGNORECASE | re.DOTALL),
)

#: Extracts the numeric comment ID from a GitHub issue comment URL.
_COMMENT_ID_URL_PATTERN = re.compile(r"/issues/(?:\d+)#issuecomment-(\d+)")

# Stable sort order for ``list_pull_requests_for_issue`` output. Open
# PRs first (incl. drafts), then closed, then merged last so the table
# always reads "in flight → historical".
_STATE_ORDER = {"open": 0, "draft": 1, "closed": 2, "merged": 3}

_CHECK_FAILURE_STATES = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "ERROR",
    "FAILURE",
    "FAILED",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}
_CHECK_PENDING_STATES = {
    "EXPECTED",
    "IN_PROGRESS",
    "PENDING",
    "QUEUED",
    "REQUESTED",
    "WAITING",
}
_CHECK_SUCCESS_STATES = {"NEUTRAL", "SKIPPED", "SUCCESS"}


def sanitize_github_body(body: str, *, max_length: int = _MAX_GITHUB_BODY_LENGTH) -> str:
    """Strip request-breaking control characters and bound the body length.

    GitHub returns a generic ``400 Bad Request`` page for POST bodies that
    contain raw control characters or exceed its size limit. Both can occur
    when agent command output is embedded in a failure comment, so callers
    route every Markdown body through this guard before handing it to ``gh``.

    Args:
        body: The raw Markdown body to post.
        max_length: Maximum characters to keep; longer bodies are
            middle-truncated with a marker so both the start and the tail of
            the original content survive.

    Returns:
        A sanitized body safe to send to the GitHub CLI.
    """
    cleaned_body = _CONTROL_CHARACTER_PATTERN.sub("", body)
    if len(cleaned_body) <= max_length:
        return cleaned_body
    keep_length = max(0, max_length - len(_BODY_TRUNCATION_MARKER))
    head_length = keep_length * 2 // 3
    tail_length = keep_length - head_length
    tail = cleaned_body[-tail_length:] if tail_length else ""
    return cleaned_body[:head_length] + _BODY_TRUNCATION_MARKER + tail


def _extract_comment_id_from_url(url: str) -> int | None:
    """Return the numeric comment ID from a GitHub comment URL."""
    match = _COMMENT_ID_URL_PATTERN.search(url)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _normalize_optional_state(raw_value: object) -> str | None:
    """Normalize GitHub CLI check state values for comparison."""
    if raw_value is None:
        return None
    normalized_value = str(raw_value).strip().upper()
    return normalized_value or None


def _normalize_mergeable(raw_mergeable: object) -> bool | None:
    """Normalize GitHub CLI mergeable enum output to the core boolean model."""
    if isinstance(raw_mergeable, bool):
        return raw_mergeable

    mergeable_state = _normalize_optional_state(raw_mergeable)
    if mergeable_state in ("MERGEABLE", "TRUE"):
        return True
    if mergeable_state in ("CONFLICTING", "FALSE"):
        return False
    return None


def _extract_rollup_entries(raw_rollup: object) -> list[dict[str, object]]:
    """Return status check entries from supported GitHub CLI rollup shapes."""
    if isinstance(raw_rollup, list):
        return [entry for entry in raw_rollup if isinstance(entry, dict)]
    if isinstance(raw_rollup, dict):
        for key in ("nodes", "checks", "contexts"):
            raw_entries = raw_rollup.get(key)
            if isinstance(raw_entries, list):
                return [entry for entry in raw_entries if isinstance(entry, dict)]
    return []


def _check_display_name(raw_check: dict[str, object]) -> str:
    """Return a compact human name for a check rollup entry."""
    for key in ("name", "context", "workflowName"):
        raw_name = raw_check.get(key)
        if raw_name:
            return str(raw_name)
    return str(raw_check.get("__typename", "check"))


def _check_summary_line(raw_check: dict[str, object]) -> str:
    """Build a readable summary for a failed or pending check."""
    state_parts = []
    for key in ("status", "conclusion", "state"):
        raw_state = raw_check.get(key)
        if raw_state:
            state_parts.append(f"{key}={raw_state}")

    check_url = raw_check.get("detailsUrl") or raw_check.get("targetUrl")
    suffix = f" ({', '.join(state_parts)})" if state_parts else ""
    if check_url:
        suffix = f"{suffix} {check_url}".rstrip()
    return f"{_check_display_name(raw_check)}{suffix}"


def _check_entry_state(raw_check: dict[str, object]) -> str:
    """Classify one status check rollup entry."""
    conclusion = _normalize_optional_state(raw_check.get("conclusion"))
    state = _normalize_optional_state(raw_check.get("state"))
    status = _normalize_optional_state(raw_check.get("status"))

    for candidate_state in (conclusion, state):
        if candidate_state in _CHECK_FAILURE_STATES:
            return "FAILURE"
        if candidate_state in _CHECK_PENDING_STATES:
            return "PENDING"
        if candidate_state in _CHECK_SUCCESS_STATES:
            return "SUCCESS"

    if status in _CHECK_FAILURE_STATES:
        return "FAILURE"
    if status in _CHECK_PENDING_STATES:
        return "PENDING"
    if status == "COMPLETED":
        return "PENDING" if conclusion is None else "SUCCESS"
    if status in _CHECK_SUCCESS_STATES:
        return "SUCCESS"
    return "PENDING"


def _normalise_pr_state(raw_state: object, *, is_draft: bool, merged_at: object) -> str:
    """Map GitHub's PR state and draft flag into one of four buckets."""
    if merged_at:
        return "merged"
    if is_draft:
        return "draft"
    state_text = str(raw_state or "").upper()
    if state_text == "MERGED":
        return "merged"
    if state_text == "CLOSED":
        return "closed"
    return "open"


def _parse_pr_summary(raw_pr: dict[str, object]) -> PullRequestSummary:
    """Convert a ``gh pr list --json`` row into a PullRequestSummary."""
    merged_at = raw_pr.get("mergedAt") or ""
    is_draft = bool(raw_pr.get("isDraft", False))
    return PullRequestSummary(
        number=int(raw_pr.get("number", 0)),
        state=_normalise_pr_state(raw_pr.get("state"), is_draft=is_draft, merged_at=merged_at),
        url=str(raw_pr.get("url", "")),
        is_draft=is_draft,
        merged=bool(merged_at),
        title=str(raw_pr.get("title", "")),
    )


def _aggregate_status_check_rollup(
    raw_rollup: object,
) -> tuple[str | None, tuple[str, ...]]:
    """Aggregate GitHub CLI statusCheckRollup entries into a stable state."""
    rollup_entries = _extract_rollup_entries(raw_rollup)
    if not rollup_entries:
        return None, ()

    pending_summaries: list[str] = []
    failure_summaries: list[str] = []
    for raw_check in rollup_entries:
        check_state = _check_entry_state(raw_check)
        if check_state == "FAILURE":
            failure_summaries.append(_check_summary_line(raw_check))
        elif check_state == "PENDING":
            pending_summaries.append(_check_summary_line(raw_check))

    if failure_summaries:
        return "FAILURE", tuple(failure_summaries)
    if pending_summaries:
        return "PENDING", tuple(pending_summaries)
    return "SUCCESS", ()


__all__ = [
    "_CHECK_FAILURE_STATES",
    "_RETRYABLE_GH_ERROR_PATTERNS",
    "_STATE_ORDER",
    "_aggregate_status_check_rollup",
    "_check_entry_state",
    "_extract_comment_id_from_url",
    "_extract_rollup_entries",
    "_normalize_mergeable",
    "_parse_pr_summary",
    "sanitize_github_body",
]
