"""Commit proxy for the agent runner.

Agent 和 runner 之间的 commit 协议文件路径。
Agent 将 commit message 写入此文件，runner 读取并执行实际 commit。
这样可以防止 agent 直接执行 git commit 绕过验证和安全检查。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
)
from backend.core.use_cases.agent_runner_feedback import (
    VerificationFailedError,
    ensure_verification_passed,
)
from backend.core.use_cases.agent_runner_git import (
    get_current_branch,
    get_head_sha,
    has_changes,
    list_changed_paths,
    run_verification,
)

_logger = logging.getLogger(__name__)

_COMMIT_REQUEST_RELATIVE_PATH = Path(".agent-runner/commit-request.json")
_MAX_COMMIT_MESSAGE_LENGTH = 200

# 空提交请求的固定文案；保留原始字符串以兼容 is_recoverable_commit_request_error
# 等基于 message 前缀匹配的旧逻辑。
EMPTY_COMMIT_REQUEST_MESSAGE = "Agent requested a commit but produced no file changes."

__all__ = [
    "EMPTY_COMMIT_REQUEST_MESSAGE",
    "EmptyCommitRequestError",
    "checkpoint_uncommitted_progress",
    "commit_requested_changes",
    "commit_runner_authored_paths",
    "default_commit_message",
    "read_commit_request",
    "remove_commit_request",
    "sanitize_commit_message",
    "unstage_changes",
]


class EmptyCommitRequestError(RuntimeError):
    """Agent 写了 commit-request 却没有任何实际文件改动。

    这是一个良性的空操作信号，而非真正的提交失败：调用方可据此区分
    "无内容可提交"（可安全收敛）与分支不匹配、禁改路径、验证失败等
    必须升级为硬失败的情况。继承 RuntimeError 以兼容既有的
    ``except RuntimeError`` 与基于 message 前缀的失败分类逻辑。
    """

    def __init__(self, message: str = EMPTY_COMMIT_REQUEST_MESSAGE) -> None:
        super().__init__(message)


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


def _commit_with_autofix_recovery(
    worktree_path: Path,
    commit_message: str,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> None:
    """提交 staged 改动，并从「自动修复型」pre-commit 钩子失败中恢复。

    某些 pre-commit 钩子（ruff-format、trailing-whitespace、end-of-file-fixer）
    会重写文件而非仅报告，导致首次 ``git commit`` 以 "files were modified by
    this hook" 失败。此处把这些钩子改写后的内容重新 stage 并重试一次；若第二次
    仍失败（例如补丁里有 ruff 报出的真实 lint 错误），底层抛出
    CommandFailedError，交由调用方上抛。

    根因层面的门禁一致性（验证命令应覆盖 ``git commit`` 的 pre-commit 钩子）
    由各仓库的 ``verification_commands`` 配置保证；本函数只负责对「自动改写型」
    钩子做一次幂等重试，避免纯格式化导致的偶发提交失败。
    """
    commit_command = ["git", "commit", "-m", commit_message]
    first_attempt = process_runner.run(commit_command, cwd=worktree_path, check=False)
    if first_attempt.return_code == 0:
        return
    if _verification_left_tracked_worktree_changes(worktree_path, process_runner):
        # Imported locally to avoid a circular dependency with agent_runner_publish.
        from backend.core.use_cases.agent_runner_publish import validate_safe_changes

        validate_safe_changes(worktree_path, config, process_runner)
        process_runner.run(["git", "add", "-u"], cwd=worktree_path)
    # 用 check=True 重试：重新 stage 解决格式问题则提交成功；否则由底层抛出
    # 带 pre-commit 输出的 CommandFailedError。
    process_runner.run(commit_command, cwd=worktree_path, check=True)


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
        RuntimeError: 分支不匹配或 commit request 无效。
        EmptyCommitRequestError: 写了 commit request 但没有任何文件变更（良性空操作）。
        VerificationFailedError: staging 后验证未通过。
        subprocess.CalledProcessError: git add 或 git commit 命令失败。
    """
    current_branch = get_current_branch(worktree_path, process_runner)
    if not current_branch:
        raise RuntimeError("Refusing to commit: worktree is in detached HEAD state.")
    if current_branch != expected_branch:
        raise RuntimeError(f"Refusing to commit on unexpected branch: {current_branch}")
    commit_message = read_commit_request(worktree_path, issue)
    remove_commit_request(worktree_path)
    if not has_changes(worktree_path, process_runner):
        raise EmptyCommitRequestError()
    # Imported locally to avoid a circular dependency with agent_runner_publish.
    from backend.core.use_cases.agent_runner_publish import validate_safe_changes

    validate_safe_changes(worktree_path, config, process_runner)
    process_runner.run(["git", "add", "-A"], cwd=worktree_path)
    # 在 git commit 前再次运行验证，确保 staged 内容仍通过门禁
    verification_results = run_verification(worktree_path, config, process_runner)
    try:
        ensure_verification_passed(verification_results)
    except VerificationFailedError:
        # Let the Fix Agent / Recovery Agent handle verification failures.
        raise
    if _verification_left_tracked_worktree_changes(worktree_path, process_runner):
        # Imported locally to avoid a circular dependency with agent_runner_publish.
        from backend.core.use_cases.agent_runner_publish import validate_safe_changes

        validate_safe_changes(worktree_path, config, process_runner)
        process_runner.run(["git", "add", "-u"], cwd=worktree_path)
    _commit_with_autofix_recovery(worktree_path, commit_message, config, process_runner)
    return verification_results


