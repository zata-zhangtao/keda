"""PRD 草稿生成与人审阅确认入 pending。

工作流：

1. ``create_prd_draft``：从指定 idea refs 抽取文本，调用
   :class:`IContentGenerator` 生成完整 PRD 草稿，写入
   ``tasks/inbox/prd-drafts/<YYYYMMDD-HHMMSS>-<slug>.md``；草稿顶部
   注入结构化 metadata 块（含 ``Draft Status`` 等）。
2. ``approve_prd_draft``：校验状态仍为 ``pending-review``，按本仓库
   命名规范生成 ``tasks/pending/<PRIORITY>-<TYPE>-<YYYYMMDD-HHMMSS>-<slug>.md``，
   复制草稿正文并把 status 改为 ``approved``，同时把已批准的
   pending 路径写回 metadata。
3. 任何一步出错都 fail fast，不留半截文件。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IContentGenerator
from backend.core.shared.models.idea_inbox import (
    ApproveDraftResult,
    CreateDraftResult,
    IdeaEntry,
    IdeaInboxSource,
    PrdDraftMetadata,
    PrdDraftStatus,
    PrdDraftSummary,
)
from backend.core.use_cases.idea_inbox import (
    DRAFTS_DIR_NAME,
    IDEAS_DIR_NAME,
    append_idea,
    atomic_write_text,
    build_excerpt,
    ensure_inbox_layout,
    extract_draft_title,
    parse_draft_header,
    read_idea_inbox,
    read_text,
)

_logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 数据 + 异常
# ─────────────────────────────────────────────────────────────────────────────


class IdeaInboxError(ValueError):
    """Idea Inbox 操作错误（签名/状态/参数非法）。"""


@dataclass(frozen=True)
class _DraftPayload:
    """从 use case 内部贯穿到落盘的草稿 payload。"""

    draft_id: str
    repo_id: str
    priority: str
    prd_type: str
    idea_refs: tuple[str, ...]
    title: str
    body: str
    created_at: str


# ─────────────────────────────────────────────────────────────────────────────
# 命名 / slug 辅助
# ─────────────────────────────────────────────────────────────────────────────


_ALLOWED_PRIORITIES = ("P0", "P1", "P2", "P3")
_ALLOWED_TYPES = (
    "FEAT",
    "BUG",
    "CHORE",
    "DOC",
    "REFACTOR",
    "TEST",
    "PERF",
    "SEC",
)


def _normalize_slug(slug: str) -> str:
    """归一化 slug：转小写、替换非字母数字为 -、去首尾 -。"""
    lowered = slug.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "idea-draft"


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_priority(priority: str) -> str:
    normalized = priority.strip().upper()
    if normalized not in _ALLOWED_PRIORITIES:
        raise IdeaInboxError(f"priority 必须是 {_ALLOWED_PRIORITIES} 之一，得到 '{priority}'。")
    return normalized


def _validate_prd_type(prd_type: str) -> str:
    normalized = prd_type.strip().upper()
    if normalized not in _ALLOWED_TYPES:
        raise IdeaInboxError(f"type 必须是 {_ALLOWED_TYPES} 之一，得到 '{prd_type}'。")
    return normalized


def _draft_filename(draft_id: str, slug: str) -> str:
    return f"{draft_id}-{_normalize_slug(slug)}.md"


def _pending_filename(priority: str, prd_type: str, draft_id: str, slug: str) -> str:
    return f"{priority}-{prd_type}-{draft_id}-{_normalize_slug(slug)}.md"


# ─────────────────────────────────────────────────────────────────────────────
# Idea 文本提取
# ─────────────────────────────────────────────────────────────────────────────


def _select_idea_texts(repo_path: Path, idea_refs: tuple[str, ...]) -> tuple[IdeaEntry, ...]:
    """根据 idea_refs 选取想法；缺失时报错。"""
    snapshot = read_idea_inbox(repo_path)
    by_id = {entry.entry_id: entry for entry in snapshot.entries}
    selected: list[IdeaEntry] = []
    for ref in idea_refs:
        entry = by_id.get(ref)
        if entry is None:
            raise IdeaInboxError(f"未找到 idea_ref '{ref}' 对应的想法。")
        selected.append(entry)
    if not selected:
        raise IdeaInboxError("idea_refs 至少需要包含一个有效的想法 id。")
    return tuple(selected)


def _build_idea_aggregated_text(entries: tuple[IdeaEntry, ...]) -> str:
    """把多个想法拼成一段供 LLM 使用的原话。"""
    blocks: list[str] = []
    for entry in entries:
        blocks.append(
            f"### 想法 {entry.entry_id}（{entry.occurred_at} · "
            f"{entry.source.value} · {entry.author}）\n\n{entry.text}\n"
        )
    return "\n".join(blocks).strip()


def _derive_draft_slug(entries: tuple[IdeaEntry, ...], title: str) -> str:
    """从首条想法/标题派生 slug。"""
    if title:
        candidate = re.sub(r"\s+", "-", title.strip())[:40]
        normalized = _normalize_slug(candidate)
        if normalized and normalized != "idea-draft":
            return normalized
    first = entries[0].text
    snippet = re.sub(r"\s+", "-", first.strip())[:40]
    return _normalize_slug(snippet)


# ─────────────────────────────────────────────────────────────────────────────
# 草稿生成
# ─────────────────────────────────────────────────────────────────────────────


_DRAFT_GENERATION_PROMPT_TEMPLATE = """你是 PRD 写作助手。下面是用户最近积累的想法原话（来自 tasks/inbox/ideas.md 的 append-only 摘录）：

