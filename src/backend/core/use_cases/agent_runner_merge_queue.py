"""Autopilot merge queue: serial squash-merge of supervisor-approved PRs.

快速档（autopilot）下的"合并队列" use case。该 use case 在每个 review pass
末尾被 :func:`backend.core.use_cases.review_once.review_once` 触发，按
Issue 号 FIFO **串行**处理所有带 ``agent/review`` 标签的开放 Issue，每条
走 7 步门禁链：verifier 门禁 → 自动签核 → rebase → 全量验证 → 禁改终扫 →
checks 全绿 → squash 合并。任一步失败则该 PR 转入既有失败/修复路径并
``continue`` 下一条，不阻塞其余 PR。

双开关语义：本文件被调用前由 :func:`review_once` 检查 ``autopilot.enabled``
*AND* ``safety.auto_merge`` 同时为真，任一为假则本段 no-op。

崩溃重入幂等性：
- 自动签核前先解析 :class:`ValidationChecklistState`，已全勾则不再改 body
  也不发评论。
- 发评论前先扫描当前 Issue 评论找 ``iar:auto-sign-off`` marker，已存在则
  不重复发。
- 合并成功后 ``gh pr merge --squash`` 对已合并 PR 视为幂等成功（由
  :class:`backend.infrastructure.github_client.GitHubCliClient` 把
  ``Already merged`` 归一为 no-op）。

队列状态完全复用 GitHub 标签与 PR 状态——无新存储，崩溃重入靠"labels +
PR 状态 + marker 查重"即可。
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    PullRequestContext,
)
from backend.core.use_cases.agent_runner_events import (
    format_event_marker,
    parse_latest_event_marker,
)
from backend.core.use_cases.agent_runner_git import (
    list_changed_paths,
    run_verification,
)
from backend.core.use_cases.agent_runner_publish import is_forbidden_path
from backend.core.use_cases.agent_runner_validation import validation_required
from backend.core.use_cases.agent_runner_workflow import transition_issue_workflow_state
from backend.core.use_cases.pr_supervisor import execute_rebase
from backend.core.use_cases.run_agent_once import create_or_reuse_worktree

_logger = logging.getLogger(__name__)

# Auto-sign-off marker prefix used to dedupe comments on daemon crash re-entry.
_AUTO_SIGN_OFF_MARKER = "<!-- iar:auto-sign-off"
# Event phase used by the merge-queue success audit comment.
_MERGED_EVENT_PHASE = "auto_merged"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _autopilot_enabled(config: AppConfig) -> bool:
    """Return True when the dual kill switch is fully armed.

    Requires both ``autopilot.enabled`` *and* ``safety.auto_merge`` to be
    true. This is the only kill-switch check in the merge queue pipeline;
    :func:`review_once` calls this before invoking :func:`process_merge_queue`.
    """
    return bool(config.autopilot.enabled and config.safety.auto_merge)


def _extract_pr_branch_from_comments(comments: list[str]) -> str | None:
    """Extract the latest known PR branch from Issue comments.

    Mirrors :func:`backend.core.use_cases.review_once._extract_pr_branch_from_comments`
    so the merge queue and the supervisor cycle agree on which branches are
    recognized — both parse ``iar:event`` markers first, then fall back to the
    ``PR Branch: \\`…\\``` / ``Branch: \\`…\\``` plaintext patterns.
    """
    for comment_body in reversed(comments):
        marker = parse_latest_event_marker([comment_body])
        if marker is not None and marker.pr_branch:
            return marker.pr_branch

        branch_patterns = (
            r"PR Branch:\s*`([^`]+)`",
            r"Branch:\s*`([^`]+)`",
        )
        for branch_pattern in branch_patterns:
            branch_match = re.search(branch_pattern, comment_body)
            if branch_match:
                return branch_match.group(1)
    return None


def _tick_sign_off_checklist(pr_body: str) -> str | None:
    """Return a new body where every line in the checklist block is ticked.

    Returns ``None`` when the body has no checklist block, or when the block
    is already fully ticked (i.e. nothing to do). Returning the existing body
    *unchanged* in the "already ticked" case is what makes the operation
    idempotent on crash re-entry.
    """
    # The block is delimited by two markers; ``re.DOTALL`` lets ``.*?`` span
    # lines. The captured middle group is what we tick in place.
    block_pattern = re.compile(
        r"(<!--\s*iar:realistic-validation\s+version=\d+\s+total=\d+\s*-->)(.*?)(<!--\s*iar:realistic-validation-end\s*-->)",
        re.DOTALL,
    )
    match = block_pattern.search(pr_body)
    if not match:
        return None

    block_text = match.group(2)
    if not any(re.match(r"^\s*[-*]\s*\[[\s]+\]", line) for line in block_text.splitlines()):
        return None

    ticked_lines = [
        re.sub(r"^(\s*[-*])\s*\[[\s]+\]\s", r"\1 [x] ", line) for line in block_text.splitlines()
    ]
    # Preserve the original body's whitespace shape around the block by
    # re-attaching any leading/trailing newlines that ``.*?`` swallowed.
    ticked_block = "\n".join(ticked_lines)
    if block_text.startswith("\n"):
        ticked_block = "\n" + ticked_block
    if block_text.endswith("\n"):
        ticked_block = ticked_block + "\n"
    return pr_body[: match.start(2)] + ticked_block + pr_body[match.end(2) :]


def _has_auto_sign_off_marker(comments: list[str]) -> bool:
    """Return True when an existing ``iar:auto-sign-off`` marker is present.

    Used by the merge queue to dedupe its own audit comments on daemon crash
    re-entry so the same Issue doesn't accumulate duplicate "I ticked the
    sign-off" comments across restarts.
    """
    return any(_AUTO_SIGN_OFF_MARKER in comment for comment in comments)


def _diff_paths(
    pr_context: PullRequestContext,
    config: AppConfig,
    process_runner: IProcessRunner,
    worktree_path: Path,
) -> list[str]:
    """Return the list of files changed on the PR branch.

    Falls back to ``git diff --name-only base...head`` from the worktree so
    path matching does not depend on a specific PR provider field.
    """
    try:
        result = process_runner.run(
            [
                "git",
                "diff",
                "--name-only",
                f"{config.git.remote}/{config.git.base_branch}...{pr_context.head_sha}",
            ],
            cwd=worktree_path,
            check=False,
        )
    except subprocess.CalledProcessError as exc:  # noqa: BLE001
        _logger.warning(
            "git diff failed for PR head %s; falling back to listing worktree changes: %s",
            pr_context.head_sha,
            exc,
        )
        result = None
    paths: list[str] = []
    if result is not None and result.return_code == 0:
        paths.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
    if not paths:
        # Fallback to local working tree changes when diff fails or returns empty.
        paths = list_changed_paths(worktree_path, process_runner)
    return paths


def _wait_for_checks_green(
    pr_context: PullRequestContext,
    config: AppConfig,
    github_client: IGitHubClient,
    *,
    timeout_seconds: int,
    poll_interval_seconds: int = 10,
) -> tuple[bool, PullRequestContext | None]:
    """Poll ``get_pull_request_context`` until checks_state == ``SUCCESS``.

    Args:
        pr_context: Initial PR context (used for branch-based lookup).
        config: App config.
        github_client: GitHub client.
        timeout_seconds: Maximum seconds to wait.
        poll_interval_seconds: Sleep between polls (default 10s).

    Returns:
        Tuple ``(is_green, latest_context)``. ``is_green`` is ``True`` only
        when the latest checks_state is ``SUCCESS``; on ``FAILURE`` or
        timeout the function returns ``False`` with the last known context
        (or ``None`` if even the initial lookup failed).
    """
    deadline = time.monotonic() + max(0, timeout_seconds)
    latest: PullRequestContext | None = pr_context
    while True:
        if latest is not None:
            if latest.checks_state == "SUCCESS":
                return True, latest
            if latest.checks_state == "FAILURE":
                return False, latest
        if time.monotonic() >= deadline:
            return False, latest
        time.sleep(max(1, poll_interval_seconds))
        refreshed = github_client.get_pull_request_context(
            latest.branch if latest else config.git.base_branch
        )
        if refreshed is not None:
            latest = refreshed


@dataclass(frozen=True)
class MergeQueueOutcome:
    """Outcome of one Issue's merge-queue attempt."""

    issue_number: int
    action: str  # "merged" | "skipped_no_pr" | "skipped_no_approval" | "skipped_verifier_missing" | "skipped_already_merged" | "blocked_forbidden" | "waiting_for_checks" | "rebase_failed" | "verification_failed"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def process_merge_queue(
    *,
    repo_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    supervisor_agent: str,
) -> tuple[int, list[MergeQueueOutcome]]:
    """Run the autopilot merge queue for one review pass.

    The function is a no-op when :func:`_autopilot_enabled` returns False
    (the caller in :mod:`review_once` already gated it, but the helper here
    is a defense-in-depth guarantee).

    Args:
        repo_path: Target repository path.
        config: Application configuration.
        github_client: GitHub client.
        process_runner: Process runner for git/verification commands.
        supervisor_agent: Agent used for rebase conflict resolution when
            conflicts emerge during rebase.

    Returns:
        ``(exit_code, outcomes)`` where exit_code is 0 on full success and
        1 if any Issue failed processing. Outcomes list one entry per
        Issue that was actually processed (skipped lists are included for
        audit but no-op skips are omitted).
    """
    if not _autopilot_enabled(config):
        return 0, []

    candidates = github_client.list_issues_by_label(config.labels.review, 100, state="open")
    # FIFO ordering: lower Issue numbers first to keep the audit trail
    # readable and to ensure earlier PRs land on the base branch first.
    candidates = sorted(candidates, key=lambda issue: issue.number)

    if not candidates:
        return 0, []

    exit_code = 0
    outcomes: list[MergeQueueOutcome] = []
    for issue in candidates:
        try:
            outcome = _process_one(
                repo_path=repo_path,
                config=config,
                issue=issue,
                github_client=github_client,
                process_runner=process_runner,
                supervisor_agent=supervisor_agent,
            )
        except Exception as exc:  # noqa: BLE001 - report and continue.
            _logger.error("Merge queue crashed for Issue #%d: %s", issue.number, exc)
            github_client.comment_issue(
                issue.number,
                f"## Agent Runner Merge Queue Failed\n\n```text\n{exc}\n```\n",
            )
            exit_code = 1
            outcomes.append(
                MergeQueueOutcome(issue_number=issue.number, action="merge_queue_crash")
            )
            continue
        outcomes.append(outcome)
    return exit_code, outcomes


