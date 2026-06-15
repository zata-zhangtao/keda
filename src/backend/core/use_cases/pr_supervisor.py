"""Post-PR supervisor cycle for agent runner."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PullRequestContext,
    SupervisorActionResult,
)
from backend.core.use_cases.agent_runner_events import (
    format_event_marker,
)
from backend.core.use_cases.agent_runner_commit import (
    read_commit_request,
    remove_commit_request,
)
from backend.core.use_cases.agent_runner_feedback import (
    VerificationFailedError,
    build_recovery_prompt,
    failed_verification_results,
)
from backend.core.use_cases.agent_runner_failure import (
    format_recovery_failure_summary,
)
from backend.core.use_cases.agent_runner_verification_recovery import (
    ensure_verification_passed_with_recovery,
)
from backend.core.use_cases.run_agent_once import (
    commit_requested_changes,
    ensure_verification_passed,
    extract_agent_response_text,
    get_current_branch,
    get_head_sha,
    has_changes,
    run_agent_with_prompt,
    run_verification,
    validate_safe_changes,
)

_logger = logging.getLogger(__name__)


def _normalize_rebase_target_name(raw: str | None) -> str | None:
    """Normalize a raw rebase target branch name from Git metadata.

    Returns None for None or empty/whitespace-only strings.
    Strips leading "refs/heads/" prefix and surrounding whitespace.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    prefix = "refs/heads/"
    if cleaned.startswith(prefix):
        cleaned = cleaned[len(prefix) :]
    cleaned = cleaned.strip()
    return cleaned if cleaned else None


def _read_active_rebase_target_branch(
    worktree_path: Path, process_runner: IProcessRunner
) -> str | None:
    """Read the active rebase target branch from Git rebase metadata.

    Tries rebase-merge/head-name first, then rebase-apply/head-name.
    Returns the normalized branch name, or None if neither exists or is empty.
    """
    for rebase_dir in ("rebase-merge", "rebase-apply"):
        rev_parse_result = process_runner.run(
            ["git", "rev-parse", "--git-path", f"{rebase_dir}/head-name"],
            cwd=worktree_path,
            check=False,
        )
        if rev_parse_result.return_code != 0:
            continue
        head_name_path = worktree_path / Path(rev_parse_result.stdout.strip())
        if head_name_path.exists():
            raw = head_name_path.read_text(encoding="utf-8")
            return _normalize_rebase_target_name(raw)
    return None


def _ensure_rebase_context_matches_pr_branch(
    worktree_path: Path,
    process_runner: IProcessRunner,
    pr_branch: str,
) -> None:
    """Guard that the current Git context is safe to continue a rebase for pr_branch.

    Accepts detached HEAD only when active rebase metadata confirms the target
    is the expected PR branch.
    """
    current_branch = get_current_branch(worktree_path, process_runner)
    if current_branch == pr_branch:
        return
    if current_branch:
        raise RuntimeError(
            f"Refusing to continue rebase on unexpected branch: "
            f"observed branch '{current_branch}', expected '{pr_branch}'"
        )
    # Branch is empty: may be in detached HEAD / rebase intermediate state.
    active_target = _read_active_rebase_target_branch(worktree_path, process_runner)
    if active_target == pr_branch:
        return
    if active_target:
        raise RuntimeError(
            f"Refusing to continue rebase: active rebase target "
            f"'{active_target}' does not match expected PR branch '{pr_branch}'"
        )
    raise RuntimeError(
        f"Refusing to continue rebase: current branch is empty and "
        f"active rebase target cannot be confirmed (expected '{pr_branch}')"
    )


# 允许的超管动作集合；用集合保证 O(1) 校验并防止拼写错误导致意外行为
VALID_SUPERVISOR_ACTIONS: set[str] = {
    "approve_for_human_review",
    "repair_pr_branch",
    "rebase_pr_branch",
    "resolve_conflict",
    "request_human_input",
    "mark_failed",
    "wait_for_checks",
}

# 人工签核门 check 的名称：该 check 在人工 Reviewer 勾选签核项之前必然失败，
# 属于设计预期，不应被超管或守卫层当作真实的 CI 失败处理
REALISTIC_VALIDATION_SIGN_OFF_CHECK = "Realistic Validation sign-off"


