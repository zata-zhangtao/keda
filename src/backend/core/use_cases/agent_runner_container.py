"""容器命令 core facade —— ``iar container`` 子命令的用例边界。

本模块是容器化 runner 编排的 core 层入口。它**不**直接 import ``engines`` 或
``infrastructure``（受架构守门约束），而是通过
:mod:`backend.core.shared.interfaces.container_runner` 定义的端口与具体实现解耦：
``api`` 层在 dispatch 时把 engines 实现（``container_auth`` / ``container_ops``）
注入本 facade。

同时，启动容器前复用 :mod:`backend.core.use_cases.daemon_single_instance` 的锁
判断语义，确保本机 ``iar daemon`` 与 ``iar container up`` 不会抢同一仓库（rv-7）。

核心契约：

- ``import_container_auth(importer, global_iar_dir=None)`` —— 一次性把本机认证
  快照写入 ``~/.iar/container-auth/``。
- ``start_runner_container(controller, options, *, runner=None)`` —— 编排：定位
  资产 → daemon lock 互斥 → 注入环境变量 → 调 docker compose up。
- ``stop_runner_container(controller, compose_file, *, runner=None)`` —— 调 docker
  compose down。
- ``stream_runner_container_logs(controller, compose_file, *, runner=None)`` —— 同步
  streaming 容器日志。

依赖方向：core 只允许依赖自身（含 ``core/shared/interfaces``）；api 层负责注入
engines 实现。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from backend.core.shared.interfaces.container_runner import (
    ContainerCommandPlan,
    ContainerUpRequest,
    DockerRunnerCallable,
    IContainerAuthImporter,
    IContainerRunnerController,
    RunnerContainerAssets,
)
from backend.core.use_cases.daemon_single_instance import (
    DaemonAlreadyRunningError,
    daemon_lock_dir,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartRunnerContainerOptions:
    """``start_runner_container`` 的可注入编排选项。

    Attributes:
        repo_path: 目标仓库绝对路径，将挂载为容器 ``/workspace/repo``。
        repo_id: 仓库 registry id；非空时启用与本机 daemon lock 的互斥检查。
        gh_token: GitHub Token；容器内 gh 用此认证。
        process_registry_path: iar console 的 process_registry_path（默认
            ``~/.iar/processes.json``），daemon lock 目录由此推导。
        uid: 容器内进程 UID；``None`` 时回退到 ``os.getuid()``。
        gid: 容器内进程 GID；``None`` 时回退到 ``os.getgid()``。
        extra_env: 其它要注入 compose 的环境变量（合并而非覆盖）。
        build: ``docker compose up --build`` 开关。
        dry_run: True 时不调 docker compose，仅返回计划。
    """

    repo_path: Path
    repo_id: str = ""
    gh_token: str = ""
    process_registry_path: str = "~/.iar/processes.json"
    uid: int | None = None
    gid: int | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    build: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class StartRunnerContainerResult:
    """``start_runner_container`` 的执行结果。"""

    plan: ContainerCommandPlan
    assets: RunnerContainerAssets
    repo_id: str
    daemon_lock_check_skipped: bool


def import_container_auth(
    importer: IContainerAuthImporter,
    *,
    global_iar_dir: Path | None = None,
):
    """执行容器认证导入（薄封装，便于 api 层在 stderr 打印进度）。"""
    _logger.info("Importing agent CLI auth snapshots into container-auth dir.")
    result = importer.import_container_auth(global_iar_dir=global_iar_dir)
    for agent_result in result.agent_results:
        if agent_result.skipped:
            _logger.info(
                "%s: skipped (%s)",
                agent_result.agent_name,
                agent_result.skip_reason,
            )
        else:
            _logger.info(
                "%s: copied %d entries to %s",
                agent_result.agent_name,
                len(agent_result.copied_entries),
                agent_result.target_dir,
            )
    return result


def _ensure_repo_path_exists(repo_path: Path) -> None:
    """早期校验仓库路径存在。"""
    if not repo_path.exists():
        raise FileNotFoundError(
            f"Target repository path does not exist: {repo_path}. "
            "Pass an existing directory via the command's --repo / REPO_PATH."
        )


def start_runner_container(
    controller: IContainerRunnerController,
    options: StartRunnerContainerOptions,
    *,
    runner: DockerRunnerCallable | None = None,
) -> StartRunnerContainerResult:
    """编排容器启动：定位资产 → daemon lock 互斥 → 调 docker compose up。

    Args:
        controller: 容器生命周期端口实现（由 api 层注入 engines 实现）。
        options: 启动选项；其中 ``repo_id`` 非空时启用本机 daemon lock 互斥。
        runner: 注入的 docker runner 协议对象（仅测试使用）。

    Returns:
        :class:`StartRunnerContainerResult` —— 包含执行的命令计划、定位的资产
        与是否跳过 daemon lock 检查。

    Raises:
        DaemonAlreadyRunningError: 当 ``repo_id`` 非空且已有活 PID daemon 时。
        FileNotFoundError: 当 ``repo_path`` 不存在或 docker CLI / compose 资产
            缺失时。
    """
    _ensure_repo_path_exists(options.repo_path)

    assets = controller.resolve_packaged_runner_assets()

    daemon_lock_check_skipped = False
    if options.repo_id:
        _check_host_daemon_lock(options)
    else:
        daemon_lock_check_skipped = True
        _logger.warning(
            "start_runner_container called without repo_id; skipping daemon lock check."
        )

    compose_file = assets.compose_file
    request = _to_engine_request(options)
    plan = controller.run_container_up(compose_file, request, runner=runner)

    return StartRunnerContainerResult(
        plan=plan,
        assets=assets,
        repo_id=options.repo_id,
        daemon_lock_check_skipped=daemon_lock_check_skipped,
    )


def _check_host_daemon_lock(options: StartRunnerContainerOptions) -> None:
    """检查同 repo_id 的本机 daemon lock；命中活 PID 时 raise。"""
    lock_dir = daemon_lock_dir(options.process_registry_path)
    existing_lock = lock_dir / f"{options.repo_id}.lock"
    if not existing_lock.is_file():
        return
    try:
        owner_text = existing_lock.read_text(encoding="utf-8").strip()
        owner_pid = int(owner_text.splitlines()[0])
    except (OSError, ValueError, IndexError):
        owner_pid = -1
    if owner_pid != os.getpid() and _is_pid_alive(owner_pid):
        _logger.error(
            "Refusing to start container: repo_id %r already served by host daemon (PID %d).",
            options.repo_id,
            owner_pid,
        )
        raise DaemonAlreadyRunningError(options.repo_id, owner_pid)


def _to_engine_request(options: StartRunnerContainerOptions) -> ContainerUpRequest:
    """把 core 编排选项转成端口层 ``ContainerUpRequest``。"""
    return ContainerUpRequest(
        repo_path=options.repo_path,
        gh_token=options.gh_token,
        repo_id=options.repo_id,
        uid=options.uid,
        gid=options.gid,
        extra_env=options.extra_env,
        dry_run=options.dry_run,
        build=options.build,
    )


def stop_runner_container(
    controller: IContainerRunnerController,
    compose_file: Path,
    *,
    runner: DockerRunnerCallable | None = None,
) -> list[str]:
    """停止容器（薄封装）。"""
    return controller.run_container_down(compose_file, runner=runner)


def stream_runner_container_logs(
    controller: IContainerRunnerController,
    compose_file: Path,
    *,
    follow: bool = True,
    runner: DockerRunnerCallable | None = None,
) -> list[str]:
    """streaming 容器日志（薄封装）。"""
    return controller.run_container_logs(compose_file, follow=follow, runner=runner)


def _is_pid_alive(pid: int) -> bool:
    """进程存活判定（macOS / Linux 均兼容）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


__all__ = [
    "StartRunnerContainerOptions",
    "StartRunnerContainerResult",
    "import_container_auth",
    "start_runner_container",
    "stop_runner_container",
    "stream_runner_container_logs",
]