<ideas>
{ideas_aggregated}
</ideas>

请基于这些想法起草一份符合本仓库 PRD 标准的草稿。结构必须包含：

1. 顶部 H1 标题 `# PRD: <人类可读标题>`，中文优先，英文次之。
2. 简短的引言（问题陈述 + 推荐方案概述）。
3. 至少 5 条 Acceptance Checklist 条目（Markdown 复选框 `- [ ]`）。
4. 至少一个 H2 章节说明实现思路或模块划分。
5. 不得修改想法原话，PR 草稿需保留"待人审阅"的语气。

输出格式：直接给出完整 Markdown 文本，不要任何额外说明、不要 Markdown 代码块包裹。
"""


def _build_draft_body(
    *,
    idea_aggregated: str,
    generator: IContentGenerator | None,
    agent_name: str,
    cwd: Path,
    timeout_seconds: int,
) -> str:
    """调用 generator 生成草稿正文；generator 不可用时返回 fallback 模板。"""
    prompt = _DRAFT_GENERATION_PROMPT_TEMPLATE.format(ideas_aggregated=idea_aggregated)
    if generator is None:
        return _fallback_draft_body(idea_aggregated)
    result = generator.generate(
        agent_name=agent_name,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout_seconds,
    )
    if result.return_code != 0 or not result.stdout.strip():
        _logger.warning(
            "Idea draft generator exited with code %s; using fallback body.",
            result.return_code,
        )
        return _fallback_draft_body(idea_aggregated)
    return result.stdout.strip()


def _fallback_draft_body(idea_aggregated: str) -> str:
    """generator 不可用时的兜底草稿（结构简单、留待人补全）。"""
    excerpt = idea_aggregated.strip().splitlines()[:6]
    return (
        "# PRD: 基于想法的草稿（待人补全）\n\n"
        "## 1. Introduction & Goals\n\n"
        "本草稿由 append-only 想法生成，缺少完整的 AI 总结。请基于下列原话补全。\n\n"
        "### 想法原话摘录\n\n" + "\n".join(excerpt) + "\n\n## 2. Acceptance Checklist\n\n"
        "- [ ] 阅读 `tasks/inbox/ideas.md` 完整原话并补全上下文。\n"
        "- [ ] 填写 Introduction / Goals 章节。\n"
        "- [ ] 描述实现方案与模块划分。\n"
        "- [ ] 列出至少 5 条可执行验收条目。\n"
        "- [ ] 由人审阅后决定是否落 pending。\n"
    )


def _render_draft_text(payload: _DraftPayload) -> str:
    """拼接草稿文件完整内容（metadata 头 + body）。"""
    idea_refs_value = ", ".join(payload.idea_refs)
    return (
        "---\n"
        f"Draft ID: {payload.draft_id}\n"
        f"Draft Status: pending-review\n"
        f"Repo ID: {payload.repo_id}\n"
        f"Priority: {payload.priority}\n"
        f"Type: {payload.prd_type}\n"
        f"Created At: {payload.created_at}\n"
        f"Source Idea Refs: {idea_refs_value}\n"
        "---\n\n"
        f"{payload.body.strip()}\n"
    )


def create_prd_draft(
    repo_path: Path,
    *,
    idea_refs: tuple[str, ...],
    generator: IContentGenerator | None,
    agent_name: str = "codex",
    cwd: Path | None = None,
    priority: str = "P2",
    prd_type: str = "FEAT",
    timeout_seconds: int = 600,
) -> CreateDraftResult:
    """根据 idea_refs 生成 PRD 草稿并写入 ``prd-drafts/``。"""
    ensure_inbox_layout(repo_path)
    repo_resolved = repo_path.resolve()
    if not idea_refs:
        raise IdeaInboxError("idea_refs 不能为空。")
    priority = _validate_priority(priority)
    prd_type = _validate_prd_type(prd_type)

    selected = _select_idea_texts(repo_resolved, idea_refs)
    aggregated = _build_idea_aggregated_text(selected)
    body = _build_draft_body(
        idea_aggregated=aggregated,
        generator=generator,
        agent_name=agent_name,
        cwd=cwd or repo_resolved,
        timeout_seconds=timeout_seconds,
    )
    title = extract_draft_title(body)
    draft_id = _now_timestamp()
    slug = _derive_draft_slug(selected, title)
    payload = _DraftPayload(
        draft_id=draft_id,
        repo_id=repo_resolved.name,
        priority=priority,
        prd_type=prd_type,
        idea_refs=idea_refs,
        title=title,
        body=body,
        created_at=_now_iso(),
    )
    draft_filename = _draft_filename(draft_id, slug)
    drafts_dir = repo_resolved / IDEAS_DIR_NAME / DRAFTS_DIR_NAME
    draft_path = drafts_dir / draft_filename
    atomic_write_text(draft_path, _render_draft_text(payload))
    metadata = PrdDraftMetadata(
        draft_id=draft_id,
        status=PrdDraftStatus.PENDING_REVIEW,
        repo_id=payload.repo_id,
        source_idea_refs=idea_refs,
        priority=priority,
        prd_type=prd_type,
        created_at=payload.created_at,
        approved_pending_path=None,
    )
    summary = PrdDraftSummary(
        metadata=metadata,
        draft_path=str(draft_path.relative_to(repo_resolved)),
        title=title,
        body_excerpt=build_excerpt(body),
    )
    _logger.info("Created PRD draft %s for repo %s", draft_path, repo_resolved.name)
    return CreateDraftResult(draft=summary, draft_path=str(draft_path.relative_to(repo_resolved)))


# ─────────────────────────────────────────────────────────────────────────────
# 草稿确认入 pending
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_draft_path(repo_path_resolved: Path, draft_relpath: str) -> Path:
    """解析草稿相对路径并阻止越界。``repo_path_resolved`` 必须是已 ``resolve`` 的绝对路径。"""
    if draft_relpath.startswith("/") or ".." in Path(draft_relpath).parts:
        raise IdeaInboxError(f"非法的草稿路径: {draft_relpath}")
    drafts_dir = (repo_path_resolved / IDEAS_DIR_NAME / DRAFTS_DIR_NAME).resolve()
    target = (repo_path_resolved / draft_relpath).resolve()
    if drafts_dir not in target.parents:
        raise IdeaInboxError("草稿路径必须位于 prd-drafts/ 目录内。")
    if not target.exists() or not target.is_file():
        raise IdeaInboxError(f"草稿文件不存在: {draft_relpath}")
    return target


def _ensure_pending_path_free(repo_path: Path, pending_filename: str) -> Path:
    pending_dir = repo_path / "tasks" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    pending_path = pending_dir / pending_filename
    if pending_path.exists():
        raise IdeaInboxError(f"pending 文件已存在，拒绝覆盖: tasks/pending/{pending_filename}")
    return pending_path


def _replace_metadata_status(
    text: str, *, new_status: PrdDraftStatus, approved_path: str | None
) -> str:
    """把草稿文本里的 status 字段改写为 approved / rejected。

    ``Approved Pending Path`` 始终紧跟 metadata 块内的最后一行之后、
    闭合 ``---`` 之前，保证 metadata 块整体结构稳定，便于重新解析。
    """
    updated = re.sub(
        r"^Draft Status:\s*.+$",
        f"Draft Status: {new_status.value}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if approved_path is None:
        return updated
    # 先把已有的 Approved Pending Path 行去掉。
    updated = re.sub(
        r"^Approved Pending Path:\s*.+$\n?",
        "",
        updated,
        count=1,
        flags=re.MULTILINE,
    )
    # 找到 metadata 块的闭合 ``---`` 行（同一文件里的第二个 ``---\n``），
    # 在它之前插入新行。
    pattern = re.compile(r"^---\s*$", re.MULTILINE)
    matches = list(pattern.finditer(updated))
    if len(matches) < 2:
        # 找不到完整 metadata 块，fallback 到追加到开头（不破坏结构）。
        return f"Approved Pending Path: {approved_path}\n" + updated
    closing = matches[-1]
    insert_pos = closing.start()
    return updated[:insert_pos] + f"Approved Pending Path: {approved_path}\n" + updated[insert_pos:]


def approve_prd_draft(
    repo_path: Path,
    *,
    draft_relpath: str,
    priority: str | None = None,
    prd_type: str | None = None,
) -> ApproveDraftResult:
    """把 ``prd-drafts/...`` 草稿复制到 ``tasks/pending/...`` 并标 approved。

    命名规则：``<PRIORITY>-<TYPE>-<YYYYMMDD-HHMMSS>-<slug>.md``。如
    果目标 pending 文件已存在或草稿已非 pending-review，则 fail fast。
    """
    ensure_inbox_layout(repo_path)
    repo_resolved = repo_path.resolve()
    draft_path = _resolve_draft_path(repo_resolved, draft_relpath)
    text = read_text(draft_path)
    metadata = parse_draft_header(text)
    if metadata is None:
        raise IdeaInboxError(f"草稿缺少 metadata 块: {draft_relpath}")
    if metadata.status is not PrdDraftStatus.PENDING_REVIEW:
        raise IdeaInboxError(f"草稿状态为 {metadata.status.value}，仅 pending-review 可批准。")
    effective_priority = _validate_priority(priority or metadata.priority)
    effective_type = _validate_prd_type(prd_type or metadata.prd_type)

    # 草稿文件名格式：<YYYYMMDD>-<HHMMSS>-<slug>.md，前两段是时间戳
    # 后面的全部是 slug，合并后再规范化。
    stem_parts = draft_path.stem.split("-")
    slug = "-".join(stem_parts[2:]) if len(stem_parts) > 2 else draft_path.stem
    slug = _normalize_slug(slug)
    pending_filename = _pending_filename(
        effective_priority, effective_type, metadata.draft_id, slug
    )
    pending_path = _ensure_pending_path_free(repo_resolved, pending_filename)
    body = text
    # 在复制到 pending 之前先去掉 metadata 头（pending 文件不再需要）。
    metadata_block_end = body.find("---", body.find("---") + 3)
    if metadata_block_end != -1:
        body_only = body[metadata_block_end + 3 :].lstrip()
    else:
        body_only = body

    pending_content = (
        f"# PRD: {extract_draft_title(body_only) or metadata.draft_id}\n\n"
        f"- Draft ID: {metadata.draft_id}\n"
        f"- Source Idea Refs: {', '.join(metadata.source_idea_refs)}\n"
        f"- Priority: {effective_priority}\n"
        f"- Type: {effective_type}\n\n"
        f"{body_only.strip()}\n"
    )
    atomic_write_text(pending_path, pending_content)

    approved_relpath = f"tasks/pending/{pending_filename}"
    updated_text = _replace_metadata_status(
        text,
        new_status=PrdDraftStatus.APPROVED,
        approved_path=approved_relpath,
    )
    atomic_write_text(draft_path, updated_text)

    title = extract_draft_title(pending_content)
    new_metadata = PrdDraftMetadata(
        draft_id=metadata.draft_id,
        status=PrdDraftStatus.APPROVED,
        repo_id=metadata.repo_id,
        source_idea_refs=metadata.source_idea_refs,
        priority=effective_priority,
        prd_type=effective_type,
        created_at=metadata.created_at,
        approved_pending_path=approved_relpath,
    )
    summary = PrdDraftSummary(
        metadata=new_metadata,
        draft_path=str(draft_path.relative_to(repo_resolved)),
        title=title,
        body_excerpt=build_excerpt(pending_content),
    )
    _logger.info("Approved PRD draft %s -> %s", draft_path, pending_path)
    return ApproveDraftResult(
        draft=summary,
        pending_path=approved_relpath,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 入口辅助
# ─────────────────────────────────────────────────────────────────────────────


def append_idea_via_inbound(
    repo_path: Path,
    *,
    text: str,
    sender: str,
    occurred_at: str | None,
) -> str:
    """外部入口（飞书 / 自定义 webhook）追加想法的薄包装。返回 entry_id。"""
    result = append_idea(
        repo_path,
        source=IdeaInboxSource.FEISHU if sender.startswith("feishu:") else IdeaInboxSource.INBOUND,
        author=sender or "anonymous",
        text=text,
        occurred_at=occurred_at,
    )
    return result.entry.entry_id