def is_sign_off_gate_only_failure(pr_context: PullRequestContext) -> bool:
    """Return True when every reported failing check is the manual sign-off gate.

    Args:
        pr_context: PR context containing the checks summary.

    Returns:
        True only when the checks summary is non-empty and every entry is the
        Realistic Validation sign-off gate, so the failure can be positively
        identified as the expected manual gate.
    """
    return bool(pr_context.checks_summary) and all(
        REALISTIC_VALIDATION_SIGN_OFF_CHECK in check
        for check in pr_context.checks_summary
    )


def build_supervisor_prompt(
    issue: IssueSummary,
    pr_context: PullRequestContext,
    config: AppConfig,
    process_runner: IProcessRunner,
    worktree_path: Path,
    issue_comments: list[str],
    pr_comments: list[str],
    base_sha_remote: str,
) -> str:
    """Build the prompt sent to the post-PR supervisor agent."""
    prd_path_match = re.search(r"PRD path:\s*`([^`]+)`", issue.body)
    prd_line = (
        f"Canonical PRD: `{prd_path_match.group(1)}`"
        if prd_path_match
        else "If the Issue references a PRD, read it before reviewing."
    )

    diff_result = process_runner.run(
        ["git", "diff", f"{config.git.base_branch}...{pr_context.head_sha}"],
        cwd=worktree_path,
        check=False,
    )
    diff_text = (
        diff_result.stdout if diff_result.return_code == 0 else "(diff unavailable)"
    )

    verification_results = run_verification(worktree_path, config, process_runner)
    verification_lines = "\n".join(
        f"- `{' '.join(result.command)}`: exit {result.return_code}"
        for result in verification_results
    )

    # 只取最近 10 条并截断到 200 字符，防止上下文过长导致模型注意力稀释或 token 超限
    issue_comments_text = "\n".join(
        f"- {comment[:200]}" for comment in issue_comments[-10:]
    )
    pr_comments_text = "\n".join(f"- {comment[:200]}" for comment in pr_comments[-10:])

    return "\n".join(
        [
            f"Post-PR Supervisor Review for Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            f"PR URL: {pr_context.pr_url}",
            f"Branch: `{pr_context.branch}`",
            f"Head SHA: `{pr_context.head_sha}`",
            f"Base SHA (remote): `{base_sha_remote}`",
            f"PR Base SHA: `{pr_context.base_sha}`",
            f"Mergeable: {pr_context.mergeable}",
            f"Checks state: {pr_context.checks_state}",
            "Checks summary:",
            "\n".join(f"- {check}" for check in pr_context.checks_summary) or "(none)",
            prd_line,
            "",
            "Issue body:",
            issue.body,
            "",
            "Diff:",
            "```diff",
            # diff 截断到 6000 字符：超管评审只需把握整体变更方向，
            # 过长的 diff 会挤占其他上下文并增加模型处理时间
            diff_text[:6000] if len(diff_text) > 6000 else diff_text,
            "```",
            "",
            "Verification results:",
            verification_lines,
            "",
            "Recent Issue comments:",
            issue_comments_text or "(none)",
            "",
            "Recent PR comments:",
            pr_comments_text or "(none)",
            "",
            "Review workflow context:",
            "- Review scope: docs/guides/review-workflow.md",
            "- Check requirement alignment, code safety, validation evidence, and docs sync.",
            "",
            "Output rules:",
            "- Respond with a single JSON object in a markdown code block.",
            "- Required fields: action, summary.",
            f"- The `{REALISTIC_VALIDATION_SIGN_OFF_CHECK}` check is an "
            "intentional manual gate: it is expected to fail until a human "
            "reviewer ticks the sign-off checkboxes. If it is the only failing "
            "check, treat checks as healthy and return "
            "approve_for_human_review instead of request_human_input.",
            "- action must be one of: approve_for_human_review, repair_pr_branch, rebase_pr_branch, resolve_conflict, wait_for_checks, request_human_input, mark_failed.",
            "- Optional fields: findings_high (int), findings_medium (int), findings_low (int), verification_status (str), head_sha (str).",
            "- Do not modify files; only return the JSON decision.",
        ]
    )


