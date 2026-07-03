"""Worktree discovery and readiness probes for the agent runner."""

from __future__ import annotations

import logging
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner
from backend.core.shared.models.agent_runner import AppConfig, IssueSummary
from backend.core.use_cases.agent_runner_git import (
    has_changes,
    has_rebase_metadata,
    is_detached_head,
)
from backend.core.use_cases.run_agent_once import format_command

_logger = logging.getLogger(__name__)


def _find_worktree_path_for_issue(
    repo_path: Path,
    issue: IssueSummary,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> Path:
    """根据 Issue 编号查找对应的 worktree 目录路径。

    通过执行配置的 path_command 获取 worktree 路径。
    path_command 通常是查找包含 issue 编号的 worktree 目录的脚本。

    Args:
        repo_path: 仓库根目录
        issue: Issue 对象
        config: 应用配置
        process_runner: 进程运行器

    Returns:
        worktree 的绝对路径
    """
    path_result = process_runner.run(
        format_command(config.worktree.path_command, issue_number=issue.number),
        cwd=repo_path,
    )
    # path_command runs with cwd=repo_path, so a relative output must be
    # anchored there too — bare resolve() would anchor it to the daemon
    # process cwd instead.
    worktree_path_output = Path(path_result.stdout.strip())
    if not worktree_path_output.is_absolute():
        worktree_path_output = repo_path / worktree_path_output
    worktree_path = worktree_path_output.resolve()
    if not worktree_path.exists():
        raise FileNotFoundError(
            "worktree path does not exist (path_command output): "
            f"{worktree_path}. path_command return_code={path_result.return_code}, "
            f"stdout={path_result.stdout!r}."
        )
    return worktree_path


def _has_existing_local_commit_ready_for_publish(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> bool:
    """检查 Issue 是否有可发布的本地提交。

    用于 running 状态 Issue 的发布恢复检测。
    轮询时发现 running Issue 会调用此函数判断是否可恢复发布。

    检测条件：
    1. 存在 worktree 目录
    2. 有超过 base 分支的提交
    3. 工作区干净（无未提交变更）

    Args:
        issue: Issue 对象
        repo_path: 仓库根目录
        config: 应用配置
        process_runner: 进程运行器

    Returns:
        是否有可发布的本地 commit
    """
    try:
        worktree_path = _find_worktree_path_for_issue(repo_path, issue, config, process_runner)
        from backend.core.use_cases.agent_runner_publication import (
            _count_local_commits_since_base,
        )

        return _count_local_commits_since_base(
            worktree_path, config, process_runner
        ) > 0 and not has_changes(worktree_path, process_runner)
    except Exception as exc:  # noqa: BLE001 - candidate probing must not fail polling.
        _logger.info(
            "Skipping existing local commit probe for Issue #%d: %s",
            issue.number,
            exc,
        )
        return False


def _worktree_needs_rebase_recovery(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> bool:
    """检查 running Issue 的 worktree 是否卡在中断的 rebase / detached HEAD。

    runner 在 rebase 中途崩溃或被中断时，会把 Issue worktree 留在 detached HEAD
    且带有 active rebase 元数据的状态。这种 worktree 的「领先 base 的干净 commit」
    探测必然为假——HEAD 停在 base 上（领先数为 0），工作区又有暂存的冲突解决
    （非干净），于是 :func:`_has_existing_local_commit_ready_for_publish` 不会把它
    识别为可恢复，导致每轮轮询都静默跳过、永不自愈。此处显式识别该状态，交由
    publish-recovery 路径里的 ``_ensure_worktree_branch`` 治愈（continue/abort
    rebase 或把分支重新挂回 detached HEAD）。

    Args:
        issue: Issue 对象。
        repo_path: 仓库根目录。
        config: 应用配置。
        process_runner: 进程运行器。

    Returns:
        worktree 是否处于需要恢复的 mid-rebase / detached HEAD 状态。
    """
    try:
        worktree_path = _find_worktree_path_for_issue(repo_path, issue, config, process_runner)
    except Exception as exc:  # noqa: BLE001 - 探测不得中断轮询。
        _logger.info(
            "Skipping rebase-recovery probe for Issue #%d: %s",
            issue.number,
            exc,
        )
        return False
    if not worktree_path.exists():
        return False
    return has_rebase_metadata(worktree_path, process_runner) or is_detached_head(
        worktree_path, process_runner
    )
