"""Post-PR supervisor cycle for agent runner."""

from __future__ import annotations

import json
import logging
import re
import subprocess
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
from backend.core.use_cases.run_agent_once import (
    commit_requested_changes,
    ensure_verification_passed,
    extract_agent_response_text,
    get_current_branch,
    get_head_sha,
    has_changes,
    read_commit_request,
    remove_commit_request,
    run_agent_with_prompt,
    run_verification,
    validate_safe_changes,
)

_logger = logging.getLogger(__name__)


# 允许的超管动作集合；用集合保证 O(1) 校验并防止拼写错误导致意外行为
VALID_SUPERVISOR_ACTIONS: set[str] = {
    "approve_for_human_review",
    "repair_pr_branch",
    "rebase_pr_branch",
    "resolve_conflict",
    "request_human_input",
    "mark_failed",
}


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
            "- action must be one of: approve_for_human_review, repair_pr_branch, rebase_pr_branch, resolve_conflict, request_human_input, mark_failed.",
            "- Optional fields: findings_high (int), findings_medium (int), findings_low (int), verification_status (str), head_sha (str).",
            "- Do not modify files; only return the JSON decision.",
        ]
    )


def parse_supervisor_action(text: str) -> SupervisorActionResult:
    """Parse supervisor JSON output from agent response text."""
    # 优先匹配 markdown 代码块，兼容模型在 JSON 外包裹解释文本的情况
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        json_text = match.group(1)
    else:
        # 回退：尝试直接提取包含 action 字段的最外层 JSON 对象
        match = re.search(r"\{.*\"action\".*\}", text, re.DOTALL)
        json_text = match.group(0) if match else "{}"

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        # 解析失败时降级为空对象，避免整轮崩溃
        payload = {}

    # 任何非法或缺失 action 均回退到 request_human_input，确保不会卡死流程
    action = str(payload.get("action", "request_human_input"))
    if action not in VALID_SUPERVISOR_ACTIONS:
        action = "request_human_input"

    findings = {}
    for level in ("high", "medium", "low"):
        key = f"findings_{level}"
        if key in payload:
            try:
                findings[level] = int(payload[key])
            except (ValueError, TypeError):
                findings[level] = 0

    return SupervisorActionResult(
        action=action,
        summary=str(payload.get("summary", "")),
        findings_counts=findings,
        verification_status=str(payload.get("verification_status", "")),
        head_sha=str(payload.get("head_sha", "")) or None,
    )


