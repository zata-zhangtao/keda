"""Commit proxy for the agent runner.

Agent 和 runner 之间的 commit 协议文件路径。
Agent 将 commit message 写入此文件，runner 读取并执行实际 commit。
这样可以防止 agent 直接执行 git commit 绕过验证和安全检查。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
)
from backend.core.use_cases.agent_runner_feedback import ensure_verification_passed
from backend.core.use_cases.agent_runner_git import (
    get_current_branch,
    has_changes,
    run_verification,
)
from backend.core.use_cases.agent_runner_publish import validate_safe_changes

_COMMIT_REQUEST_RELATIVE_PATH = Path(".agent-runner/commit-request.json")
_MAX_COMMIT_MESSAGE_LENGTH = 200

__all__ = [
    "commit_requested_changes",
    "default_commit_message",
    "read_commit_request",
    "remove_commit_request",
    "sanitize_commit_message",
    "unstage_changes",
]


def default_commit_message(issue: IssueSummary) -> str:
    """Build the fallback commit message for an Issue."""
    return f"[Agent] Issue #{issue.number}: {issue.title}"


def sanitize_commit_message(raw_message: object, issue: IssueSummary) -> str:
    """Return a single-line commit message safe to pass to Git."""
    if not isinstance(raw_message, str):
        return default_commit_message(issue)
    message = " ".join(raw_message.split())
    if not message:
        return default_commit_message(issue)
    return message[:_MAX_COMMIT_MESSAGE_LENGTH]


def read_commit_request(worktree_path: Path, issue: IssueSummary) -> str:
    """Read the agent's restricted commit request file."""
    request_path = worktree_path / _COMMIT_REQUEST_RELATIVE_PATH
    if not request_path.is_file():
        raise RuntimeError("Agent left uncommitted changes without a commit request.")
    with request_path.open("r", encoding="utf-8") as request_file:
        try:
            request_payload = json.load(request_file)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Commit request must be valid JSON.") from exc
    if not isinstance(request_payload, dict):
        raise RuntimeError("Commit request must be a JSON object.")
    return sanitize_commit_message(request_payload.get("commit_message"), issue)


def remove_commit_request(worktree_path: Path) -> None:
    """Remove the transient agent commit request file from the worktree."""
    request_path = worktree_path / _COMMIT_REQUEST_RELATIVE_PATH
    if request_path.exists():
        request_path.unlink()
    request_directory = request_path.parent
    try:
        request_directory.rmdir()
    except OSError:
        pass


def _verification_left_tracked_worktree_changes(
    worktree_path: Path,
    process_runner: IProcessRunner,
) -> bool:
    """Return whether verification changed tracked files after staging."""
    diff_result = process_runner.run(
        ["git", "diff", "--quiet"],
        cwd=worktree_path,
        check=False,
    )
    if diff_result.return_code == 0:
        return False
    if diff_result.return_code == 1:
        return True
    raise RuntimeError(
        "Unable to inspect worktree changes after verification: "
        f"{diff_result.stderr.strip()}"
    )


def commit_requested_changes(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    *,
    expected_branch: str,
) -> list[CommandResult]:
    """Commit agent changes through the runner's restricted commit proxy.

    该函数是 runner 控制 commit 的关键安全门：
    1. 检查分支安全（防止 agent 切换到意外分支后提交）
    2. 从 commit-request 文件读取 message（agent 不直接执行 git commit）
    3. 检查 forbidden paths（防止敏感文件被修改）
    4. 先 stage 再运行验证（确保提交内容通过 lint/test）
    5. 最后执行 git commit

    Args:
        issue: 当前处理的 Issue。
        worktree_path: agent 工作的 git worktree 路径。
        config: Agent Runner 配置。
        process_runner: 命令执行器。
        expected_branch: 期望的分支名。

    Returns:
        staging 后验证命令的结果列表。

    Raises:
        RuntimeError: 分支不匹配、无 commit request、无文件变更。
        VerificationFailedError: staging 后验证未通过。
        subprocess.CalledProcessError: git add 或 git commit 命令失败。
    """
    current_branch = get_current_branch(worktree_path, process_runner)
    if current_branch != expected_branch:
        raise RuntimeError(f"Refusing to commit on unexpected branch: {current_branch}")
    commit_message = read_commit_request(worktree_path, issue)
    remove_commit_request(worktree_path)
    if not has_changes(worktree_path, process_runner):
        raise RuntimeError("Agent requested a commit but produced no file changes.")
    validate_safe_changes(worktree_path, config, process_runner)
    process_runner.run(["git", "add", "-A"], cwd=worktree_path)
    # 在 git commit 前再次运行验证，确保 staged 内容仍通过门禁
    verification_results = run_verification(worktree_path, config, process_runner)
    ensure_verification_passed(verification_results)
    if _verification_left_tracked_worktree_changes(worktree_path, process_runner):
        validate_safe_changes(worktree_path, config, process_runner)
        process_runner.run(["git", "add", "-u"], cwd=worktree_path)
    process_runner.run(["git", "commit", "-m", commit_message], cwd=worktree_path)
    return verification_results


def unstage_changes(worktree_path: Path, process_runner: IProcessRunner) -> None:
    """Reset the Git index after a staged verification failure."""
    process_runner.run(["git", "reset", "--mixed"], cwd=worktree_path)