# ---------------------------------------------------------------------------
# Per-issue state machine
# ---------------------------------------------------------------------------


def _process_one(
    *,
    repo_path: Path,
    config: AppConfig,
    issue: IssueSummary,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    supervisor_agent: str,
) -> MergeQueueOutcome:
    """Run the 7-step gate chain for a single Issue."""

    if not _autopilot_enabled(config):
        return MergeQueueOutcome(issue_number=issue.number, action="skipped_disabled")

    comments = github_client.list_issue_comments(issue.number)
    pr_branch = _extract_pr_branch_from_comments(comments)
    if pr_branch is None:
        return MergeQueueOutcome(issue_number=issue.number, action="skipped_no_pr")

    pr_context = github_client.get_pull_request_context(pr_branch)
    if pr_context is None:
        return MergeQueueOutcome(issue_number=issue.number, action="skipped_no_pr")
    pr_number_match = re.search(r"/pull/(\d+)", pr_context.pr_url)
    if not pr_number_match:
        return MergeQueueOutcome(issue_number=issue.number, action="skipped_no_pr")
    pr_number = int(pr_number_match.group(1))

    # Step 1: verifier gate
    if config.autopilot.require_verifier_pass and validation_required(issue.body, config):
        if config.labels.verifier_passed not in issue.labels:
            _logger.info(
                "Issue #%d requires validation but is missing %s label; skipping.",
                issue.number,
                config.labels.verifier_passed,
            )
            return MergeQueueOutcome(issue_number=issue.number, action="skipped_verifier_missing")

    # Step 2: auto sign-off (idempotent)
    if config.autopilot.auto_sign_off:
        pr_body_now = pr_context.body
        if pr_body_now:
            ticked_body = _tick_sign_off_checklist(pr_body_now)
            if ticked_body is not None and ticked_body != pr_body_now:
                github_client.update_pull_request_body(pr_number, ticked_body)
                if not _has_auto_sign_off_marker(comments):
                    github_client.comment_issue(
                        issue.number,
                        "## Autopilot auto sign-off\n\n"
                        "Verifier 绿灯后已自动勾选 Realistic Validation sign-off 清单"
                        "（机器门禁替换人工勾选，详见 PRD auto-merge-queue）。\n\n"
                        + format_event_marker(
                            phase="auto_signed_off",
                            cycle=0,
                            action="auto_sign_off",
                            pr_branch=pr_branch,
                        ),
                    )

    # Resolve the worktree for rebase + verification.
    try:
        worktree_path = create_or_reuse_worktree(repo_path, issue, config, process_runner)
    except Exception as exc:  # noqa: BLE001 - worktree failure is a soft skip.
        _logger.warning("Could not prepare worktree for Issue #%d: %s", issue.number, exc)
        return MergeQueueOutcome(issue_number=issue.number, action="skipped_no_worktree")

    # Step 3: rebase
    head_before = _safe_head_sha(worktree_path, process_runner)
    try:
        execute_rebase(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=process_runner,
            pr_branch=pr_branch,
            expected_head=head_before or pr_context.head_sha,
            supervisor_agent=supervisor_agent,
        )
    except Exception as exc:  # noqa: BLE001 - rebase failures route through supervising.
        _logger.warning(
            "Rebase failed for Issue #%d; routing back to supervisor: %s", issue.number, exc
        )
        github_client.comment_issue(
            issue.number,
            f"## Agent Runner Merge Queue — Rebase Failed\n\n```text\n{exc}\n```\n",
        )
        transition_issue_workflow_state(
            github_client, issue.number, config, config.labels.supervising
        )
        return MergeQueueOutcome(issue_number=issue.number, action="rebase_failed")

    # Step 4: full verification re-run
    verification_results = run_verification(worktree_path, config, process_runner)
    if any(result.return_code != 0 for result in verification_results):
        github_client.comment_issue(
            issue.number,
            "## Agent Runner Merge Queue — Verification Failed\n\n"
            "工作区重跑 verification_commands 失败；保留 ``agent/review`` 标签等待修复。\n",
        )
        return MergeQueueOutcome(issue_number=issue.number, action="verification_failed")

    # Step 5: forbidden-path final scan against the PR diff
    diff_paths = _diff_paths(pr_context, config, process_runner, worktree_path)
    forbidden_hits = sorted({path for path in diff_paths if is_forbidden_path(path, config)})
    if forbidden_hits:
        github_client.edit_issue_labels(issue.number, add=[config.labels.blocked])
        github_client.comment_issue(
            issue.number,
            "## Agent Runner Merge Queue — Forbidden Path\n\n"
            "PR diff 命中 ``safety.forbidden_path_patterns`` 中至少一条，永不自动合并：\n\n"
            + "\n".join(f"- `{path}`" for path in forbidden_hits)
            + "\n",
        )
        return MergeQueueOutcome(issue_number=issue.number, action="blocked_forbidden")

    # Step 6: wait for checks
    checks_green, _latest_context = _wait_for_checks_green(
        pr_context,
        config,
        github_client,
        timeout_seconds=config.autopilot.merge_check_timeout_seconds,
    )
    if not checks_green:
        return MergeQueueOutcome(issue_number=issue.number, action="waiting_for_checks")

    # Step 7: squash merge + audit
    try:
        github_client.merge_pull_request(pr_number, method=config.autopilot.merge_method)
    except Exception as exc:  # noqa: BLE001 - runtime errors route through blocked.
        _logger.error("Merge failed for Issue #%d PR #%d: %s", issue.number, pr_number, exc)
        github_client.comment_issue(
            issue.number,
            f"## Agent Runner Merge Queue — Merge Failed\n\n```text\n{exc}\n```\n",
        )
        transition_issue_workflow_state(github_client, issue.number, config, config.labels.blocked)
        return MergeQueueOutcome(issue_number=issue.number, action="merge_failed")

    head_after_merge = _safe_head_sha(worktree_path, process_runner) or pr_context.head_sha
    new_comments = github_client.list_issue_comments(issue.number)
    auto_sign_off_marker = ""
    if any(_AUTO_SIGN_OFF_MARKER in comment for comment in new_comments):
        auto_sign_off_marker = (
            "\n\n<!-- iar:auto-sign-off version=1 stage=merged verifier_gate=passed -->"
        )
    github_client.comment_issue(
        issue.number,
        "## Agent Runner Auto-Merged\n\n"
        f"PR #​{pr_number} 已通过合并队列的 7 步门禁链并被 ``squash`` 合并。\n"
        f"Head: `{head_after_merge}`\n"
        f"Branch: `{pr_branch}`\n\n"
        + format_event_marker(
            phase=_MERGED_EVENT_PHASE,
            cycle=0,
            head_sha=head_after_merge,
            pr_branch=pr_branch,
            action="merged_squash",
        )
        + auto_sign_off_marker,
    )
    transition_issue_workflow_state(
        github_client,
        issue.number,
        config,
        config.labels.review,  # any non-blocked durable state — Issue is now handled
    )
    # Best-effort: drop the now-stale `agent/review` label since the PR is merged.
    try:
        github_client.edit_issue_labels(issue.number, remove=[config.labels.review])
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "Could not drop %s label from Issue #%d: %s", config.labels.review, issue.number, exc
        )

    _logger.info(
        "Issue #%d PR #%d auto-merged into %s.", issue.number, pr_number, config.git.base_branch
    )
    return MergeQueueOutcome(issue_number=issue.number, action="merged")


