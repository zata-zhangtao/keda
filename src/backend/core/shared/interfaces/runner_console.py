"""统一管理终端（Operations Console）的端口与共享模型。

本模块定义管理终端在 core 层依赖的三个端口：

- ``IRunnerProcessSupervisor``：托管 runner 子进程（spawn / 探活 / 停止 /
  日志续读）。实现位于 ``infrastructure/console/process_supervisor.py``。
- ``IRunHistoryStore``：运行历史与审计日志的旁路存储。实现位于
  ``infrastructure/persistence/console_store.py``（本地 SQLite）。
- ``IRepositoryRegistryEditor``：对 ``config.toml`` 仓库 registry 的受限
  写回。实现位于 ``infrastructure/config/registry_editor.py``（tomlkit）。

设计约束：

- SQLite 历史只是旁路记录，不参与 workflow 状态机决策；GitHub
  labels/comments/PR 仍是唯一事实来源。
- 进程监管以 pidfile registry 中由面板启动的托管进程为操作对象；
  同时提供 ``list_unmanaged_processes`` 用于观测用户手工启动的 CLI
      进程，但不对其执行停止、重启等生命周期操作。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence


class RunnerProcessKind(str, Enum):
    """面板可托管的 runner 进程类型（白名单）。"""

    DAEMON = "daemon"
    REVIEW_DAEMON = "review_daemon"
    RUN_ONCE = "run_once"
    REVIEW_ONCE = "review_once"
    BLOCKED_CONTINUE = "blocked_continue"


#: 常驻类进程：同一 (repo_id, kind) 同时只允许一个 running 实例。
PERSISTENT_PROCESS_KINDS = frozenset(
    {RunnerProcessKind.DAEMON, RunnerProcessKind.REVIEW_DAEMON}
)


@dataclass(frozen=True)
class RunnerProcessRecord:
    """一个被托管 runner 进程的状态快照。"""

    process_id: str
    repo_id: str
    kind: RunnerProcessKind
    pid: int
    status: str  # running / exited / stopped / killed
    exit_code: int | None
    log_path: str
    command: tuple[str, ...]
    started_at: str
    stopped_at: str | None


@dataclass(frozen=True)
class ProcessLogChunk:
    """日志 offset 续读的一段内容。"""

    content: str
    next_offset: int
    eof: bool


@dataclass(frozen=True)
class RunRecord:
    """一次 Issue 处理的运行结果（旁路记录）。"""

    repo_id: str
    repo_path: str
    issue_number: int
    trigger: str  # cli_run / cli_daemon / console_run / console_daemon
    agent: str
    outcome: str  # completed / failed / blocked
    error_summary: str | None
    started_at: str  # ISO8601 UTC
    finished_at: str  # ISO8601 UTC
    duration_seconds: float


@dataclass(frozen=True)
class AttemptRecord:
    """一次 agent execution attempt 的旁路记录。

    与 :class:`backend.core.shared.models.agent_runner.AttemptResult` 同构，
    用于本地 SQLite 持久化，方便在 runner 崩溃或跨 agent fallback 后仍能
    复盘每轮耗时与失败原因。
    """

    repo_id: str
    issue_number: int
    agent: str
    attempt_number: int
    failure_type: str
    recovered: bool
    detail: str
    started_at: str  # ISO8601 UTC
    finished_at: str  # ISO8601 UTC
    duration_seconds: float


@dataclass(frozen=True)
class AuditEntry:
    """一次管理终端写操作的审计条目。"""

    occurred_at: str  # ISO8601 UTC
    actor: str
    action: str
    repo_id: str | None
    issue_number: int | None
    params_json: str
    result: str  # accepted / rejected / error
    detail: str | None


@dataclass(frozen=True)
class DailyRunTrendEntry:
    """运行历史按天聚合的一个数据点。"""

    day: str  # YYYY-MM-DD
    completed: int
    failed: int
    blocked: int
    average_duration_seconds: float | None


class IRunnerProcessSupervisor(ABC):
    """托管 runner 子进程生命周期的端口。"""

    @abstractmethod
    def spawn(
        self,
        *,
        repo_id: str,
        kind: RunnerProcessKind,
        argv: Sequence[str],
        cwd: Path,
    ) -> RunnerProcessRecord:
        """启动一个脱离当前进程组的 runner 子进程并登记。

        Args:
            repo_id: 目标仓库 ID。
            kind: 进程类型（白名单枚举）。
            argv: 完整命令参数序列，不经过 shell 解析。
            cwd: 子进程工作目录。

        Returns:
            RunnerProcessRecord: 新进程的登记记录（status 为 running）。
        """
        ...

    @abstractmethod
    def list_processes(self) -> list[RunnerProcessRecord]:
        """列出全部登记的进程并刷新其存活状态。"""
        ...

    @abstractmethod
    def list_unmanaged_processes(
        self, registry_entries: list[RegistryRepositoryEntry]
    ) -> list[RunnerProcessRecord]:
        """扫描系统进程，返回属于 registry 但未在 pidfile 中登记的 runner 进程。

        结果仅用于观测，不纳入 ``stop`` / ``read_log`` 等托管生命周期操作。
        实现应过滤当前用户拥有的进程，并排除已在 ``list_processes`` 中
        出现的 pid。

        Args:
            registry_entries: 当前 registry 中的仓库条目，用于按 cwd 匹配
                未显式指定 ``--repo-id`` 的手动进程。
        """
        ...

    @abstractmethod
    def get_process(self, process_id: str) -> RunnerProcessRecord | None:
        """按 ID 查询单个进程的最新状态，不存在时返回 ``None``。"""
        ...

    @abstractmethod
    def stop(self, process_id: str, *, timeout_seconds: int) -> RunnerProcessRecord:
        """停止进程：先 SIGTERM，超时后升级 SIGKILL。

        Args:
            process_id: 进程登记 ID。
            timeout_seconds: SIGTERM 后等待的秒数。

        Returns:
            RunnerProcessRecord: 停止后的最终记录。

        Raises:
            KeyError: 进程 ID 未登记。
        """
        ...

    @abstractmethod
    def read_log(
        self, process_id: str, *, offset: int, max_bytes: int
    ) -> ProcessLogChunk:
        """从指定偏移量续读进程日志。

        Args:
            process_id: 进程登记 ID。
            offset: 起始字节偏移。
            max_bytes: 本次最多读取的字节数。

        Returns:
            ProcessLogChunk: 日志内容、下一偏移与是否到达文件尾。

        Raises:
            KeyError: 进程 ID 未登记。
        """
        ...


class IRunHistoryStore(ABC):
    """运行历史与审计日志的旁路存储端口。"""

    @abstractmethod
    def append_run(self, run_record: RunRecord) -> None:
        """追加一条运行记录。实现必须不抛出阻断 runner 的异常。"""
        ...

    @abstractmethod
    def append_attempt(self, attempt_record: AttemptRecord) -> None:
        """追加一条 attempt 记录。实现必须不抛出阻断 runner 的异常。"""
        ...

    @abstractmethod
    def append_audit(self, audit_entry: AuditEntry) -> None:
        """追加一条审计条目。"""
        ...

    @abstractmethod
    def list_recent_runs(
        self, *, repo_id: str | None = None, limit: int = 100
    ) -> list[RunRecord]:
        """倒序列出最近的运行记录。"""
        ...

    @abstractmethod
    def list_recent_audits(self, *, limit: int = 100) -> list[AuditEntry]:
        """倒序列出最近的审计条目。"""
        ...

    @abstractmethod
    def daily_run_trend(
        self, *, repo_id: str | None, days: int
    ) -> list[DailyRunTrendEntry]:
        """按天聚合最近 ``days`` 天的运行结果。"""
        ...


@dataclass(frozen=True)
class RegistryRepositoryEntry:
    """registry 中一个仓库条目的摘要视图。"""

    repo_id: str
    path: str
    enabled: bool
    display_name: str | None
    path_exists: bool


@dataclass(frozen=True)
class DiscoveredRepositoryEntry:
    """本地扫描发现的 IAR 仓库候选条目。"""

    repo_id: str
    path: str
    display_name: str | None
    already_registered: bool


class IRepositoryRegistryEditor(ABC):
    """对仓库 registry（config.toml）的受限读写端口。

    实现只允许触碰 ``agent_runner.repositories.<repo_id>`` 子树，
    其余配置节必须保持原样（含注释与格式）。
    """

    @abstractmethod
    def list_repositories(self) -> list[RegistryRepositoryEntry]:
        """列出 registry 中的全部仓库条目。"""
        ...

    @abstractmethod
    def add_repository(
        self, *, repo_id: str, path: str, display_name: str | None
    ) -> None:
        """新增一个仓库条目（enabled 默认 true）。

        Raises:
            ValueError: repo_id 已存在。
        """
        ...

    @abstractmethod
    def set_enabled(self, repo_id: str, *, enabled: bool) -> None:
        """启用或停用一个已有条目。

        Raises:
            KeyError: repo_id 不存在。
        """
        ...

    @abstractmethod
    def remove_repository(self, repo_id: str) -> None:
        """从 registry 中删除一个仓库条目。

        Raises:
            KeyError: repo_id 不存在。
        """
        ...


@dataclass(frozen=True)
class RoadmapQueueEntry:
    """roadmap 全局调度队列的一条记录（core 侧端口类型）。"""

    repo_id: str
    prd_path: str
    status: str  # queued / running / completed / failed
    trigger: str  # manual / global
    started_at: str | None
    finished_at: str | None
    error_detail: str | None
    entry_id: int | None = None


@dataclass(frozen=True)
class RoadmapSettingsEntry:
    """roadmap 用户设置（core 侧端口类型）。"""

    repo_id: str
    max_parallel: int
    default_view: str  # timeline / list
    updated_at: str


class IRoadmapStore(ABC):
    """roadmap 调度队列与设置的旁路存储端口。"""

    @abstractmethod
    def get_roadmap_settings(self, repo_id: str) -> RoadmapSettingsEntry | None:
        """读取指定仓库的 roadmap 设置；不存在时返回 ``None``。"""
        ...

    @abstractmethod
    def save_roadmap_settings(self, settings: RoadmapSettingsEntry) -> None:
        """保存或更新 roadmap 设置；失败时抛出异常。"""
        ...

    @abstractmethod
    def enqueue_roadmap(self, entry: RoadmapQueueEntry) -> int:
        """将 PRD 加入 roadmap 队列，返回自增 ID；失败时抛出异常。"""
        ...

    @abstractmethod
    def list_roadmap_queue(
        self, *, repo_id: str | None = None, status: str | None = None
    ) -> list[RoadmapQueueEntry]:
        """列出 roadmap 队列条目，支持按仓库与状态过滤。"""
        ...

    @abstractmethod
    def update_roadmap_queue_status(
        self,
        *,
        entry_id: int,
        status: str,
        started_at: str | None = None,
        finished_at: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """更新队列条目的状态；失败时抛出异常。"""
        ...

    @abstractmethod
    def clear_roadmap_queue(self, *, repo_id: str | None = None) -> None:
        """清空 roadmap 队列；失败时抛出异常。"""
        ...
