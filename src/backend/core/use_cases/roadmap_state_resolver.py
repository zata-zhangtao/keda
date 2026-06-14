"""Resolve live GitHub state for roadmap PRDs.

Maps Issue labels, PR state, and dependency blockers onto the unified
``RoadmapPrdState`` and computes the next actionable item for each PRD.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace

from backend.core.shared.interfaces.agent_runner import IGitHubClient
from backend.core.shared.models.agent_runner import AppConfig, LabelConfig
from backend.core.shared.models.roadmap import (
    RoadmapPrd,
    RoadmapPrdState,
)
from backend.core.use_cases.agent_runner_monitor import (
    _extract_pr_branch_from_issue,
    _lookup_pr_context,
    _resolve_primary_label,
)

_logger = logging.getLogger(__name__)


def _state_from_labels(
    labels: tuple[str, ...],
    labels_config: LabelConfig,
    issue_state: str,
    pr_merged: bool,
) -> RoadmapPrdState:
    """Map Issue labels and PR state to a roadmap state."""
    if issue_state.upper() == "CLOSED" and pr_merged:
        return RoadmapPrdState.MERGED
    primary = _resolve_primary_label(labels, labels_config)
    mapping = {
        labels_config.ready: RoadmapPrdState.READY,
        labels_config.running: RoadmapPrdState.RUNNING,
        labels_config.supervising: RoadmapPrdState.SUPERVISING,
        labels_config.review: RoadmapPrdState.REVIEW,
        labels_config.failed: RoadmapPrdState.FAILED,
        labels_config.blocked: RoadmapPrdState.BLOCKED,
        labels_config.waiting: RoadmapPrdState.WAITING,
    }
    return mapping.get(primary, RoadmapPrdState.NOT_STARTED)


def _is_pr_merged(
    issue_number: int,
    github_client: IGitHubClient,
    issue_body: str,
) -> tuple[bool, str | None]:
    """Return whether the associated PR has been merged and its URL."""
    try:
        comments = github_client.list_issue_comments(issue_number)
    except Exception as exc:  # noqa: BLE001
        _logger.info("Failed to list comments for issue #%s: %s", issue_number, exc)
        comments = []

    # Reuse the monitor helper to resolve the PR branch from event markers.
    from backend.core.shared.models.agent_runner import IssueSummary

    issue = IssueSummary(
        number=issue_number, title="", url="", body=issue_body, labels=()
    )
    pr_branch = _extract_pr_branch_from_issue(issue, github_client, comments)
    if pr_branch is None:
        return False, None

    try:
        merged_url = github_client.find_merged_pr_by_head(pr_branch)
    except Exception as exc:  # noqa: BLE001
        _logger.info("Failed to find merged PR for %s: %s", pr_branch, exc)
        merged_url = None
    return bool(merged_url), merged_url


def _compute_next_action(
    state: RoadmapPrdState,
    pr_context: object | None,
    issue_url: str | None,
) -> dict | None:
    """Build the operator-facing next-action hint for a PRD."""
    if state is RoadmapPrdState.REVIEW and pr_context is not None:
        pr_url = getattr(pr_context, "pr_url", None)
        if pr_url:
            return {"label": "去审阅 PR", "url": pr_url}
    if state is RoadmapPrdState.MERGED:
        return {"label": "开始下一个", "url": None}
    if state is RoadmapPrdState.NOT_STARTED:
        return {"label": "开始", "url": None}
    if state is RoadmapPrdState.FAILED:
        return {"label": "重试", "url": issue_url}
    if state is RoadmapPrdState.BLOCKED:
        return {"label": "继续", "url": issue_url}
    return None


def resolve_roadmap_states(
    prds: Sequence[RoadmapPrd],
    github_client: IGitHubClient,
    config: AppConfig,
    block_reasons: Mapping[str, str | None],
) -> list[RoadmapPrd]:
    """Resolve live GitHub state for a list of roadmap PRDs.

    Args:
        prds: PRDs from the scanner.
        github_client: GitHub client.
        config: Merged app config for the target repository.
        block_reasons: Dependency blocker map from :func:`evaluate_roadmap_dependencies`.

    Returns:
        New list of PRDs with ``state``, ``block_reason``, and ``next_action`` updated.
    """
    labels_config = config.labels
    resolved: list[RoadmapPrd] = []

    for prd in prds:
        if prd.status == "archived":
            resolved.append(
                replace(
                    prd,
                    state=RoadmapPrdState.ARCHIVED,
                    block_reason=None,
                    next_action=None,
                )
            )
            continue

        if prd.issue_number is None:
            block_reason = block_reasons.get(prd.prd_path)
            state = (
                RoadmapPrdState.UNRESOLVED_DEPENDENCY
                if block_reason and "无法解析" in block_reason
                else RoadmapPrdState.NOT_STARTED
            )
            resolved.append(
                replace(
                    prd,
                    state=state,
                    block_reason=block_reason,
                    next_action=_compute_next_action(state, None, prd.issue_url),
                )
            )
            continue

        try:
            issue = github_client.get_issue(prd.issue_number)
        except Exception as exc:  # noqa: BLE001
            _logger.info("Failed to fetch issue #%s: %s", prd.issue_number, exc)
            block_reason = block_reasons.get(prd.prd_path)
            resolved.append(
                replace(
                    prd,
                    state=RoadmapPrdState.NOT_STARTED,
                    block_reason=block_reason or f"无法获取 Issue #{prd.issue_number}",
                    next_action=None,
                )
            )
            continue

        pr_merged, merged_url = _is_pr_merged(
            prd.issue_number, github_client, issue.body
        )
        pr_context = _lookup_pr_context(issue, github_client)
        state = _state_from_labels(issue.labels, labels_config, issue.state, pr_merged)

        # Override with dependency blocker if present, unless already merged/archived.
        block_reason = block_reasons.get(prd.prd_path)
        if block_reason and state not in {
            RoadmapPrdState.MERGED,
            RoadmapPrdState.ARCHIVED,
        }:
            state = RoadmapPrdState.WAITING

        # Use merged URL as PR context URL for the review/merged action.
        if pr_merged and merged_url and pr_context is not None:
            pr_context = replace_pr_context_url(pr_context, merged_url)

        resolved.append(
            replace(
                prd,
                state=state,
                block_reason=block_reason,
                next_action=_compute_next_action(state, pr_context, issue.url),
            )
        )

    # Second pass: highlight downstream PRDs whose upstream just merged.
    final: list[RoadmapPrd] = []
    for prd in resolved:
        next_action = prd.next_action
        if prd.state is RoadmapPrdState.WAITING and not prd.block_reason:
            # All upstream dependencies cleared but not yet started.
            next_action = {"label": "可开始", "url": None}
        final.append(replace(prd, next_action=next_action))
    return final


def replace_pr_context_url(pr_context: object, url: str) -> object:
    """Return a new PR context with the given URL.

    This helper avoids importing ``PullRequestContext`` directly into the
    function body while still allowing URL override.
    """
    from backend.core.shared.models.agent_runner import PullRequestContext

    if isinstance(pr_context, PullRequestContext):
        return PullRequestContext(
            pr_url=url,
            branch=pr_context.branch,
            head_sha=pr_context.head_sha,
            base_sha=pr_context.base_sha,
            mergeable=pr_context.mergeable,
            checks_state=pr_context.checks_state,
            checks_summary=pr_context.checks_summary,
            number=pr_context.number,
            body=pr_context.body,
        )
    return pr_context