def _ensure_worktree(
    repo_path: Path,
    issue: IssueSummary,
    config: AppConfig,
    process_runner: IProcessRunner,
    pr_branch: str,
) -> Path | None:
    """Return the worktree path for an Issue, creating if needed.

    This is a thin convenience wrapper around
    :func:`create_or_reuse_worktree` that returns ``None`` on failure rather
    than raising, mirroring the merge queue's soft-skip policy for missing
    worktrees. ``pr_branch`` is currently informational only — the create
    helper derives the branch from the issue number.

    Args:
        repo_path: Target repository path.
        issue: The Issue being processed.
        config: Application configuration.
        process_runner: Process runner.
        pr_branch: PR branch name (kept for API symmetry).

    Returns:
        The worktree path, or ``None`` if it could not be prepared.
    """
    del pr_branch  # pragma: no cover - API symmetry, not used.
    try:
        return create_or_reuse_worktree(repo_path, issue, config, process_runner)
    except Exception:  # noqa: BLE001 - soft skip.
        return None


def _safe_head_sha(worktree_path: Path, process_runner: IProcessRunner) -> str | None:
    """Return the current HEAD SHA in a worktree, or None on failure."""
    try:
        result = process_runner.run(["git", "rev-parse", "HEAD"], cwd=worktree_path, check=False)
    except subprocess.CalledProcessError:
        return None
    if result.return_code != 0:
        return None
    head_text = result.stdout.strip()
    return head_text or None
