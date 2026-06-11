"""Agent Runner 管理终端（Operations Console）写操作与统计 API。

只读监控端点保留在 ``routes/agent_runner.py``；本路由承载全部
console 能力：托管进程启停与日志、白名单动作、完成度统计、审计
与仓库 registry 管理。

安全边界：本机单用户部署。所有写操作只能映射到硬编码白名单动作
枚举，后端从枚举构建 argv，永不接受请求方传入的原始命令字符串。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core.shared.interfaces.runner_console import RunnerProcessKind
from backend.core.use_cases.console_actions import (
    ConsoleActionError,
    execute_issue_action,
    execute_repository_action,
)
from backend.core.use_cases.console_processes import (
    ConsoleProcessError,
    start_runner_process,
    stop_runner_process,
    tail_runner_log,
)
from backend.core.use_cases.console_stats import (
    build_completion_stats_overview,
    build_run_history_trend,
)
from backend.core.use_cases.repository_registry import (
    RegistryValidationError,
    add_registry_repository,
    list_registry_repositories,
    set_registry_repository_enabled,
)
from backend.engines.agent_runner.factory import (
    create_console_store,
    create_github_client,
    create_process_supervisor,
    create_registry_editor,
    load_fresh_agent_runner_settings,
    resolve_console_spawn_cwd,
    resolve_repository_targets_with_diagnostics,
)

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-runner-console"])


def _serialize(value: Any) -> Any:
    """递归地把 dataclass / Enum 转成 JSON 友好结构。"""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    return value


def _resolve_contexts():
    # registry 写回后必须即时生效，因此每次重新加载 settings。
    settings = load_fresh_agent_runner_settings()
    contexts, _failures = resolve_repository_targets_with_diagnostics(settings)
    return contexts


# ─────────────────────────────────────────────────────────────────────────────
# 托管进程
# ─────────────────────────────────────────────────────────────────────────────


class StartProcessRequest(BaseModel):
    """启动托管进程的请求体。"""

    repo_id: str = Field(min_length=1)
    kind: RunnerProcessKind


@router.get("/agent-runner/console/processes")
def list_console_processes() -> dict:
    """列出全部托管进程（含已退出的历史记录）。"""
    supervisor = create_process_supervisor()
    return {"processes": [_serialize(r) for r in supervisor.list_processes()]}


@router.post("/agent-runner/console/processes", status_code=201)
def start_console_process(request: StartProcessRequest) -> dict:
    """为目标仓库启动一个白名单类型的 runner 进程。"""
    settings = load_fresh_agent_runner_settings()
    try:
        record = start_runner_process(
            repo_id=request.repo_id,
            kind=request.kind,
            contexts=_resolve_contexts(),
            supervisor=create_process_supervisor(),
            runner_command=settings.console.runner_command,
            spawn_cwd=resolve_console_spawn_cwd(),
        )
    except ConsoleProcessError as exc:
        status_code = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    _audit_process_action(
        action=f"start_{request.kind.value}",
        repo_id=request.repo_id,
        detail=f"Spawned process {record.process_id}.",
    )
    return _serialize(record)


@router.post("/agent-runner/console/processes/{process_id}/stop")
def stop_console_process(process_id: str) -> dict:
    """停止一个托管进程（SIGTERM，超时升级 SIGKILL）。"""
    settings = load_fresh_agent_runner_settings()
    try:
        record = stop_runner_process(
            process_id=process_id,
            supervisor=create_process_supervisor(),
            stop_timeout_seconds=settings.console.stop_timeout_seconds,
        )
    except ConsoleProcessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit_process_action(
        action="stop_process",
        repo_id=record.repo_id,
        detail=f"Process {process_id} -> {record.status}.",
    )
    return _serialize(record)


@router.get("/agent-runner/console/processes/{process_id}/logs")
def read_console_process_log(process_id: str, offset: int = 0) -> dict:
    """从指定偏移续读托管进程日志。"""
    try:
        chunk = tail_runner_log(
            process_id=process_id,
            offset=offset,
            supervisor=create_process_supervisor(),
        )
    except ConsoleProcessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize(chunk)


def _audit_process_action(*, action: str, repo_id: str, detail: str) -> None:
    """进程启停的审计落库（best effort）。"""
    from datetime import datetime, timezone

    from backend.core.shared.interfaces.runner_console import AuditEntry

    try:
        create_console_store().append_audit(
            AuditEntry(
                occurred_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                actor="console",
                action=action,
                repo_id=repo_id,
                issue_number=None,
                params_json="{}",
                result="accepted",
                detail=detail,
            )
        )
    except Exception as exc:  # noqa: BLE001 - audit must not break the action.
        _logger.warning("Failed to audit process action %s: %s", action, exc)


# ─────────────────────────────────────────────────────────────────────────────
# 白名单动作
# ─────────────────────────────────────────────────────────────────────────────


class RepositoryActionRequest(BaseModel):
    """仓库级动作请求体（run_once / review_once）。"""

    action: str = Field(min_length=1)


class IssueActionRequest(BaseModel):
    """Issue 级动作请求体（retry_failed / blocked_continue）。"""

    action: str = Field(min_length=1)


@router.post("/agent-runner/console/repositories/{repo_id}/actions")
def execute_console_repository_action(
    repo_id: str, request: RepositoryActionRequest
) -> dict:
    """执行仓库级白名单动作。"""
    settings = load_fresh_agent_runner_settings()
    try:
        action_result = execute_repository_action(
            action=request.action,
            repo_id=repo_id,
            contexts=_resolve_contexts(),
            supervisor=create_process_supervisor(),
            store=create_console_store(),
            runner_command=settings.console.runner_command,
            spawn_cwd=resolve_console_spawn_cwd(),
        )
    except ConsoleActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(action_result)


@router.post(
    "/agent-runner/console/repositories/{repo_id}/issues/{issue_number}/actions"
)
def execute_console_issue_action(
    repo_id: str, issue_number: int, request: IssueActionRequest
) -> dict:
    """执行 Issue 级白名单动作。"""
    settings = load_fresh_agent_runner_settings()
    try:
        action_result = execute_issue_action(
            action=request.action,
            repo_id=repo_id,
            issue_number=issue_number,
            contexts=_resolve_contexts(),
            github_client_factory=create_github_client,
            supervisor=create_process_supervisor(),
            store=create_console_store(),
            runner_command=settings.console.runner_command,
            spawn_cwd=resolve_console_spawn_cwd(),
        )
    except ConsoleActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(action_result)


# ─────────────────────────────────────────────────────────────────────────────
# 统计与审计
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/agent-runner/console/stats/overview")
def get_console_stats_overview() -> dict:
    """各仓库的实时完成度统计（GitHub 口径）。"""
    stats = build_completion_stats_overview(
        contexts=_resolve_contexts(),
        github_client_factory=create_github_client,
    )
    return {"repositories": [_serialize(entry) for entry in stats]}


@router.get("/agent-runner/console/stats/history")
def get_console_stats_history(repo_id: str | None = None, days: int = 30) -> dict:
    """本地运行历史的按天趋势（SQLite 口径）。"""
    trend = build_run_history_trend(
        store=create_console_store(), repo_id=repo_id, days=days
    )
    return {
        "repo_id": repo_id,
        "days": days,
        "trend": [_serialize(entry) for entry in trend],
    }


@router.get("/agent-runner/console/runs")
def list_console_runs(repo_id: str | None = None, limit: int = 100) -> dict:
    """倒序列出最近的运行记录。"""
    bounded_limit = min(max(limit, 1), 500)
    runs = create_console_store().list_recent_runs(repo_id=repo_id, limit=bounded_limit)
    return {"runs": [_serialize(entry) for entry in runs]}


@router.get("/agent-runner/console/audit")
def list_console_audit(limit: int = 100) -> dict:
    """倒序列出最近的审计条目。"""
    bounded_limit = min(max(limit, 1), 500)
    audits = create_console_store().list_recent_audits(limit=bounded_limit)
    return {"audits": [_serialize(entry) for entry in audits]}


# ─────────────────────────────────────────────────────────────────────────────
# 仓库 registry 管理
# ─────────────────────────────────────────────────────────────────────────────


class AddRepositoryRequest(BaseModel):
    """新增 registry 仓库条目的请求体。"""

    repo_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    display_name: str | None = None


class SetRepositoryEnabledRequest(BaseModel):
    """启停 registry 仓库条目的请求体。"""

    enabled: bool


@router.get("/agent-runner/repositories")
def list_console_repositories() -> dict:
    """列出 registry 的全部仓库条目（含路径存在性）。"""
    entries = list_registry_repositories(create_registry_editor())
    return {"repositories": [_serialize(entry) for entry in entries]}


@router.post("/agent-runner/repositories", status_code=201)
def add_console_repository(request: AddRepositoryRequest) -> dict:
    """新增一个 registry 仓库条目（校验路径存在且为 git 仓库）。"""
    try:
        entry = add_registry_repository(
            editor=create_registry_editor(),
            repo_id=request.repo_id,
            path=request.path,
            display_name=request.display_name,
        )
    except RegistryValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_process_action(
        action="registry_add",
        repo_id=request.repo_id,
        detail=f"Added repository at {entry.path}.",
    )
    return _serialize(entry)


@router.patch("/agent-runner/repositories/{repo_id}")
def set_console_repository_enabled(
    repo_id: str, request: SetRepositoryEnabledRequest
) -> dict:
    """启用或停用一个 registry 仓库条目。"""
    try:
        set_registry_repository_enabled(
            editor=create_registry_editor(),
            repo_id=repo_id,
            enabled=request.enabled,
        )
    except RegistryValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit_process_action(
        action="registry_set_enabled",
        repo_id=repo_id,
        detail=f"enabled={request.enabled}",
    )
    return {"repo_id": repo_id, "enabled": request.enabled}
