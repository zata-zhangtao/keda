"""Post-PR supervisor 的修复分支执行流程。"""

from __future__ import annotations

from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner
from backend.core.shared.models.agent_runner import AppConfig, CommandResult, IssueSummary
from backend.core.use_cases.agent_runner_feedback import (
    VerificationFailedError,
    build_recovery_prompt,
    failed_verification_results,
)
from backend.core.use_cases.agent_runner_failure import format_recovery_failure_summary
from backend.core.use_cases.run_agent_once import (
    commit_requested_changes,
    ensure_verification_passed,
    has_changes,
    run_agent_with_prompt,
    run_verification,
)


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
    """在既有 PR 分支上运行修复 Agent 并提交变更。

    Args:
        issue: 正在修复的 Issue。
        worktree_path: Agent worktree 路径。
        config: 应用配置。
        process_runner: 命令执行器。
        pr_branch: PR 分支名称。
        expected_head: 修复前预期的 HEAD SHA。
        supervisor_agent: 执行修复的 Agent。

    Returns:
        修复提交后的验证结果。
    """
    from backend.core.use_cases.agent_runner_git import get_current_branch, get_head_sha

    current_head = get_head_sha(worktree_path, process_runner)
    if current_head != expected_head:
        raise RuntimeError(
            f"Repair aborted: HEAD {current_head} does not match expected {expected_head}"
        )

    current_branch = get_current_branch(worktree_path, process_runner)
    if current_branch != pr_branch:
        raise RuntimeError(f"Repair aborted: on branch {current_branch}, expected {pr_branch}")

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
            supervisor_agent, repair_prompt, worktree_path, process_runner, issue=issue
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
                    "Repair agent changed files without writing .agent-runner/commit-request.json."
                )
            verification_results = run_verification(worktree_path, config, process_runner)
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

    process_runner.run(
        ["git", "push", config.git.remote, pr_branch],
        cwd=worktree_path,
    )

    return verification_results