def contains_supervisor_decision(text: str) -> bool:
    """Return True when the text contains a decodable JSON decision object.

    用于区分两类性质不同的失败：agent 正常运行但输出无效（保持 fail-closed，
    直接 mark_failed）与 agent 基础设施级崩溃（API / 网络错误导致非零退出且
    stdout 残缺，stdout 中找不到任何 JSON 决策），后者值得在同一 cycle 内重试。

    Args:
        text: Agent response text extracted from captured stdout.

    Returns:
        True only when a JSON object containing an ``action`` field can be
        decoded from the text, regardless of whether the action is valid.
    """
    # 提取逻辑与 parse_supervisor_action 保持一致，确保"可识别决策"的判定
    # 不会与实际解析行为产生分歧
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        json_text = match.group(1)
    else:
        match = re.search(r"\{.*\"action\".*\}", text, re.DOTALL)
        if not match:
            return False
        json_text = match.group(0)

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and "action" in payload


def parse_supervisor_action(text: str) -> SupervisorActionResult:
    """Parse supervisor JSON output from agent response text."""
    # 优先匹配 markdown 代码块，兼容模型在 JSON 外包裹解释文本的情况
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        json_text = match.group(1)
    else:
        # 回退：尝试直接提取包含 action 字段的最外层 JSON 对象
        match = re.search(r"\{.*\"action\".*\}", text, re.DOTALL)
        if not match:
            return SupervisorActionResult(
                action="mark_failed",
                summary=(
                    "Supervisor output was not parseable JSON; refusing to mark "
                    "the Issue blocked without an explicit human-input reason."
                ),
            )
        json_text = match.group(0)

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        return SupervisorActionResult(
            action="mark_failed",
            summary=(
                "Supervisor output was not parseable JSON; refusing to mark the "
                "Issue blocked without an explicit human-input reason."
            ),
        )

    raw_action = payload.get("action")
    action = str(raw_action) if raw_action is not None else ""
    if action not in VALID_SUPERVISOR_ACTIONS:
        return SupervisorActionResult(
            action="mark_failed",
            summary=(
                "Supervisor returned an unknown or missing action; refusing to "
                "mark the Issue blocked without an explicit human-input reason."
            ),
        )

    findings = {}
    for level in ("high", "medium", "low"):
        key = f"findings_{level}"
        if key in payload:
            try:
                findings[level] = int(payload[key])
            except (ValueError, TypeError):
                findings[level] = 0

    summary = str(payload.get("summary", ""))
    if action == "request_human_input" and not summary.strip():
        return SupervisorActionResult(
            action="mark_failed",
            summary=(
                "Supervisor requested human input without a summary; refusing "
                "to move the Issue to blocked without an actionable reason."
            ),
            findings_counts=findings,
            verification_status=str(payload.get("verification_status", "")),
            head_sha=str(payload.get("head_sha", "")) or None,
        )

    return SupervisorActionResult(
        action=action,
        summary=summary,
        findings_counts=findings,
        verification_status=str(payload.get("verification_status", "")),
        head_sha=str(payload.get("head_sha", "")) or None,
    )


