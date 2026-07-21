"""Realistic Validation review 阶段的 daemon 软门禁。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import AppConfig, IssueSummary, PullRequestContext
from backend.core.use_cases.agent_runner_events import (
    format_event_marker,
    parse_latest_event_marker,
    parse_latest_event_marker_for_phases,
)

_logger = logging.getLogger(__name__)


def build_validation_passed_comment(*, head_sha: str, pr_url: str) -> str:
    """构建人工完成验证签核后的审计评论。"""
    marker = format_event_marker(phase="validation_passed", cycle=1, head_sha=head_sha)
    return "\n".join(
        [
            marker,
            "",
            "## Realistic Validation Signed Off",
            "",
            f"- PR: {pr_url}",
            f"- Head SHA at sign-off: `{head_sha}`",
            "",
            "A human reviewer verified the validation evidence and ticked "
            "every Realistic Validation checklist item.",
        ]
    )


def build_validation_reset_comment(*, head_sha: str, evidence_head: str) -> str:
    """构建证据过期后重置签核的通知评论。"""
    marker = format_event_marker(phase="validation_reset", cycle=1, head_sha=head_sha)
    return "\n".join(
        [
            marker,
            "",
            "## Realistic Validation Sign-off Reset",
            "",
            f"- New commits were pushed after evidence was captured at "
            f"`{evidence_head}` (PR head is now `{head_sha}`).",
            "- The checklist has been unticked. Fresh evidence and a new "
            "human sign-off are required before merge.",
        ]
    )


def _ensure_issue_validation_labels(
    *, issue: IssueSummary, config: AppConfig, github_client: IGitHubClient, target_passed: bool
) -> None:
    """幂等收敛 Issue 的验证标签状态。"""
    pending_label = config.labels.validation_pending
    passed_label = config.labels.validation_passed
    desired_label = passed_label if target_passed else pending_label
    obsolete_label = pending_label if target_passed else passed_label
    if desired_label in issue.labels and obsolete_label not in issue.labels:
        return
    github_client.edit_issue_labels(issue.number, add=[desired_label], remove=[obsolete_label])


def _gate_single_issue(
    *, issue: IssueSummary, config: AppConfig, github_client: IGitHubClient
) -> None:
    """运行一个 ``agent/review`` Issue 的验证软门禁。"""
    from backend.core.use_cases.agent_runner_validation import (
        parse_latest_evidence_marker,
        parse_validation_checklist_state,
        reset_validation_checklist,
    )

    issue_comments = github_client.list_issue_comments(issue.number)
    lifecycle_marker = parse_latest_event_marker(issue_comments)
    pr_branch = lifecycle_marker.pr_branch if lifecycle_marker else None
    if not pr_branch:
        return
    pr_context: PullRequestContext | None = github_client.get_pull_request_context(pr_branch)
    if pr_context is None or pr_context.number is None:
        return
    checklist_state = parse_validation_checklist_state(pr_context.body)
    if checklist_state is None or checklist_state.total == 0:
        return
    if checklist_state.unchecked_count > 0:
        _ensure_issue_validation_labels(
            issue=issue, config=config, github_client=github_client, target_passed=False
        )
        return

    pr_comments = github_client.list_pr_comments(pr_context.number)
    evidence_marker = parse_latest_evidence_marker(pr_comments)
    if evidence_marker is not None and evidence_marker.head_sha != pr_context.head_sha:
        github_client.update_pull_request_body(
            pr_context.number, reset_validation_checklist(pr_context.body)
        )
        github_client.comment_pr(
            pr_context.number,
            build_validation_reset_comment(
                head_sha=pr_context.head_sha, evidence_head=evidence_marker.head_sha
            ),
        )
        _ensure_issue_validation_labels(
            issue=issue, config=config, github_client=github_client, target_passed=False
        )
        if config.labels.verifier_passed in issue.labels:
            github_client.edit_issue_labels(issue.number, remove=[config.labels.verifier_passed])
        return

    _ensure_issue_validation_labels(
        issue=issue, config=config, github_client=github_client, target_passed=True
    )
    audit_marker = parse_latest_event_marker_for_phases(
        issue_comments, {"validation_passed", "validation_reset"}
    )
    already_audited = (
        audit_marker is not None
        and audit_marker.phase == "validation_passed"
        and audit_marker.head_sha == pr_context.head_sha
    )
    if not already_audited:
        github_client.comment_issue(
            issue.number,
            build_validation_passed_comment(head_sha=pr_context.head_sha, pr_url=pr_context.pr_url),
        )


def cleanup_closed_issue_evidence_branches(
    *,
    repo_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> None:
    """删除已关闭 Issue 对应的远端证据分支。"""
    branch_prefix = config.validation.branch_prefix
    ls_remote_result = process_runner.run(
        ["git", "ls-remote", "--heads", config.git.remote, f"refs/heads/{branch_prefix}*"],
        cwd=repo_path,
        check=False,
    )
    if ls_remote_result.return_code != 0:
        return
    branch_issue_pattern = re.compile(rf"refs/heads/({re.escape(branch_prefix)}issue-(\d+))$")
    for ls_remote_line in ls_remote_result.stdout.splitlines():
        branch_match = branch_issue_pattern.search(ls_remote_line.strip())
        if not branch_match:
            continue
        branch_ref_name = branch_match.group(1)
        issue_number = int(branch_match.group(2))
        try:
            tracked_issue = github_client.get_issue(issue_number)
        except Exception as lookup_exc:  # noqa: BLE001 - 清理失败不应阻断轮询。
            _logger.info("Skipping evidence branch cleanup for #%d: %s", issue_number, lookup_exc)
            continue
        if tracked_issue.state.upper() != "CLOSED":
            continue
        process_runner.run(
            ["git", "push", config.git.remote, "--delete", branch_ref_name],
            cwd=repo_path,
            check=False,
        )
        _logger.info(
            "Deleted evidence branch %s for closed Issue #%d.", branch_ref_name, issue_number
        )


def process_validation_gate(
    *,
    repo_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    max_issues: int = 20,
) -> None:
    """跨 review 阶段 Issue 运行验证软门禁。"""
    if not config.validation.enabled:
        return
    review_issues = github_client.list_review_candidate_issues([config.labels.review], max_issues)
    for review_issue in review_issues:
        try:
            _gate_single_issue(issue=review_issue, config=config, github_client=github_client)
        except Exception as gate_exc:  # noqa: BLE001 - 单个 Issue 不应阻断其余轮询。
            _logger.error("Validation gate failed for Issue #%d: %s", review_issue.number, gate_exc)
    try:
        cleanup_closed_issue_evidence_branches(
            repo_path=repo_path,
            config=config,
            github_client=github_client,
            process_runner=process_runner,
        )
    except Exception as cleanup_exc:  # noqa: BLE001 - 清理失败不应阻断轮询。
        _logger.error("Evidence branch cleanup failed: %s", cleanup_exc)
