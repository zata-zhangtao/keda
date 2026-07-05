"""Agent Runner factory functions.

Each ``create_*`` and ``resolve_*`` entrypoint in this package is a thin
wrapper around the underlying engines / infrastructure implementations.
The original ``backend.engines.agent_runner.factory`` module imported
these directly; after the line-split refactor they are routed through
this sub-package while :mod:`backend.engines.agent_runner.factory`
continues to re-export them for backward compatibility.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from backend.core.shared.interfaces.agent_output_view import IAgentOutputView
from backend.core.shared.interfaces.runner_console import (
    IRoadmapStore,
    IRepositoryRegistryEditor,
    IRunHistoryStore,
    IRunnerProcessSupervisor,
)
from backend.core.shared.models.agent_deliberation import DeliberationEvent
from backend.core.shared.models.agent_decision import ReplConfig
from backend.core.shared.models.agent_runner import (
    AppConfig,
    RepositoryRunContext,
)
from backend.engines.agent_runner.deliberation_outputs import (
    write_deliberation_outputs,
)
from backend.engines.agent_runner.factories.content_generators import (
    create_content_generator,
    create_planner_runner,
)
from backend.engines.agent_runner.factory_config_builder import (
    build_app_config_from_settings,
)
from backend.engines.agent_runner.factory_repository_resolver import (
    resolve_repository_targets,
    resolve_repository_targets_with_diagnostics,
)
from backend.engines.agent_runner.persistence.loop_state_json import (
    JsonLoopStateStore,
    resolve_loop_state_path,
)
from backend.engines.agent_runner.scheduler.loop_clock import SystemClock
from backend.engines.agent_runner.transcript_runner import create_transcript_runner
from backend.infrastructure.config.registry_editor import TomlRegistryEditor
from backend.infrastructure.config.settings import (
    AgentRunnerSettings,
    config,
    resolve_project_root_path,
    resolve_registry_config_toml_path,
)
from backend.infrastructure.console.process_supervisor import PidfileProcessSupervisor
from backend.infrastructure.github_client import GitHubCliClient
from backend.infrastructure.logging.logger import logger
from backend.infrastructure.persistence.console_store import SqliteConsoleStore
from backend.infrastructure.process_runner import SubprocessRunner

if TYPE_CHECKING:
    pass

__all__ = [
    "build_app_config",
    "build_app_config_from_settings",
    "build_deliberation_config_from_settings",
    "create_content_generator",
    "create_console_store",
    "create_event_sink",
    "create_github_client",
    "create_loop_clock",
    "create_loop_state_store",
    "create_planner_runner",
    "create_process_runner",
    "create_process_supervisor",
    "create_registry_editor",
    "create_repl_command_executor",
    "create_roadmap_store",
    "create_transcript_runner",
    "get_agent_runner_settings",
    "get_agent_runner_status_data",
    "load_fresh_agent_runner_settings",
    "logger",
    "resolve_console_spawn_cwd",
    "resolve_issue_from_prd_target",
    "resolve_repository_targets",
    "resolve_repository_targets_with_diagnostics",
    "write_deliberation_outputs",
]


def build_app_config() -> AppConfig:
    """Convert global pydantic-settings ``AgentRunnerSettings`` to frozen ``AppConfig``."""
    return build_app_config_from_settings(config.agent_runner)


def get_agent_runner_settings() -> AgentRunnerSettings:
    """Return the global ``AgentRunnerSettings`` instance."""
    return config.agent_runner


def load_fresh_agent_runner_settings() -> AgentRunnerSettings:
    """重新从 config.toml 与环境变量加载 ``AgentRunnerSettings``。

    管理终端写回 registry 后，进程级单例 ``config`` 不会自动刷新；
    需要即时反映 registry 变化的读路径（监控 overview、console）应使用
    本函数而非 :func:`get_agent_runner_settings`。
    """
    return AgentRunnerSettings()


def get_agent_runner_status_data() -> dict:
    """Build the status response dict for the FastAPI status endpoint.

    Returns:
        A dictionary with ``daemon_mode``, ``config`` (global runner settings),
        and ``repositories`` (list of per-repository summaries).
    """
    agent_runner_settings = config.agent_runner
    app_config = build_app_config_from_settings(agent_runner_settings)
    repositories = []
    for repo_id in agent_runner_settings.repositories:
        repo = agent_runner_settings.repositories[repo_id]
        base_branch = app_config.git.base_branch
        remote = app_config.git.remote
        if repo.git is not None:
            base_branch = repo.git.base_branch or base_branch
            remote = repo.git.remote or remote
        repositories.append(
            {
                "repo_id": repo_id,
                "display_name": repo.display_name or repo_id,
                "enabled": repo.enabled,
                "base_branch": base_branch,
                "remote": remote,
            }
        )
    return {
        "daemon_mode": False,
        "config": {
            "max_issues": app_config.runner.max_issues,
            "default_agent": app_config.runner.default_agent,
            "max_recovery_attempts": app_config.runner.max_recovery_attempts,
            "recovery_retry_delay_seconds": (app_config.runner.recovery_retry_delay_seconds),
            "ready_label": app_config.labels.ready,
            "running_label": app_config.labels.running,
            "supervising_label": app_config.labels.supervising,
            "review_label": app_config.labels.review,
            "failed_label": app_config.labels.failed,
            "base_branch": app_config.git.base_branch,
            "remote": app_config.git.remote,
            "auto_merge": app_config.safety.auto_merge,
            "forbidden_path_patterns": list(app_config.safety.forbidden_path_patterns),
            "autopilot_enabled": app_config.autopilot.enabled,
            "autopilot_merge_method": app_config.autopilot.merge_method,
            "autopilot_require_verifier_pass": app_config.autopilot.require_verifier_pass,
            "autopilot_auto_sign_off": app_config.autopilot.auto_sign_off,
            "autopilot_merge_check_timeout_seconds": (
                app_config.autopilot.merge_check_timeout_seconds
            ),
            "pre_pr_review_enabled": app_config.pre_pr_review.enabled,
            "post_pr_supervisor_enabled": app_config.post_pr_supervisor.enabled,
        },
        "repositories": repositories,
    }


def resolve_issue_from_prd_target(
    settings: AgentRunnerSettings,
    *,
    repo_id: str | None = None,
    repo_path_override: str | None = None,
    cwd: Path,
) -> RepositoryRunContext:
    """Resolve the single target repository for ``iar issue create``.

    Defaults to the current Git repository and its repository-local config.

    Args:
        settings: Agent runner settings.
        repo_id: Optional configured repository ID selector.
        repo_path_override: Optional ad-hoc repository path selector.
        cwd: Current working directory.

    Returns:
        A single repository run context.

    Raises:
        ValueError: If both ``repo_id`` and ``repo_path_override`` are provided,
            or if ``repo_id`` does not exist or is disabled.
    """
    contexts = resolve_repository_targets(
        settings,
        repo_id=repo_id,
        repo_path_override=repo_path_override,
        fallback_path=str(cwd),
    )
    return contexts[0]


def create_console_store() -> IRunHistoryStore:
    """创建管理终端的运行历史 / 审计 SQLite 存储。"""
    console_settings = get_agent_runner_settings().console
    return SqliteConsoleStore(console_settings.history_db_path)


def create_roadmap_store() -> IRoadmapStore:
    """创建 roadmap 队列 / 设置存储（与 console_store 共用同一个 SQLite 文件）。"""
    console_settings = get_agent_runner_settings().console
    return SqliteConsoleStore(console_settings.history_db_path)


def create_process_supervisor() -> IRunnerProcessSupervisor:
    """创建托管 runner 进程的监管器（pidfile + 日志目录已解析）。"""
    console_settings = get_agent_runner_settings().console
    log_dir = Path(console_settings.process_log_dir).expanduser()
    if not log_dir.is_absolute():
        log_dir = resolve_project_root_path() / log_dir
    return PidfileProcessSupervisor(
        registry_path=console_settings.process_registry_path,
        log_dir=log_dir,
    )


def create_registry_editor() -> IRepositoryRegistryEditor:
    """创建仓库 registry 的受限写回编辑器。

    Registry 是全局共享的，必须固定写入 ``~/.iar/config.toml``，而不是
    当前工作目录下搜索到的某个项目级 config.toml。这避免了在目标
    仓库内执行 ``iar init`` 时意外污染该仓库的应用配置。
    """
    return TomlRegistryEditor(resolve_registry_config_toml_path())


def resolve_console_spawn_cwd() -> Path:
    """托管进程的工作目录：keda 项目根（保证子进程读到正确配置）。"""
    return resolve_project_root_path()


def create_process_runner() -> SubprocessRunner:
    """Create a new subprocess runner instance."""
    return SubprocessRunner()


def build_deliberation_config_from_settings(
    agent_runner_settings: AgentRunnerSettings,
) -> "AppConfig":  # type: ignore[override]
    """Convert pydantic-settings deliberation config to frozen core config.

    Re-exported for backward compatibility with the original
    ``backend.engines.agent_runner.factory.build_deliberation_config_from_settings``.
    """
    from backend.engines.agent_runner.factory_config_builder import (
        _build_deliberation_config,
    )

    return _build_deliberation_config(agent_runner_settings.deliberation)  # type: ignore[return-value]


def create_event_sink(
    output_dir: Path,
    output_view: IAgentOutputView | None = None,
) -> Callable[[DeliberationEvent], None]:
    """Create an event sink that writes to events.jsonl and shows a summary line.

    Args:
        output_dir: Directory where ``events.jsonl`` is appended.
        output_view: Optional live output view. When provided, the human
            readable summary line is routed through ``output_view.log`` so it
            does not corrupt an active live display; otherwise it is printed.
    """
    import json
    import threading

    events_path = output_dir / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)

    lock = threading.Lock()

    def _sink(event: DeliberationEvent) -> None:
        line = json.dumps(
            {
                "session_id": event.session_id,
                "round": event.round,
                "agent": event.agent,
                "event_type": event.event_type,
                "message": event.message,
                "timestamp": event.timestamp,
            },
            ensure_ascii=False,
        )
        with lock:
            with open(events_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        summary = (
            f"[{event.session_id}] round={event.round} agent={event.agent} event={event.event_type}"
        )
        if output_view is not None:
            output_view.log(summary)
        else:
            print(summary)

    return _sink


def create_github_client(
    repo_path: Path, process_runner: SubprocessRunner | None = None
) -> GitHubCliClient:
    """Create a new GitHub CLI client instance."""
    return GitHubCliClient(repo_path, process_runner)


def create_repl_command_executor(
    process_runner: SubprocessRunner | None = None,
    config: ReplConfig | None = None,
):
    """Create a :class:`ReplCommandExecutor` for the REPL entrypoint.

    Imports the executor lazily to avoid a hard dependency from this
    module's import time. ``config`` defaults to the merged global
    ReplConfig (``config.agent_runner.repl`` translated via
    ``build_app_config``).
    """
    from backend.engines.agent_runner.repl_command_executor import (
        ReplCommandExecutor,
    )

    if config is None:
        config = build_app_config().repl
    return ReplCommandExecutor(process_runner=process_runner or SubprocessRunner(), config=config)


def create_loop_state_store(state_path: Path | None = None) -> JsonLoopStateStore:
    """Create a JSON-backed loop state store.

    Args:
        state_path: Optional override for the on-disk JSON path. Defaults
            to ``~/.iar/loop-state.json``.

    Returns:
        A :class:`JsonLoopStateStore` instance.
    """
    return JsonLoopStateStore(state_path or resolve_loop_state_path())


def create_loop_clock() -> SystemClock:
    """Create the production wall-clock implementation for the loop daemon."""
    return SystemClock()