def guard_supervisor_action_for_pr_state(
    action_result: SupervisorActionResult,
    pr_context: PullRequestContext,
) -> SupervisorActionResult:
    """Correct supervisor actions that contradict deterministic PR state.

    这是 LLM 决策与实际 PR 状态之间的守卫层：模型可能基于过时上下文
    批准代码，或把可机器修复的冲突保守地搁置给人工，而 GitHub 的
    mergeable/checks_state 是更接近事实的确定性信号，因此必须独立校验。
    """
    # 模型可能因为看到 Checks state: FAILURE 而保守地请求人工介入，但当唯一
    # 失败项是人工签核门且 PR 可合并时，语义正确的结局是转人工评审而非阻塞
    if (
        action_result.action == "request_human_input"
        and pr_context.mergeable is not False
        and pr_context.checks_state == "FAILURE"
        and is_sign_off_gate_only_failure(pr_context)
    ):
        summary = (
            "Action rewritten by sign-off gate guard: the only failing check "
            f"is the {REALISTIC_VALIDATION_SIGN_OFF_CHECK} manual gate, which "
            "is expected to fail until a human reviewer ticks the checkboxes. "
            "Approving for human review instead of requesting human input. "
            f"Supervisor summary: {action_result.summary}"
        )
        return SupervisorActionResult(
            action="approve_for_human_review",
            summary=summary,
            findings_counts=action_result.findings_counts,
            verification_status=action_result.verification_status,
            head_sha=action_result.head_sha,
        )

    # mergeable=False 是确定性、机器可修的信号：冲突不会随等待自愈，而
    # approve 会让人工 Reviewer 无法合并，request_human_input/wait_for_checks
    # 会把 Issue 留在 blocked 或 supervising 状态——review 轮询不扫描 blocked，
    # supervising 则因上下文未变而被跳过，冲突被永久搁置（真实案例：
    # Issue #53 / PR #70）。因此这三类动作一律先改写为 rebase 解决冲突。
    # mark_failed 保留终态：它也是 infra crash 与不可解析输出的兜底，
    # 改写会在故障期间制造无意义的返工。
    if pr_context.mergeable is False and action_result.action in (
        "approve_for_human_review",
        "request_human_input",
        "wait_for_checks",
    ):
        summary = (
            "Action rewritten by PR mergeability gate: the PR is currently "
            "conflicting or otherwise not mergeable, and "
            f"'{action_result.action}' would leave the conflict unresolved. "
            f"Requesting rebase first. Supervisor summary: {action_result.summary}"
        )
        return SupervisorActionResult(
            action="rebase_pr_branch",
            summary=summary,
            findings_counts=action_result.findings_counts,
            verification_status=action_result.verification_status,
            head_sha=action_result.head_sha,
        )

    if action_result.action != "approve_for_human_review":
        return action_result

    if pr_context.checks_state == "FAILURE":
        # The Realistic Validation sign-off is an intentional manual gate;
        # it is expected to fail until a human reviewer ticks the checkboxes.
        # Do not block approval for human review solely because of this gate,
        # but only when we can positively identify it as the unique failure.
        if is_sign_off_gate_only_failure(pr_context):
            return action_result

        failed_checks_text = (
            "; ".join(pr_context.checks_summary)
            if pr_context.checks_summary
            else "failed PR checks"
        )
        summary = (
            "Approval blocked by PR checks gate: checks are failing "
            f"({failed_checks_text}). Requesting branch repair before human "
            f"review. Supervisor summary: {action_result.summary}"
        )
        return SupervisorActionResult(
            action="repair_pr_branch",
            summary=summary,
            findings_counts=action_result.findings_counts,
            verification_status=action_result.verification_status,
            head_sha=action_result.head_sha,
        )

    if pr_context.checks_state == "PENDING":
        pending_checks_text = (
            "; ".join(pr_context.checks_summary)
            if pr_context.checks_summary
            else "PR checks are still pending"
        )
        summary = (
            "Approval deferred because PR checks are still pending "
            f"({pending_checks_text}). Waiting for checks to complete before "
            f"human review. Supervisor summary: {action_result.summary}"
        )
        return SupervisorActionResult(
            action="wait_for_checks",
            summary=summary,
            findings_counts=action_result.findings_counts,
            verification_status=action_result.verification_status,
            head_sha=action_result.head_sha,
        )

    return action_result


def build_supervisor_result_comment(
    *,
    action: str,
    supervisor: str,
    summary: str,
    findings_counts: dict[str, int],
    verification_status: str,
    head_sha: str | None,
    cycle: int,
    base_sha: str | None = None,
    checks_state: str | None = None,
    mergeable: bool | None = None,
    issue_comments_count: int | None = None,
    pr_comments_count: int | None = None,
) -> str:
    """Build the human-readable comment for a supervisor cycle result."""
    # marker 中记录 action，使下一轮 review pass 能识别 mark_failed 结局，
    # 在人工恢复 label 后允许同一上下文重新评审
    marker = format_event_marker(
        phase="post_pr_supervisor",
        cycle=cycle,
        head_sha=head_sha,
        base_sha=base_sha,
        action=action,
        checks_state=checks_state,
        mergeable=mergeable,
        issue_comments_count=issue_comments_count,
        pr_comments_count=pr_comments_count,
    )
    high = findings_counts.get("high", 0)
    medium = findings_counts.get("medium", 0)
    low = findings_counts.get("low", 0)
    return "\n".join(
        [
            marker,
            "",
            "## Agent Runner Post-PR Supervisor",
            "",
            f"- Action: {action}",
            f"- Supervisor: {supervisor}",
            f"- Summary: {summary}",
            f"- Findings: {high} high, {medium} medium, {low} low",
            f"- Verification: {verification_status or 'unknown'}",
            f"- Head SHA: `{head_sha or 'N/A'}`",
        ]
    )


