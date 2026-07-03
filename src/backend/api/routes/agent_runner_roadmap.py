"""Roadmap API for PRD orchestration.

Provides read endpoints for PRD state/dependencies and write endpoints for
single/global start actions.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core.shared.interfaces.runner_console import AuditEntry, IRoadmapStore
from backend.core.shared.models.roadmap import (
    RoadmapDependency,
    RoadmapDependencyKind,
    RoadmapPrd,
    RoadmapSettingsEntry,
)
from backend.core.use_cases.roadmap_actions import (
    RoadmapActionError,
    get_or_create_roadmap_settings,
    start_global_roadmap,
    start_prd,
    stop_global_roadmap,
)
from backend.core.use_cases.roadmap_dependencies import evaluate_roadmap_dependencies
from backend.core.use_cases.roadmap_prd_scanner import scan_roadmap_prds
from backend.core.use_cases.roadmap_state_resolver import resolve_roadmap_states
from backend.engines.agent_runner.factory import (
    create_github_client,
    create_process_runner,
    create_process_supervisor,
    create_roadmap_store,
    load_fresh_agent_runner_settings,
    resolve_console_spawn_cwd,
    resolve_repository_targets_with_diagnostics,
)

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-runner-roadmap"])

_ROADMAP_CACHE: dict[str, Any] = {}
_ROADMAP_CACHE_TTL_SECONDS = 30
_cache_lock = threading.Lock()


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
    """Resolve enabled repository contexts with diagnostics."""
    settings = load_fresh_agent_runner_settings()
    contexts, _failures = resolve_repository_targets_with_diagnostics(settings)
    return contexts


def _resolve_context(repo_id: str):
    """Return a single enabled repository context."""
    for context in _resolve_contexts():
        if context.repo_id == repo_id:
            return context
    raise HTTPException(status_code=400, detail=f"仓库 '{repo_id}' 不存在或未启用。")


def _encode_prd_path(prd_path: str) -> str:
    """Encode a relative PRD path for safe URL use."""
    return base64.urlsafe_b64encode(prd_path.encode("utf-8")).decode("ascii")


def _decode_prd_path(encoded_path: str) -> str:
    """Decode a URL-safe base64 PRD path."""
    try:
        return base64.urlsafe_b64decode(encoded_path.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="非法的 PRD 路径编码。") from exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(
    store: IRoadmapStore,
    *,
    action: str,
    repo_id: str,
    prd_path: str,
    issue_number: int | None,
    result: str,
    detail: str,
) -> None:
    """Best-effort audit logging for roadmap actions."""
    try:
        store.append_audit(
            AuditEntry(
                occurred_at=_now_iso(),
                actor="roadmap",
                action=action,
                repo_id=repo_id,
                issue_number=issue_number,
                params_json=f'{{"prd_path": "{prd_path}"}}',
                result=result,
                detail=detail,
            )
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Failed to audit roadmap action %s: %s", action, exc)


def _build_roadmap_response(
    repo_id: str,
    include_archived: bool,
) -> dict:
    """Scan PRDs and resolve live GitHub state."""
    context = _resolve_context(repo_id)
    github_client = create_github_client(context.repo_path)
    prds = scan_roadmap_prds(context.repo_path, include_archived=include_archived)
    block_reasons = evaluate_roadmap_dependencies(
        prds,
        github_client=github_client,
        labels_config=context.config.labels,
    )
    resolved = resolve_roadmap_states(
        prds,
        github_client=github_client,
        config=context.config,
        block_reasons=block_reasons,
    )
    # Enrich dependency targets with current issue numbers for the UI.
    prd_issue_map = {prd.prd_path: prd.issue_number for prd in resolved}
    enriched: list[RoadmapPrd] = []
    for prd in resolved:
        deps: list[RoadmapDependency] = []
        for dep in prd.delivery_dependencies:
            if dep.kind is RoadmapDependencyKind.PRD:
                issue_number = prd_issue_map.get(dep.to_path)
                detail = f"{dep.to_path}"
                if issue_number:
                    detail += f" (#{issue_number})"
                deps.append(
                    RoadmapDependency(
                        from_path=dep.from_path,
                        to_path=dep.to_path,
                        kind=dep.kind,
                        detail=detail,
                    )
                )
            else:
                deps.append(dep)
        enriched.append(
            RoadmapPrd(
                prd_path=prd.prd_path,
                title=prd.title,
                status=prd.status,
                priority=prd.priority,
                issue_url=prd.issue_url,
                issue_number=prd.issue_number,
                state=prd.state,
                acceptance_total=prd.acceptance_total,
                acceptance_checked=prd.acceptance_checked,
                delivery_dependencies=tuple(deps),
                updated_at=prd.updated_at,
                block_reason=prd.block_reason,
                next_action=prd.next_action,
            )
        )
    return {
        "prds": [_serialize(p) for p in enriched],
        "repo_id": repo_id,
        "include_archived": include_archived,
        "scanned_at": _now_iso(),
    }


def _get_cached_roadmap_response(repo_id: str, include_archived: bool) -> dict:
    """Return cached roadmap response or rebuild it."""
    cache_key = f"{repo_id}:archived={include_archived}"
    now = time.time()
    with _cache_lock:
        entry = _ROADMAP_CACHE.get(cache_key)
        if entry and (now - entry["timestamp"]) < _ROADMAP_CACHE_TTL_SECONDS:
            return entry["payload"]
    payload = _build_roadmap_response(repo_id, include_archived)
    with _cache_lock:
        _ROADMAP_CACHE[cache_key] = {"payload": payload, "timestamp": now}
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Read endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/agent-runner/roadmap/prds")
def list_roadmap_prds(repo_id: str, include_archived: bool = False) -> dict:
    """列出 PRD 路线图，包含依赖与状态。"""
    return _get_cached_roadmap_response(repo_id, include_archived)


@router.get("/agent-runner/roadmap/settings")
def get_roadmap_settings(repo_id: str) -> dict:
    """读取 roadmap 设置。"""
    store = create_roadmap_store()
    settings = get_or_create_roadmap_settings(store, repo_id)
    return _serialize(settings)


# ─────────────────────────────────────────────────────────────────────────────
# Write endpoints
# ─────────────────────────────────────────────────────────────────────────────


class UpdateSettingsRequest(BaseModel):
    """更新 roadmap 用户设置。"""

    max_parallel: int = Field(default=2, ge=1, le=10)
    default_view: str = Field(default="list", pattern="^(timeline|list)$")


@router.patch("/agent-runner/roadmap/settings")
def update_roadmap_settings(repo_id: str, request: UpdateSettingsRequest) -> dict:
    """更新 roadmap 并发数与默认视图。"""
    store = create_roadmap_store()
    settings = RoadmapSettingsEntry(
        repo_id=repo_id,
        max_parallel=request.max_parallel,
        default_view=request.default_view,
        updated_at=_now_iso(),
    )
    try:
        store.save_roadmap_settings(settings)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"保存设置失败: {exc}") from exc
    return _serialize(settings)


class StartPrdRequest(BaseModel):
    """单个 PRD 开始请求。"""

    repo_id: str = Field(min_length=1)


@router.post("/agent-runner/roadmap/prds/{encoded_path}/start")
def start_roadmap_prd(encoded_path: str, request: StartPrdRequest) -> dict:
    """开始单个 PRD：创建 Issue（若需要）、添加 ready、启动 runner。"""
    prd_path = _decode_prd_path(encoded_path)
    settings = load_fresh_agent_runner_settings()
    contexts = _resolve_contexts()
    store = create_roadmap_store()
    try:
        result = start_prd(
            prd_path=prd_path,
            repo_id=request.repo_id,
            contexts=contexts,
            github_client=create_github_client(_resolve_context(request.repo_id).repo_path),
            supervisor=create_process_supervisor(),
            store=store,
            runner_command=settings.console.runner_command,
            spawn_cwd=resolve_console_spawn_cwd(),
            process_runner=create_process_runner(),
        )
    except RoadmapActionError as exc:
        _audit(
            store,
            action="start_prd",
            repo_id=request.repo_id,
            prd_path=prd_path,
            issue_number=None,
            result="error",
            detail=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Invalidate cache for this repo so the next read reflects the new state.
    _ROADMAP_CACHE.pop(f"{request.repo_id}:archived=False", None)
    return _serialize(result)


class StartGlobalRequest(BaseModel):
    """全局开始请求。"""

    repo_id: str = Field(min_length=1)
    max_parallel: int = Field(default=2, ge=1, le=10)


@router.post("/agent-runner/roadmap/start-global")
def start_roadmap_global(request: StartGlobalRequest) -> dict:
    """按并发上限批量开始无依赖的 pending PRD。"""
    settings = load_fresh_agent_runner_settings()
    contexts = _resolve_contexts()
    store = create_roadmap_store()
    try:
        result = start_global_roadmap(
            repo_id=request.repo_id,
            max_parallel=request.max_parallel,
            contexts=contexts,
            github_client_factory=create_github_client,
            supervisor=create_process_supervisor(),
            store=store,
            runner_command=settings.console.runner_command,
            spawn_cwd=resolve_console_spawn_cwd(),
            process_runner=create_process_runner(),
        )
    except RoadmapActionError as exc:
        _audit(
            store,
            action="start_global",
            repo_id=request.repo_id,
            prd_path="",
            issue_number=None,
            result="error",
            detail=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _ROADMAP_CACHE.pop(f"{request.repo_id}:archived=False", None)
    return _serialize(result)


class StopGlobalRequest(BaseModel):
    """停止全局调度请求。"""

    repo_id: str = Field(min_length=1)


@router.post("/agent-runner/roadmap/stop-global")
def stop_roadmap_global(request: StopGlobalRequest) -> dict:
    """清空 roadmap 队列，已运行的不中断。"""
    store = create_roadmap_store()
    try:
        return stop_global_roadmap(repo_id=request.repo_id, store=store)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"停止全局调度失败: {exc}") from exc
