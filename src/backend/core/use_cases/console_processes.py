"""管理终端的托管进程控制用例。

职责：

- 从白名单 ``RunnerProcessKind`` 枚举构建 ``iar`` argv —— 永不接受
  调用方传入的原始命令字符串，防注入且审计可枚举。
- 校验目标仓库在 registry 中且 enabled。
- 常驻类进程（daemon / review_daemon）按 ``(repo_id, kind)`` 去重。
- 委托 ``IRunnerProcessSupervisor`` 完成 spawn / stop / 日志续读。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from backend.core.shared.interfaces.runner_console import (
    PERSISTENT_PROCESS_KINDS,
    IRunnerProcessSupervisor,
    ProcessLogChunk,
    RunnerProcessKind,
    RunnerProcessRecord,
)
from backend.core.shared.models.agent_runner import RepositoryRunContext

_logger = logging.getLogger(__name__)

_DEFAULT_LOG_CHUNK_BYTES = 64 * 1024


class ConsoleProcessError(ValueError):
    """托管进程操作被拒绝（参数非法、目标不存在或重复启动）。"""


@dataclass(frozen=True)
class ConsoleProcessLaunchPlan:
    """一次托管进程启动的完整描述（argv 已由白名单构建）。"""

    repo_id: str
    kind: RunnerProcessKind
    argv: tuple[str, ...]
    cwd: Path


def build_runner_argv(
    *,
    runner_command: Sequence[str],
    kind: RunnerProcessKind,
    repo_id: str,
    issue_number: int | None = None,
) -> tuple[str, ...]:
    """从白名单枚举构建 runner 子进程的 argv。

    Args:
        runner_command: 启动命令前缀（如 ``["uv", "run", "iar"]``）。
        kind: 进程类型枚举。
        repo_id: 目标仓库 ID（传给 ``--repo-id``）。
        issue_number: 仅 ``BLOCKED_CONTINUE`` 需要的 Issue 编号。

    Returns:
        完整 argv 元组。

    Raises:
        ConsoleProcessError: kind 与参数组合非法。
    """
    command_prefix = tuple(runner_command)
    if not command_prefix:
        raise ConsoleProcessError("console.runner_command must not be empty.")
    selector = ("--repo-id", repo_id)
    if kind is RunnerProcessKind.DAEMON:
        return (*command_prefix, "daemon", *selector)
    if kind is RunnerProcessKind.REVIEW_DAEMON:
        return (*command_prefix, "review-daemon", *selector)
    if kind is RunnerProcessKind.RUN_ONCE:
        return (*command_prefix, "run", *selector)
    if kind is RunnerProcessKind.REVIEW_ONCE:
        return (*command_prefix, "review", *selector)
    if kind is RunnerProcessKind.BLOCKED_CONTINUE:
        if issue_number is None or issue_number <= 0:
            raise ConsoleProcessError("blocked_continue requires a positive issue_number.")
        return (
            *command_prefix,
            "blocked-continue",
            "--issue",
            str(issue_number),
            *selector,
        )
    raise ConsoleProcessError(f"Unsupported process kind: {kind}.")


def _resolve_enabled_context(
    repo_id: str, contexts: Sequence[RepositoryRunContext]
) -> RepositoryRunContext:
    for context in contexts:
        if context.repo_id == repo_id:
            return context
    raise ConsoleProcessError(f"Repository '{repo_id}' is not an enabled registry target.")


def start_runner_process(
    *,
    repo_id: str,
    kind: RunnerProcessKind,
    contexts: Sequence[RepositoryRunContext],
    supervisor: IRunnerProcessSupervisor,
    runner_command: Sequence[str],
    spawn_cwd: Path,
    issue_number: int | None = None,
) -> RunnerProcessRecord:
    """启动一个托管 runner 进程。

    Args:
        repo_id: 目标仓库 ID。
        kind: 进程类型（白名单枚举）。
        contexts: 当前可解析的 enabled 仓库上下文（用于校验 repo_id）。
        supervisor: 进程监管端口。
        runner_command: 启动命令前缀。
        spawn_cwd: 子进程工作目录（keda 项目根，保证读到正确配置）。
        issue_number: blocked_continue 所需的 Issue 编号。

    Returns:
        新进程的登记记录。

    Raises:
        ConsoleProcessError: 校验失败或同类常驻进程已在运行。
    """
    _resolve_enabled_context(repo_id, contexts)
    if kind in PERSISTENT_PROCESS_KINDS:
        for record in supervisor.list_processes():
            if record.repo_id == repo_id and record.kind == kind and record.status == "running":
                raise ConsoleProcessError(
                    f"A running {kind.value} process already exists for "
                    f"repository '{repo_id}' (process {record.process_id})."
                )
    argv = build_runner_argv(
        runner_command=runner_command,
        kind=kind,
        repo_id=repo_id,
        issue_number=issue_number,
    )
    _logger.info("Starting console process %s for '%s': %s", kind.value, repo_id, argv)
    return supervisor.spawn(repo_id=repo_id, kind=kind, argv=argv, cwd=spawn_cwd)


def stop_runner_process(
    *,
    process_id: str,
    supervisor: IRunnerProcessSupervisor,
    stop_timeout_seconds: int,
) -> RunnerProcessRecord:
    """停止一个托管进程。

    Raises:
        ConsoleProcessError: 进程未登记。
    """
    try:
        return supervisor.stop(process_id, timeout_seconds=stop_timeout_seconds)
    except KeyError as exc:
        raise ConsoleProcessError(str(exc)) from exc


def tail_runner_log(
    *,
    process_id: str,
    offset: int,
    supervisor: IRunnerProcessSupervisor,
    max_bytes: int = _DEFAULT_LOG_CHUNK_BYTES,
) -> ProcessLogChunk:
    """从指定偏移续读托管进程日志。

    Raises:
        ConsoleProcessError: 进程未登记。
    """
    try:
        return supervisor.read_log(process_id, offset=offset, max_bytes=max_bytes)
    except KeyError as exc:
        raise ConsoleProcessError(str(exc)) from exc