def build_rework_intent_comment(
    *,
    action: str,
    pr_branch: str,
    head_sha: str,
) -> str:
    """Build the comment that marks a post-PR rework intent."""
    marker = format_event_marker(
        phase="post_pr_rework_requested",
        cycle=1,
        head_sha=head_sha,
        pr_branch=pr_branch,
        action=action,
    )
    return "\n".join(
        [
            marker,
            "",
            "## Agent Runner Post-PR Rework Requested",
            "",
            f"- Action: {action}",
            f"- PR Branch: `{pr_branch}`",
            f"- Head SHA: `{head_sha}`",
            "- A runner will pick this up on the next `iar run` pass.",
        ]
    )


def build_rebase_repair_complete_comment(
    *,
    action: str,
    head_sha: str,
    verification_passed: bool,
) -> str:
    """Build the comment after a rebase or repair completes."""
    marker = format_event_marker(
        phase="rebase_repair_complete",
        cycle=1,
        head_sha=head_sha,
    )
    return "\n".join(
        [
            marker,
            "",
            "## Agent Runner Rebase/Repair Complete",
            "",
            f"- Action: {action}",
            f"- Head SHA: `{head_sha}`",
            f"- Verification: {'passed' if verification_passed else 'failed'}",
        ]
    )


def build_conflict_resolution_prompt(
    issue: IssueSummary,
    pr_branch: str,
    expected_head: str,
    conflicted_files: list[str],
) -> str:
    """Build the prompt for the rebase conflict resolution agent."""
    files_text = "\n".join(f"- {f}" for f in conflicted_files) or "(none)"
    return "\n".join(
        [
            f"Resolve rebase conflicts for Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            f"PR Branch: `{pr_branch}`",
            f"Expected HEAD: `{expected_head}`",
            "",
            "The rebase onto the remote base branch encountered conflicts in these files:",
            files_text,
            "",
            "Resolve all conflicts and request a commit.",
            "- Only modify conflicted files inside the current worktree.",
            "- Do not switch branches, push, or abort the rebase.",
            "- Do not run `git add` or `git commit`; the runner handles staging.",
            "- After resolving conflicts, write `.agent-runner/commit-request.json` "
            "as JSON with `commit_message`.",
        ]
    )