def guard_supervisor_action_for_pr_state(
    action_result: SupervisorActionResult,
    pr_context: PullRequestContext,
) -> SupervisorActionResult:
    """Prevent unsafe approval when deterministic PR state is not reviewable.

    这是 LLM 决策与实际 PR 状态之间的守卫层：模型可能基于过时上下文
    批准代码，而 GitHub 的 mergeable/checks_state 是更接近事实的
    确定性信号，因此必须独立校验。
    """
    if action_result.action != "approve_for_human_review":
        return action_result

    # mergeable=False 通常意味着存在冲突，直接批准会导致后续人工 Reviewer
    # 无法合并，因此强制转交 rebase 流程先解决冲突
    if pr_context.mergeable is False:
        summary = (
            "Approval blocked by PR mergeability gate: the PR is currently "
            "conflicting or otherwise not mergeable. Requesting rebase before "
            f"human review. Supervisor summary: {action_result.summary}"
        )
        return SupervisorActionResult(
            action="rebase_pr_branch",
            summary=summary,
            findings_counts=action_result.findings_counts,
            verification_status=action_result.verification_status,
            head_sha=action_result.head_sha,
        )

    if pr_context.checks_state == "FAILURE":
        # The Realistic Validation sign-off is an intentional manual gate;
        # it is expected to fail until a human reviewer ticks the checkboxes.
        # Do not block approval for human review solely because of this gate,
        # but only when we can positively identify it as the unique failure.
        if pr_context.checks_summary and all(
            "Realistic Validation sign-off" in check
            for check in pr_context.checks_summary
        ):
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
    marker = format_event_marker(
        phase="post_pr_supervisor",
        cycle=cycle,
        head_sha=head_sha,
        base_sha=base_sha,
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
            "- A runner will pick this up on the next `run-once` pass.",
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
    # 在执行任何变更前校验 HEAD 与分支，防止因并发操作或其他流程
    # 已修改工作树而导致 rebase 在错误状态上执行
    current_head = get_head_sha(worktree_path, process_runner)
    if current_head != expected_head:
        raise RuntimeError(
            f"Rebase aborted: HEAD {current_head} does not match expected {expected_head}"
        )

    current_branch = get_current_branch(worktree_path, process_runner)
    if current_branch != pr_branch:
        raise RuntimeError(
            f"Rebase aborted: on branch {current_branch}, expected {pr_branch}"
        )

    remote = config.git.remote
    base_branch = config.git.base_branch

    # 拉取远程最新 base，确保 rebase 目标是最新的，减少后续再次冲突的概率
    process_runner.run(
        ["git", "fetch", remote, base_branch],
        cwd=worktree_path,
    )

    # 执行 rebase；不设置 check=True，因为冲突是预期内的正常分支情况
    rebase_result = process_runner.run(
        ["git", "rebase", f"{remote}/{base_branch}"],
        cwd=worktree_path,
        check=False,
    )

    if rebase_result.return_code != 0:
        # 使用配置中的最大修复次数，避免在复杂冲突上无限循环消耗资源
        max_attempts = max(0, config.post_pr_supervisor.max_repair_attempts)
        for attempt in range(1, max_attempts + 1):
            # 获取当前存在冲突的文件列表，仅让 Agent 聚焦这些文件
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
            prompt = build_conflict_resolution_prompt(
                issue, pr_branch, expected_head, conflicted_files
            )
            run_agent_with_prompt(
                supervisor_agent, prompt, worktree_path, process_runner
            )

            # Agent 通过写入 commit-request.json 显式表达提交意图，
            # 避免无意的文件修改被自动提交
            request_path = worktree_path / ".agent-runner" / "commit-request.json"
            if request_path.is_file():
                # 在提交前再次确认分支，防止 Agent 中途切换了分支
                current_branch = get_current_branch(worktree_path, process_runner)
                if current_branch != pr_branch:
                    raise RuntimeError(
                        f"Refusing to commit on unexpected branch: {current_branch}"
                    )
                _ = read_commit_request(worktree_path, issue)
                remove_commit_request(worktree_path)
                if not has_changes(worktree_path, process_runner):
                    raise RuntimeError(
                        "Agent requested a commit but produced no file changes."
                    )
                validate_safe_changes(worktree_path, config, process_runner)
                process_runner.run(["git", "add", "-A"], cwd=worktree_path)
                # rebase --continue 前先做验证，确保冲突解决后的代码仍然健康
                verification_results = run_verification(
                    worktree_path, config, process_runner
                )
                ensure_verification_passed(verification_results)
                continue_result = process_runner.run(
                    ["git", "rebase", "--continue"],
                    cwd=worktree_path,
                    check=False,
                )
                if continue_result.return_code == 0:
                    # rebase 成功后再验证并推送，保证推送到远程的代码一定通过校验
                    verification_results = run_verification(
                        worktree_path, config, process_runner
                    )
                    ensure_verification_passed(verification_results)
                    # force-with-lease 比 force 安全：若远程分支在 fetch 后被他人
                    # 更新，则推送会失败，避免覆盖他人的并行工作
                    process_runner.run(
                        ["git", "push", "--force-with-lease", remote, pr_branch],
                        cwd=worktree_path,
                    )
                    return verification_results
            else:
                # Agent 没有写 commit-request 却改了文件，属于未授权修改，必须拒绝
                if has_changes(worktree_path, process_runner):
                    raise RuntimeError(
                        "Rebase conflict agent changed files without writing "
                        ".agent-runner/commit-request.json."
                    )
                # Agent 未修改文件，尝试继续 rebase，可能冲突已被外部解决
                continue_result = process_runner.run(
                    ["git", "rebase", "--continue"],
                    cwd=worktree_path,
                    check=False,
                )
                if continue_result.return_code == 0:
                    verification_results = run_verification(
                        worktree_path, config, process_runner
                    )
                    ensure_verification_passed(verification_results)
                    process_runner.run(
                        ["git", "push", "--force-with-lease", remote, pr_branch],
                        cwd=worktree_path,
                    )
                    return verification_results

        # 所有重试次数用尽后回退 rebase，保持工作树干净并抛出异常让上层决策
        process_runner.run(
            ["git", "rebase", "--abort"],
            cwd=worktree_path,
            check=False,
        )
        raise RuntimeError("Rebase conflict resolution exhausted")

    # rebase 未遇到冲突时，仍需验证代码健康度再推送
    verification_results = run_verification(worktree_path, config, process_runner)
    ensure_verification_passed(verification_results)

    # 仅在 PR 分支上使用 force-with-lease 推送，避免误操作其他分支
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
    # 与 rebase 同理：在让 Agent 介入前先锁定工作树状态，防止竞态
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

    run_agent_with_prompt(
        supervisor_agent, repair_prompt, worktree_path, process_runner
    )

    # Agent 必须通过 commit-request.json 显式请求提交；直接检测文件变更不可靠，
    # 因为 Agent 可能仅做探索性查看或临时文件修改，不应被误提交
    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    if request_path.is_file():
        verification_results = commit_requested_changes(
            issue,
            worktree_path,
            config,
            process_runner,
            expected_branch=pr_branch,
        )
    else:
        if has_changes(worktree_path, process_runner):
            raise RuntimeError(
                "Repair agent changed files without writing "
                ".agent-runner/commit-request.json."
            )
        # Agent 未做修改时仍需确认当前代码通过验证，避免空转后留下隐患
        verification_results = run_verification(worktree_path, config, process_runner)
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

    try:
        result = run_agent_with_prompt(
            supervisor_agent,
            supervisor_prompt,
            worktree_path,
            process_runner,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        # Claude stream-json may return non-zero exit code while still
        # producing valid output in stdout; attempt to parse captured output
        # before giving up.
        _logger.warning(
            "Supervisor agent exited with code %d for Issue #%d; "
            "attempting to parse captured stdout anyway.",
            exc.returncode,
            issue.number,
        )
        result = CommandResult(
            command=tuple(exc.cmd),
            return_code=exc.returncode,
            stdout=exc.output or "",
            stderr=exc.stderr or "",
        )
    raw_action_result = parse_supervisor_action(extract_agent_response_text(result))
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
    # 方便后续 run-once 调度器通过 Issue 评论追踪整体进度
    github_client.comment_issue(issue.number, comment_body)

    return action_result