def unstage_changes(worktree_path: Path, process_runner: IProcessRunner) -> None:
    """Reset the Git index after a staged verification failure."""
    process_runner.run(["git", "reset", "--mixed"], cwd=worktree_path)


def checkpoint_uncommitted_progress(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    *,
    expected_branch: str,
) -> str | None:
    """把 agent 的在途改动提交成 WIP checkpoint，使其能跨 claim 续作。

    当交付门禁（verification / PRD / 证据）尚未全部满足、整轮尝试即将失败时，
    agent 已经产出的改动仍然有价值。把它们提交成 checkpoint，让进度落在本地
    分支上，被下一次 claim 在已提交基础上继续推进，而不是随失败被丢弃
    （worktree 复用 / 重置只保留已提交内容）。

    与正常 commit proxy（:func:`commit_requested_changes`）的区别：

    - 不要求 ``.agent-runner/commit-request.json``：agent 可能在写入提交请求前
      就耗尽预算，此处直接保存在途改动（含新增文件）。
    - 跳过 pre-commit hooks（在途工作可能还不通过 lint）。
    - **隔离禁改路径**：只 stage 非禁改路径并提交,把禁改文件留在工作区不入历史。
      旧实现一旦发现禁改路径就整块放弃 checkpoint,导致最该保住的在途代码也被
      丢掉;现在改为保住安全部分,禁改文件交人工/下一次处理。全部都是禁改路径时
      没有可提交内容,返回 ``None``。

    发布门禁（``_reuse_existing_local_commit`` / publication）仍会拦截未完成的
    工作，因此 checkpoint 永远不会被推送或合入；它只让进度可续作。

    Args:
        issue: 当前处理的 Issue。
        worktree_path: agent 工作的 git worktree 路径。
        config: Agent Runner 配置（用于 forbidden-path 判定）。
        process_runner: 命令执行器。
        expected_branch: 期望的分支名；分支不匹配时拒绝提交。

    Returns:
        新 checkpoint commit 的 SHA；没有可安全提交的内容或分支异常时返回 ``None``。
    """
    if not has_changes(worktree_path, process_runner):
        return None
    current_branch = get_current_branch(worktree_path, process_runner)
    if not current_branch or current_branch != expected_branch:
        return None
    # 禁改路径判定必须先于 staging,以便把它们排除在 checkpoint 之外。
    # Imported locally to avoid a circular dependency with agent_runner_publish.
    from backend.core.use_cases.agent_runner_publish import is_forbidden_path

    changed_paths = list_changed_paths(worktree_path, process_runner)
    safe_paths = [
        changed_path
        for changed_path in changed_paths
        if not is_forbidden_path(changed_path, config)
    ]
    if not safe_paths:
        # 在途改动全是禁改路径:没有可安全 checkpoint 的内容。
        return None
    process_runner.run(["git", "add", "--", *safe_paths], cwd=worktree_path)
    checkpoint_message = (
        f"[Agent][WIP] Issue #{issue.number} checkpoint "
        "(delivery gates not yet satisfied; not for merge)"
    )
    process_runner.run(
        ["git", "commit", "--no-verify", "-m", checkpoint_message],
        cwd=worktree_path,
    )
    excluded_paths = [
        changed_path
        for changed_path in changed_paths
        if is_forbidden_path(changed_path, config)
    ]
    if excluded_paths:
        _logger.warning(
            "Checkpoint for Issue #%d excluded %d forbidden path(s): %s",
            issue.number,
            len(excluded_paths),
            ", ".join(sorted(set(excluded_paths))),
        )
    return get_head_sha(worktree_path, process_runner)