def execute_rebase(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    pr_branch: str,
    expected_head: str,
    supervisor_agent: str,
) -> list[CommandResult]:
    """Rebase the PR branch onto the latest remote base safely.

    Args:
        issue: The Issue being rebased.
        worktree_path: Path to the worktree.
        config: Application configuration.
        process_runner: Process runner for git commands.
        pr_branch: Name of the PR branch.
        expected_head: Expected current HEAD SHA before rebase.
        supervisor_agent: Agent to run for conflict resolution.

    Returns:
        Verification results after rebase.
    """
    # 校验 HEAD 与分支，防止在错误状态上执行 rebase
    current_head = get_head_sha(worktree_path, process_runner)
    if current_head != expected_head:
        raise RuntimeError(
            f"Rebase aborted: HEAD {current_head} does not match expected {expected_head}"
        )

    _ensure_rebase_context_matches_pr_branch(worktree_path, process_runner, pr_branch)

    remote = config.git.remote
    base_branch = config.git.base_branch

    # 拉取最新 base，减少再次冲突概率
    process_runner.run(
        ["git", "fetch", remote, base_branch],
        cwd=worktree_path,
    )

    # 执行 rebase；冲突是预期情况，所以不设置 check=True
    rebase_result = process_runner.run(
        ["git", "rebase", f"{remote}/{base_branch}"],
        cwd=worktree_path,
        check=False,
    )

    if rebase_result.return_code != 0:
        max_attempts = max(0, config.post_pr_supervisor.max_repair_attempts)
        for attempt in range(1, max_attempts + 1):
            diff_names_result = process_runner.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=worktree_path,
                check=False,
            )
            conflicted_files = [
                line.strip()
                for line in diff_names_result.stdout.splitlines()
                if line.strip()
            ]

            if not conflicted_files and not has_changes(worktree_path, process_runner):
                continue_result = process_runner.run(
                    ["git", "-c", "core.editor=true", "rebase", "--continue"],
                    cwd=worktree_path,
                    check=False,
                )
                if continue_result.return_code == 0:
                    verification_results = ensure_verification_passed_with_recovery(
                        issue=issue,
                        worktree_path=worktree_path,
                        config=config,
                        process_runner=process_runner,
                        supervisor_agent=supervisor_agent,
                        pr_branch=pr_branch,
                    )
                    process_runner.run(
                        ["git", "push", "--force-with-lease", remote, pr_branch],
                        cwd=worktree_path,
                    )
                    return verification_results
                # continue 失败说明仍有未解决冲突，进入下一轮让 agent 处理
                continue

            if conflicted_files:
                prompt = build_conflict_resolution_prompt(
                    issue, pr_branch, expected_head, conflicted_files
                )
            else:
                failure_summary = format_recovery_failure_summary(
                    "Verification failed during rebase; continue fixing the issue.",
                    [],
                )
                prompt = build_recovery_prompt(
                    issue,
                    worktree_path,
                    recovery_attempt=attempt,
                    max_recovery_attempts=max_attempts,
                    failure_summary=failure_summary,
                )
            run_agent_with_prompt(
                supervisor_agent, prompt, worktree_path, process_runner
            )

            # Agent 通过 commit-request.json 显式表达提交意图
            request_path = worktree_path / ".agent-runner" / "commit-request.json"
            if request_path.is_file():
                # 再次确认分支上下文，防止 Agent 中途切换分支
                _ensure_rebase_context_matches_pr_branch(
                    worktree_path, process_runner, pr_branch
                )
                _ = read_commit_request(worktree_path, issue)
                remove_commit_request(worktree_path)
                if not has_changes(worktree_path, process_runner):
                    raise RuntimeError(
                        "Agent requested a commit but produced no file changes."
                    )
                validate_safe_changes(worktree_path, config, process_runner)
                process_runner.run(["git", "add", "-A"], cwd=worktree_path)
                # continue 前验证冲突解决结果
                verification_results = run_verification(
                    worktree_path, config, process_runner
                )
                if failed_verification_results(verification_results):
                    # 验证失败则取消 staging，让 agent 下一轮继续修
                    process_runner.run(
                        ["git", "reset", "--mixed"],
                        cwd=worktree_path,
                        check=False,
                    )
                    continue
                continue_result = process_runner.run(
                    ["git", "-c", "core.editor=true", "rebase", "--continue"],
                    cwd=worktree_path,
                    check=False,
                )
                if continue_result.return_code == 0:
                    # rebase 成功后再验证并推送
                    verification_results = ensure_verification_passed_with_recovery(
                        issue=issue,
                        worktree_path=worktree_path,
                        config=config,
                        process_runner=process_runner,
                        supervisor_agent=supervisor_agent,
                        pr_branch=pr_branch,
                    )
                    process_runner.run(
                        ["git", "push", "--force-with-lease", remote, pr_branch],
                        cwd=worktree_path,
                    )
                    return verification_results
            else:
                # Agent 未写 commit-request 却改文件，拒绝
                if has_changes(worktree_path, process_runner):
                    raise RuntimeError(
                        "Rebase conflict agent changed files without writing "
                        ".agent-runner/commit-request.json."
                    )
                # Agent 未改文件，尝试继续 rebase
                continue_result = process_runner.run(
                    ["git", "-c", "core.editor=true", "rebase", "--continue"],
                    cwd=worktree_path,
                    check=False,
                )
                if continue_result.return_code == 0:
                    verification_results = ensure_verification_passed_with_recovery(
                        issue=issue,
                        worktree_path=worktree_path,
                        config=config,
                        process_runner=process_runner,
                        supervisor_agent=supervisor_agent,
                        pr_branch=pr_branch,
                    )
                    process_runner.run(
                        ["git", "push", "--force-with-lease", remote, pr_branch],
                        cwd=worktree_path,
                    )
                    return verification_results

        # 重试用尽后回退 rebase
        process_runner.run(
            ["git", "rebase", "--abort"],
            cwd=worktree_path,
            check=False,
        )
        raise RuntimeError("Rebase conflict resolution exhausted")

    # 无冲突 rebase 后验证再推送
    verification_results = ensure_verification_passed_with_recovery(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        process_runner=process_runner,
        supervisor_agent=supervisor_agent,
        pr_branch=pr_branch,
    )

    # 用 force-with-lease 推送到 PR 分支
    process_runner.run(
        ["git", "push", "--force-with-lease", remote, pr_branch],
        cwd=worktree_path,
    )

    return verification_results


