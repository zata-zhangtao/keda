"""Dataclass models and shared constants for the GitHub CLI client.

This module hosts the small frozen dataclasses used as return types from
:mod:`backend.infrastructure.github_client` plus the constants that
``sanitize_github_body`` depends on. Splitting these out keeps the
client implementation focused on ``gh`` invocation logic while letting
callers and tests type-annotate against stable data shapes.

The dataclasses are re-exported from :mod:`backend.infrastructure.github_client`
for backward compatibility with existing import paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# GitHub rejects POST bodies above ~65,536 characters; stay well below it.
_MAX_GITHUB_BODY_LENGTH = 60000

# Marker inserted when a Markdown body is middle-truncated so both the start
# and the tail of the original content survive.
_BODY_TRUNCATION_MARKER = "\n\n... (truncated to fit GitHub's size limit) ...\n\n"


@dataclass(frozen=True)
class IssueSummary:
    """GitHub Issue selected for runner execution."""

    number: int
    title: str
    url: str
    body: str
    labels: tuple[str, ...]
    state: str = "OPEN"


@dataclass(frozen=True)
class PullRequestSummary:
    """Local mirror of :class:`backend.core.shared.models.agent_runner.PullRequestSummary`."""

    number: int
    state: str
    url: str
    is_draft: bool
    merged: bool
    title: str


@dataclass(frozen=True)
class LabelConfig:
    """GitHub labels used as runner queue state."""

    ready: str = "agent/ready"
    running: str = "agent/running"
    supervising: str = "agent/supervising"
    review: str = "agent/review"
    failed: str = "agent/failed"
    blocked: str = "agent/blocked"
    waiting: str = "agent/waiting"
    validation_pending: str = "validation/pending"
    validation_passed: str = "validation/passed"
    group_prefix: str = "task-group/"
    rework_prd: str = "agent/rework-prd"
    deliberate: str = "agent/deliberate"
    agent_labels: dict[str, str] = field(
        default_factory=lambda: {
            "codex": "agent/codex",
            "claude": "agent/claude",
            "kimi": "agent/kimi",
        }
    )


@dataclass(frozen=True)
class PullRequestContext:
    """PR context returned by GitHub CLI."""

    pr_url: str
    branch: str
    head_sha: str
    base_sha: str
    mergeable: bool | None = None
    checks_state: str | None = None
    checks_summary: tuple[str, ...] = ()
    number: int | None = None
    body: str = ""


@dataclass(frozen=True)
class GhAuthStatus:
    """GitHub CLI authentication status."""

    authenticated: bool
    account: str | None = None
    failure_reason: str | None = None


__all__ = [
    "GhAuthStatus",
    "IssueSummary",
    "LabelConfig",
    "PullRequestContext",
    "PullRequestSummary",
    "_BODY_TRUNCATION_MARKER",
    "_MAX_GITHUB_BODY_LENGTH",
]