def commit_runner_authored_paths(
    worktree_path: Path,
    relative_paths: Sequence[str],
    commit_message: str,
    config: AppConfig,
    process_runner: IProcessRunner,
    *,
    expected_branch: str,
) -> str | None:
    """Commit specific runner-authored paths (e.g. a generated PRD).

    与 agent commit proxy（:func:`commit_requested_changes`）的区别：本函数用于
    runner 直接产出、而非 agent 产出的文件改动，因此既不要求
    ``.agent-runner/commit-request.json``，也不运行 ``verification_commands``
    门禁——提交对象是文档（如生成的 PRD），不是需要 lint/test 的实现代码。

    仍保留两道安全门：提交前校验分支（防止落到非预期分支），并执行
    forbidden-path 校验（禁改路径绝不进历史）。``git commit`` 的 pre-commit
    钩子若改写文件，由 :func:`_commit_with_autofix_recovery` 重新 stage 后重试。

    Args:
        worktree_path: 目标 git worktree 路径。
        relative_paths: 相对 worktree 的待提交路径（如 ``tasks/pending/x.md``）。
        commit_message: 提交信息。
        config: Agent Runner 配置（用于 forbidden-path 校验）。
        process_runner: 命令执行器。
        expected_branch: 期望的分支名；分支不匹配时拒绝提交。

    Returns:
        新提交的 SHA；没有任何内容被 stage（如原地重写产出相同文本）时返回
        ``None``。

    Raises:
        RuntimeError: worktree 处于 detached HEAD 或分支不匹配。
    """
    current_branch = get_current_branch(worktree_path, process_runner)
    if not current_branch:
        raise RuntimeError("Refusing to commit: worktree is in detached HEAD state.")
    if current_branch != expected_branch:
        raise RuntimeError(f"Refusing to commit on unexpected branch: {current_branch}")
    process_runner.run(
        ["git", "add", "--", *relative_paths],
        cwd=worktree_path,
    )
    # 仅当确有 staged 改动才提交：``git diff --cached --quiet`` 返回 0 表示无暂存
    # 差异（例如重写产出与现有 PRD 完全一致），此时直接返回 None 视为 no-op。
    staged_diff = process_runner.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=worktree_path,
        check=False,
    )
    if staged_diff.return_code == 0:
        return None
    # Imported locally to avoid a circular dependency with agent_runner_publish.
    from backend.core.use_cases.agent_runner_publish import validate_safe_changes

    validate_safe_changes(worktree_path, config, process_runner)
    _commit_with_autofix_recovery(worktree_path, commit_message, config, process_runner)
    return get_head_sha(worktree_path, process_runner)