def execute_repair(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    pr_branch: str,
    expected_head: str,
    supervisor_agent: str,
) -> list[CommandResult]:
    """Run a repair agent on the existing PR branch and commit changes.

    Args:
        issue: The Issue being repaired.
        worktree_path: Path to the worktree.
        config: Application configuration.
        process_runner: Process runner for commands.
        pr_branch: Name of the PR branch.
        expected_head: Expected current HEAD SHA before repair.
        supervisor_agent: Agent to run for repair.

    Returns:
        Verification results after repair commit.
    """
    # 先锁定工作树状态，防止竞态
    current_head = get_head_sha(worktree_path, process_runner)
    if current_head != expected_head:
        raise RuntimeError(
            f"Repair aborted: HEAD {current_head} does not match expected {expected_head}"
        )

    current_branch = get_current_branch(worktree_path, process_runner)
    if current_branch != pr_branch:
        raise RuntimeError(
            f"Repair aborted: on branch {current_branch}, expected {pr_branch}"
        )

    repair_prompt = "\n".join(
        [
            f"Repair PR branch for Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            f"Worktree: {worktree_path}",
            "",
            "The post-PR supervisor requested code changes on this branch.",
            "Inspect the current worktree, make the necessary fixes, and request a commit.",
            "- Only modify files inside the current worktree.",
            "- Do not switch branches, merge main, push, or create PRs.",
            "- Do not run `git add` or `git commit`; the runner handles commits.",
            "- After fixing, write `.agent-runner/commit-request.json` as JSON with `commit_message`.",
        ]
    )

    max_attempts = max(1, config.post_pr_supervisor.max_repair_attempts)
    verification_results: list[CommandResult] = []
    for attempt in range(1, max_attempts + 1):
        run_agent_with_prompt(
            supervisor_agent, repair_prompt, worktree_path, process_runner
        )

        request_path = worktree_path / ".agent-runner" / "commit-request.json"
        if request_path.is_file():
            try:
                verification_results = commit_requested_changes(
                    issue,
                    worktree_path,
                    config,
                    process_runner,
                    expected_branch=pr_branch,
                )
            except VerificationFailedError as exc:
                if attempt >= max_attempts:
                    raise
                # 取消 staging，让 agent 继续修
                process_runner.run(
                    ["git", "reset", "--mixed"],
                    cwd=worktree_path,
                    check=False,
                )
                repair_prompt = build_recovery_prompt(
                    issue,
                    worktree_path,
                    recovery_attempt=attempt,
                    max_recovery_attempts=max_attempts,
                    failure_summary=format_recovery_failure_summary(
                        "Verification failed before repair commit.",
                        exc.verification_results,
                    ),
                )
                continue
        else:
            if has_changes(worktree_path, process_runner):
                raise RuntimeError(
                    "Repair agent changed files without writing "
                    ".agent-runner/commit-request.json."
                )
            # Agent 未修改时仍需验证当前代码
            verification_results = run_verification(
                worktree_path, config, process_runner
            )
            if failed_verification_results(verification_results):
                if attempt >= max_attempts:
                    ensure_verification_passed(verification_results)
                repair_prompt = build_recovery_prompt(
                    issue,
                    worktree_path,
                    recovery_attempt=attempt,
                    max_recovery_attempts=max_attempts,
                    failure_summary=format_recovery_failure_summary(
                        "Verification failed before repair commit.",
                        verification_results,
                    ),
                )
                continue
        break
    else:
        ensure_verification_passed(verification_results)

    remote = config.git.remote
    process_runner.run(
        ["git", "push", remote, pr_branch],
        cwd=worktree_path,
    )

    return verification_results


