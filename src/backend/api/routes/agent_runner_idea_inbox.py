"""Idea Inbox API（受控 /api/v1/agent-runner/idea-inbox/*）。

本路由承载本地前端与外部 IM 入口（飞书、自定义 webhook 等）共用
的 Idea Inbox 操作：

- repository snapshot / append idea / refresh summary
- 生成 PRD 草稿 / 草稿确认入 pending
- 通用签名 inbound endpoint

签名 secret 来自环境变量 ``IAR_IDEA_INBOX_INBOUND_SECRET``，不写入
``config.toml`` / ``.iar.toml``。所有 DTO 转换、HTTP 状态码与签名校
验在本模块完成；append-only 约束、状态机与命名规范在 core use case。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from backend.core.shared.interfaces.agent_runner import IContentGenerator
from backend.core.shared.models.idea_inbox import (
    ApproveDraftResult,
    CreateDraftResult,
    IdeaInboxSource,
)
from backend.core.use_cases import idea_prd_drafts as idea_prd_drafts_module
from backend.core.use_cases.idea_inbox import (
    append_idea,
    read_idea_inbox,
    refresh_idea_summary,
)
from backend.core.use_cases.idea_prd_drafts import (
    IdeaInboxError,
    approve_prd_draft,
    create_prd_draft,
)
from backend.engines.agent_runner.factory import (
    create_content_generator,
    load_fresh_agent_runner_settings,
    resolve_repository_targets_with_diagnostics,
)

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-runner-idea-inbox"])

INBOUND_SECRET_ENV = "IAR_IDEA_INBOX_INBOUND_SECRET"
INBOUND_SIGNATURE_HEADER = "X-IAR-Signature"


# ─────────────────────────────────────────────────────────────────────────────
# 序列化 + 上下文解析
# ─────────────────────────────────────────────────────────────────────────────


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_contexts() -> list:
    settings = load_fresh_agent_runner_settings()
    contexts, _failures = resolve_repository_targets_with_diagnostics(settings)
    return contexts


def _resolve_context(repo_id: str):
    for context in _resolve_contexts():
        if context.repo_id == repo_id:
            return context
    raise HTTPException(status_code=400, detail=f"仓库 '{repo_id}' 不存在或未启用。")


# ─────────────────────────────────────────────────────────────────────────────
# 请求 / 响应模型
# ─────────────────────────────────────────────────────────────────────────────


class AppendIdeaRequest(BaseModel):
    """前端追加想法的请求体。"""

    text: str = Field(min_length=1)
    author: str = Field(default="anonymous", min_length=1, max_length=120)
    occurred_at: str | None = Field(default=None)


class RefreshSummaryRequest(BaseModel):
    """刷新 summary 的请求体。"""

    summary_text: str = Field(min_length=1)
    source_label: str = Field(default="agent", min_length=1, max_length=32)


class CreateDraftRequest(BaseModel):
    """生成 PRD 草稿的请求体。"""

    idea_refs: list[str] = Field(min_length=1)
    priority: str = Field(default="P2")
    prd_type: str = Field(default="FEAT")
    agent_name: str = Field(default="codex")
    timeout_seconds: int = Field(default=600, ge=10, le=3600)


class ApproveDraftRequest(BaseModel):
    """确认草稿的请求体（priority / type 可覆盖 metadata）。"""

    priority: str | None = Field(default=None)
    prd_type: str | None = Field(default=None)


class InboundMessage(BaseModel):
    """外部 IM / webhook 通用入站消息。"""

    provider: str = Field(default="manual", min_length=1, max_length=32)
    repo_id: str = Field(min_length=1, max_length=120)
    sender: str = Field(default="anonymous", min_length=1, max_length=120)
    text: str = Field(min_length=1)
    occurred_at: str | None = Field(default=None)
    # 可选：把消息直接转成 PRD 草稿。
    draft_priority: str | None = Field(default=None)
    draft_type: str | None = Field(default=None)
    draft_idea_refs: list[str] | None = Field(default=None)


# ─────────────────────────────────────────────────────────────────────────────
# Repository snapshot
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/agent-runner/idea-inbox/repositories/{repo_id}")
def get_idea_inbox(repo_id: str) -> dict:
    """读取目标仓库的 Idea Inbox 完整快照。"""
    context = _resolve_context(repo_id)
    snapshot = read_idea_inbox(context.repo_path)
    return _serialize(snapshot)


@router.post(
    "/agent-runner/idea-inbox/repositories/{repo_id}/ideas",
    status_code=201,
)
def append_idea_to_repo(repo_id: str, request: AppendIdeaRequest) -> dict:
    """append 一条想法到 ``ideas.md``。"""
    context = _resolve_context(repo_id)
    try:
        result = append_idea(
            context.repo_path,
            source=IdeaInboxSource.FRONTEND,
            author=request.author,
            text=request.text,
            occurred_at=request.occurred_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(result)


@router.post("/agent-runner/idea-inbox/repositories/{repo_id}/summary/refresh")
def refresh_repo_summary(repo_id: str, request: RefreshSummaryRequest) -> dict:
    """重写 ``summary.md``。"""
    context = _resolve_context(repo_id)
    result = refresh_idea_summary(
        context.repo_path,
        summary_text=request.summary_text,
        source_label=request.source_label,
    )
    return _serialize(result)


# ─────────────────────────────────────────────────────────────────────────────
# 草稿生成 / 确认
# ─────────────────────────────────────────────────────────────────────────────


def _get_content_generator() -> IContentGenerator | None:
    """装配 content generator。失败时返回 None 让草稿走 fallback。"""
    try:
        return create_content_generator()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Failed to build content generator: %s", exc)
        return None


@router.post(
    "/agent-runner/idea-inbox/repositories/{repo_id}/drafts",
    status_code=201,
)
def create_repo_draft(repo_id: str, request: CreateDraftRequest) -> dict:
    """生成 PRD 草稿到 ``tasks/inbox/prd-drafts/``。"""
    context = _resolve_context(repo_id)
    generator = _get_content_generator()
    try:
        result: CreateDraftResult = create_prd_draft(
            context.repo_path,
            idea_refs=tuple(request.idea_refs),
            generator=generator,
            agent_name=request.agent_name,
            cwd=context.repo_path,
            priority=request.priority,
            prd_type=request.prd_type,
            timeout_seconds=request.timeout_seconds,
        )
    except IdeaInboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(result)


@router.post("/agent-runner/idea-inbox/repositories/{repo_id}/drafts/{encoded_path:path}/approve")
def approve_repo_draft(repo_id: str, encoded_path: str, request: ApproveDraftRequest) -> dict:
    """确认草稿落入 ``tasks/pending/``。"""
    context = _resolve_context(repo_id)
    draft_path = _decode_draft_path(encoded_path)
    try:
        result: ApproveDraftResult = approve_prd_draft(
            context.repo_path,
            draft_relpath=draft_path,
            priority=request.priority,
            prd_type=request.prd_type,
        )
    except IdeaInboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(result)


# ─────────────────────────────────────────────────────────────────────────────
# 外部 IM / webhook inbound
# ─────────────────────────────────────────────────────────────────────────────


def _expected_signature(secret: str, body: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _decode_draft_path(encoded_path: str) -> str:
    """URL-safe base64 解码草稿相对路径。"""
    import base64

    try:
        return base64.urlsafe_b64decode(encoded_path.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="非法的草稿路径编码。") from exc


def _verify_signature(
    body: str,
    signature_header: str | None,
    secret: str | None,
) -> None:
    """校验 HMAC SHA256 签名。缺失 secret 或签名不匹配都拒绝。"""
    if not secret:
        raise HTTPException(
            status_code=503,
            detail=(
                f"inbound endpoint 未配置共享 secret " f"({INBOUND_SECRET_ENV} 为空)，拒绝接收。"
            ),
        )
    if not signature_header:
        raise HTTPException(status_code=401, detail="缺少签名 header。")
    expected = _expected_signature(secret, body)
    if not hmac.compare_digest(expected, signature_header.strip()):
        raise HTTPException(status_code=401, detail="签名校验失败。")


def _resolve_source(provider: str) -> IdeaInboxSource:
    normalized = provider.strip().lower()
    if normalized in {s.value for s in IdeaInboxSource}:
        return IdeaInboxSource(normalized)
    return IdeaInboxSource.INBOUND


@router.post("/agent-runner/idea-inbox/inbound", status_code=202)
async def inbound_idea(
    request: Request,
    x_iar_signature: str | None = Header(default=None, alias=INBOUND_SIGNATURE_HEADER),
) -> dict:
    """外部 IM / webhook 入口。

    必须：

    1. 携带 ``X-IAR-Signature`` HMAC SHA256 签名（基于 raw request body）。
    2. 显式携带 ``repo_id``，且该 repo 必须在 registry 中 enabled。
    3. ``text`` 非空。

    签名计算方式::

        body = <raw HTTP request body, UTF-8>
        signature = "sha256=" + HMAC_SHA256(secret, body).hexdigest()

    发送方需要保证 body 在 HMAC 计算时与发送的 body 字节级一致
    （不要重新序列化后再签名）。
    """
    raw_body_bytes = await request.body()
    raw_body = raw_body_bytes.decode("utf-8")
    secret = os.environ.get(INBOUND_SECRET_ENV)
    _verify_signature(raw_body, x_iar_signature, secret)

    try:
        payload_dict = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"非法 JSON 负载：{exc}") from exc
    try:
        payload = InboundMessage.model_validate(payload_dict)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"非法负载：{exc}") from exc

    context = _resolve_context(payload.repo_id)
    source = _resolve_source(payload.provider)
    author = f"feishu:{payload.sender}" if source is IdeaInboxSource.FEISHU else payload.sender
    append_result = append_idea(
        context.repo_path,
        source=source,
        author=author,
        text=payload.text,
        occurred_at=payload.occurred_at,
    )
    response: dict[str, Any] = {
        "accepted": True,
        "repo_id": payload.repo_id,
        "entry_id": append_result.entry.entry_id,
        "ideas_path": append_result.ideas_path,
        "occurred_at": _now_iso(),
    }
    # 可选：直接生成 PRD 草稿（仍然需要人在前端确认才能进 pending）。
    if payload.draft_priority or payload.draft_type or payload.draft_idea_refs:
        idea_refs = tuple(payload.draft_idea_refs or (append_result.entry.entry_id,))
        generator = _get_content_generator()
        try:
            draft = create_prd_draft(
                context.repo_path,
                idea_refs=idea_refs,
                generator=generator,
                priority=payload.draft_priority or "P2",
                prd_type=payload.draft_type or "FEAT",
            )
        except IdeaInboxError as exc:
            _logger.warning("Inbound draft generation failed: %s", exc)
            response["draft_error"] = str(exc)
        else:
            response["draft"] = _serialize(draft)
    return response


# ─────────────────────────────────────────────────────────────────────────────
# 元数据查询（方便前端列出可用 priority / type 等）
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/agent-runner/idea-inbox/metadata")
def get_idea_inbox_metadata() -> dict:
    """返回草稿可用的 priority / type 等元数据。"""
    return {
        "priorities": list(idea_prd_drafts_module._ALLOWED_PRIORITIES),  # type: ignore[attr-defined]
        "prd_types": list(idea_prd_drafts_module._ALLOWED_TYPES),  # type: ignore[attr-defined]
        "inbound_signature_header": INBOUND_SIGNATURE_HEADER,
        "inbound_secret_env": INBOUND_SECRET_ENV,
    }


# Expose the constants for the openapi docs / tests.
__all__ = ["router", "INBOUND_SECRET_ENV", "INBOUND_SIGNATURE_HEADER"]