def run_post_pr_supervisor_cycle(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    pr_context: PullRequestContext,
    supervisor_agent: str,
    cycle: int,
) -> SupervisorActionResult:
    """Run a single post-PR supervisor cycle.

    Args:
        issue: The Issue being supervised.
        worktree_path: Path to the worktree.
        config: Application configuration.
        github_client: GitHub client for comments and context.
        process_runner: Process runner for commands.
        pr_context: PR context.
        supervisor_agent: Agent to use for supervision.
        cycle: Cycle number for event markers.

    Returns:
        Supervisor action result.
    """
    issue_comments = github_client.list_issue_comments(issue.number)
    # PR URL 中解析 PR number；Issue 与 PR 通常一一对应，但 PR 评论需单独拉取
    pr_number_match = re.search(r"/pull/(\d+)", pr_context.pr_url)
    pr_comments: list[str] = []
    if pr_number_match:
        pr_comments = github_client.list_pr_comments(int(pr_number_match.group(1)))

    # 获取远程 base 分支最新 SHA，用于判断 PR 是否落后于 base，
    # 并在提示词中给模型提供合并基线参考
    base_sha_remote = github_client.get_remote_base_sha(
        config.git.remote, config.git.base_branch
    )

    supervisor_prompt = build_supervisor_prompt(
        issue=issue,
        pr_context=pr_context,
        config=config,
        process_runner=process_runner,
        worktree_path=worktree_path,
        issue_comments=issue_comments,
        pr_comments=pr_comments,
        base_sha_remote=base_sha_remote,
    )

    # agent 非零退出且 stdout 中识别不到任何 JSON 决策时，视为基础设施级
    # 崩溃（API / 网络错误），在同一 cycle 内做有限重试；agent 正常退出但
    # 输出不可解析仍保持 fail-closed 直接 mark_failed，不重试。
    # 重试之间做指数退避（初始秒数每次翻倍并按上限封顶），以便扛住
    # 分钟级的 API 提供方中断，而不仅是秒级抖动
    max_crash_retries = max(0, config.post_pr_supervisor.max_agent_crash_retries)
    max_attempts = max_crash_retries + 1
    initial_backoff_seconds = max(
        0, config.post_pr_supervisor.crash_retry_initial_backoff_seconds
    )
    max_backoff_seconds = max(
        0, config.post_pr_supervisor.crash_retry_max_backoff_seconds
    )
    response_text = ""
    crash_exit_code: int | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = run_agent_with_prompt(
                supervisor_agent,
                supervisor_prompt,
                worktree_path,
                process_runner,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            result = CommandResult(
                command=tuple(exc.cmd),
                return_code=exc.returncode,
                stdout=exc.output or "",
                stderr=exc.stderr or "",
            )
            response_text = extract_agent_response_text(result)
            # Claude stream-json may return non-zero exit code while still
            # producing valid output in stdout; only treat the failure as an
            # infrastructure crash when no JSON decision can be recognized.
            if contains_supervisor_decision(response_text):
                _logger.warning(
                    "Supervisor agent exited with code %d for Issue #%d; "
                    "using the JSON decision found in captured stdout.",
                    exc.returncode,
                    issue.number,
                )
                crash_exit_code = None
                break
            crash_exit_code = exc.returncode
            _logger.warning(
                "Supervisor agent exited with code %d for Issue #%d with no "
                "JSON decision in stdout (attempt %d/%d); treating it as an "
                "agent infrastructure crash.",
                exc.returncode,
                issue.number,
                attempt,
                max_attempts,
            )
            if attempt < max_attempts:
                backoff_seconds = min(
                    max_backoff_seconds,
                    initial_backoff_seconds * (2 ** (attempt - 1)),
                )
                if backoff_seconds > 0:
                    _logger.info(
                        "Waiting %d seconds before supervisor retry %d/%d "
                        "for Issue #%d.",
                        backoff_seconds,
                        attempt + 1,
                        max_attempts,
                        issue.number,
                    )
                    time.sleep(backoff_seconds)
            continue
        response_text = extract_agent_response_text(result)
        crash_exit_code = None
        break

    if crash_exit_code is not None:
        raw_action_result = SupervisorActionResult(
            action="mark_failed",
            summary=(
                "Supervisor agent infrastructure failure: the agent process "
                f"exited with code {crash_exit_code} and produced no JSON "
                f"decision after {max_attempts} attempt(s); this is likely an "
                "API or network error rather than a review decision."
            ),
        )
    else:
        raw_action_result = parse_supervisor_action(response_text)
    # 先经过守卫层校正，再对外暴露最终决策，确保不违背客观 PR 状态
    action_result = guard_supervisor_action_for_pr_state(
        raw_action_result,
        pr_context,
    )

    comment_body = build_supervisor_result_comment(
        action=action_result.action,
        supervisor=supervisor_agent,
        summary=action_result.summary,
        findings_counts=action_result.findings_counts,
        verification_status=action_result.verification_status,
        head_sha=action_result.head_sha or pr_context.head_sha,
        cycle=cycle,
        base_sha=base_sha_remote,
        checks_state=pr_context.checks_state,
        mergeable=pr_context.mergeable,
        issue_comments_count=len(issue_comments) + 1,
        pr_comments_count=len(pr_comments),
    )
    # 评论写入 Issue 而非 PR，确保事件时间线与原始需求单保持一致，
    # 方便后续 run 调度器通过 Issue 评论追踪整体进度
    github_client.comment_issue(issue.number, comment_body)

    return action_result
