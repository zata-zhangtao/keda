"""Idea Inbox 文件事实源读写（append-only + 可重写 summary）。

约束：

- ``tasks/inbox/ideas.md`` 只追加，绝不重写已有内容；append 的时间
  块采用 ``## YYYY-MM-DD HH:MM`` 形式，与现有 idea-inbox skill 兼容。
- ``tasks/inbox/summary.md`` 是 AI 派生的可重写文件，每次刷新整文覆
  盖，并在顶部标注「事实以 ideas.md 为准」。
- 草稿目录 ``tasks/inbox/prd-drafts/`` 由 :mod:`idea_prd_drafts` 维
  护，本模块只负责路径解析与目录确认。
- 文本 I/O 全部显式 ``encoding="utf-8"``。
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from backend.core.shared.models.idea_inbox import (
    AppendIdeaResult,
    IdeaEntry,
    IdeaInboxPaths,
    IdeaInboxSnapshot,
    IdeaInboxSource,
    PrdDraftMetadata,
    PrdDraftStatus,
    PrdDraftSummary,
    RefreshSummaryResult,
)

_logger = logging.getLogger(__name__)


IDEAS_DIR_NAME = "tasks/inbox"
IDEAS_FILE_NAME = "ideas.md"
SUMMARY_FILE_NAME = "summary.md"
DRAFTS_DIR_NAME = "prd-drafts"

_IDEA_HEADER_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})(?:\s+·\s+(?P<tag>.+?))?\s*$")

_SUMMARY_HEADER = (
    "# Idea Inbox — AI 总结\n\n"
    "> 本文件由 AI 派生。**事实来源是 `tasks/inbox/ideas.md`**，"
    "本文件可被重写。\n\n"
)


def resolve_inbox_paths(repo_path: Path) -> IdeaInboxPaths:
    """返回目标仓库的 Idea Inbox 三个相对路径。"""
    return IdeaInboxPaths(
        ideas_file=f"{IDEAS_DIR_NAME}/{IDEAS_FILE_NAME}",
        summary_file=f"{IDEAS_DIR_NAME}/{SUMMARY_FILE_NAME}",
        drafts_dir=f"{IDEAS_DIR_NAME}/{DRAFTS_DIR_NAME}",
    )


def _abs_ideas_path(repo_path: Path) -> Path:
    return repo_path / IDEAS_DIR_NAME / IDEAS_FILE_NAME


def _abs_summary_path(repo_path: Path) -> Path:
    return repo_path / IDEAS_DIR_NAME / SUMMARY_FILE_NAME


def _abs_drafts_dir(repo_path: Path) -> Path:
    return repo_path / IDEAS_DIR_NAME / DRAFTS_DIR_NAME


def _ensure_inbox_layout(repo_path: Path) -> None:
    """确保 ``tasks/inbox`` 与 ``prd-drafts/`` 目录存在。"""
    (repo_path / IDEAS_DIR_NAME).mkdir(parents=True, exist_ok=True)
    _abs_drafts_dir(repo_path).mkdir(parents=True, exist_ok=True)


def ensure_inbox_layout(repo_path: Path) -> None:
    """公共入口：保证 inbox 目录与草稿目录存在。"""
    _ensure_inbox_layout(repo_path)


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def read_text(path: Path) -> str:
    """公共入口：读取文本文件，缺失返回空字符串。"""
    return _read_text(path)


def _atomic_write_text(path: Path, content: str) -> None:
    """写文件，原子替换：先写临时文件再 rename，避免半截状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def atomic_write_text(path: Path, content: str) -> None:
    """公共入口：原子写入文本文件。"""
    _atomic_write_text(path, content)


def _new_entry_id() -> str:
    """生成全局唯一的想法 entry id（基于时间戳 + 随机后缀）。"""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"idea-{timestamp}-{secrets.token_hex(3)}"


def _format_idea_block(
    *,
    occurred_at: str,
    source: IdeaInboxSource,
    author: str,
    text: str,
    entry_id: str,
) -> str:
    """格式化一个 append 块。

    形式::

        ## YYYY-MM-DD HH:MM · source · author (entry-id)

        > 想法原文...
    """
    source_label = {
        IdeaInboxSource.FRONTEND: "frontend",
        IdeaInboxSource.INBOUND: "inbound",
        IdeaInboxSource.FEISHU: "feishu",
        IdeaInboxSource.MANUAL: "manual",
    }[source]
    header = f"## {occurred_at} · {source_label}" f" · {author or 'anonymous'} ({entry_id})"
    quoted_lines = "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
    # 块前留一个空行，确保相邻 ## 段落之间有视觉间距。
    return f"\n{header}\n\n{quoted_lines}\n"


def _idea_header_lines(ideas_text: str) -> list[tuple[int, str]]:
    """返回所有 ``## ...`` 标题行的行号与原文。"""
    headers: list[tuple[int, str]] = []
    for index, line in enumerate(ideas_text.splitlines()):
        if _IDEA_HEADER_RE.match(line.strip()):
            headers.append((index, line.strip()))
    return headers


def _parse_idea_entries(ideas_text: str) -> list[IdeaEntry]:
    """从 ``ideas.md`` 文本中解析 IdeaEntry 列表。

    跳过顶部的非 H2 段落（H1 + 解释文字）；遇到 ``## YYYY-MM-DD`` 后
    收集紧随的 ``> ...`` blockquote 直到下一个 H2。
    """
    lines = ideas_text.splitlines()
    entries: list[IdeaEntry] = []
    in_entry = False
    current: dict[str, object] = {}
    blockquote_buffer: list[str] = []

    def _flush() -> None:
        if not current:
            return
        text_value = "\n".join(blockquote_buffer).strip()
        if not text_value:
            current.clear()
            blockquote_buffer.clear()
            return
        entries.append(
            IdeaEntry(
                entry_id=str(current["entry_id"]),
                occurred_at=str(current["occurred_at"]),
                source=IdeaInboxSource(str(current["source"])),
                author=str(current["author"]),
                text=text_value,
            )
        )
        current.clear()
        blockquote_buffer.clear()

    for line in lines:
        header_match = _IDEA_HEADER_RE.match(line.strip())
        if header_match:
            _flush()
            in_entry = True
            occurred_at = header_match.group(1)
            tag = header_match.group("tag") or ""
            # 先抓出括号里的 entry id（可能嵌在 author 字段中）。
            entry_id_match = re.search(r"\((?P<eid>idea-[^)]+)\)", tag)
            entry_id_value = entry_id_match.group("eid") if entry_id_match else ""
            # 去掉括号部分后再按 · 拆 source / author。
            tag_without_eid = re.sub(r"\(idea-[^)]+\)", "", tag)
            source_value = "manual"
            author_value = "anonymous"
            for part in [segment.strip() for segment in tag_without_eid.split("·")]:
                if part in {s.value for s in IdeaInboxSource}:
                    source_value = part
                elif part:
                    author_value = part
            current = {
                "occurred_at": occurred_at,
                "source": source_value,
                "author": author_value or "anonymous",
                "entry_id": entry_id_value or _new_entry_id(),
            }
            blockquote_buffer = []
            continue
        if in_entry:
            if line.startswith(">"):
                cleaned = line[1:].lstrip() if len(line) > 1 else ""
                blockquote_buffer.append(cleaned)
            elif line.strip() == "":
                blockquote_buffer.append("")
            else:
                # 出现非 blockquote / 非空行视为 entry 结束。
                _flush()
                in_entry = False
    _flush()
    return entries


def read_idea_inbox(repo_path: Path) -> IdeaInboxSnapshot:
    """读取目标仓库的完整 Idea Inbox 快照。"""
    _ensure_inbox_layout(repo_path)
    paths = resolve_inbox_paths(repo_path)
    ideas_path = _abs_ideas_path(repo_path)
    summary_path = _abs_summary_path(repo_path)

    ideas_text = _read_text(ideas_path)
    summary_text = _read_text(summary_path)
    entries = _parse_idea_entries(ideas_text)
    drafts = list_drafts(repo_path)
    return IdeaInboxSnapshot(
        repo_id=repo_path.name,
        ideas_path=paths.ideas_file,
        summary_path=paths.summary_file,
        drafts_dir=paths.drafts_dir,
        ideas_raw=ideas_text,
        summary_raw=summary_text,
        entries=tuple(entries),
        drafts=tuple(drafts),
    )


def append_idea(
    repo_path: Path,
    *,
    source: IdeaInboxSource,
    author: str,
    text: str,
    occurred_at: str | None = None,
) -> AppendIdeaResult:
    """append-only 追加一条想法到 ``ideas.md``。

    不会重写已有内容；若 ``ideas.md`` 不存在则初始化为带标题与说明的
    空日志。
    """
    if not text or not text.strip():
        raise ValueError("想法原文不能为空。")
    _ensure_inbox_layout(repo_path)
    ideas_path = _abs_ideas_path(repo_path)
    if not ideas_path.exists():
        _initialize_ideas_file(ideas_path)
    existing = ideas_path.read_text(encoding="utf-8")
    stamp = occurred_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    entry_id = _new_entry_id()
    block = _format_idea_block(
        occurred_at=stamp,
        source=source,
        author=author or "anonymous",
        text=text.strip(),
        entry_id=entry_id,
    )
    # 在已有内容末尾和块开头之间补一个空行，保证相邻 H2 段落之间有视觉间距。
    body_block = block.lstrip("\n")
    if existing.rstrip():
        new_content = existing.rstrip() + "\n\n" + body_block
    else:
        new_content = body_block
    _atomic_write_text(ideas_path, new_content)
    _logger.info("Appended idea %s for repo %s (source=%s)", entry_id, repo_path.name, source)
    return AppendIdeaResult(
        entry=IdeaEntry(
            entry_id=entry_id,
            occurred_at=stamp,
            source=source,
            author=author or "anonymous",
            text=text.strip(),
        ),
        ideas_path=str(ideas_path.relative_to(repo_path)),
    )


def _initialize_ideas_file(ideas_path: Path) -> None:
    """初始化一份带说明的 ``ideas.md`` 空模板。"""
    template = (
        "# Idea Inbox — 原话日志\n\n"
        "> 追加式、逐字保留。AI 只在末尾追加，永不改写已有条目。"
        "事实来源是本文件。\n"
    )
    _atomic_write_text(ideas_path, template)


def _format_summary_text(
    *,
    entries: Sequence[IdeaEntry],
    source_label: str,
    body: str,
) -> str:
    """生成 ``summary.md`` 全文，固定包含声明 + 来源说明。"""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry_listing = "\n".join(
        f"- `{entry.occurred_at}` [{entry.source.value}] {entry.text[:60]}"
        for entry in entries[-20:]
    )
    return (
        _SUMMARY_HEADER
        + f"> 最近生成：{timestamp}（来源：{source_label}）\n\n"
        + f"## 当前收录 {len(entries)} 条想法\n\n"
        + (entry_listing or "（暂无）")
        + "\n\n## AI 总结\n\n"
        + body.strip()
        + "\n"
    )


def refresh_idea_summary(
    repo_path: Path,
    *,
    summary_text: str,
    source_label: str = "manual",
) -> RefreshSummaryResult:
    """重写 ``summary.md``。

    本函数只负责落盘：生成内容由调用方（core use case 之外的
    ``IContentGenerator`` 封装）负责。来源标签会写入文件头供前端展示。
    """
    _ensure_inbox_layout(repo_path)
    summary_path = _abs_summary_path(repo_path)
    ideas_path = _abs_ideas_path(repo_path)
    ideas_text = _read_text(ideas_path)
    entries = _parse_idea_entries(ideas_text)
    final_text = _format_summary_text(entries=entries, source_label=source_label, body=summary_text)
    _atomic_write_text(summary_path, final_text)
    return RefreshSummaryResult(
        summary_path=str(summary_path.relative_to(repo_path)),
        summary_text=final_text,
        source=source_label,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 草稿文件元数据解析（仅读，与 idea_prd_drafts 共享常量）
# ─────────────────────────────────────────────────────────────────────────────


_DRAFT_STATUS_LINE_RE = re.compile(
    r"^Draft Status:\s*(?P<status>pending-review|approved|rejected)\s*$",
    re.IGNORECASE,
)
_DRAFT_REPO_LINE_RE = re.compile(r"^Repo ID:\s*(?P<repo>.+?)\s*$")
_DRAFT_PRIORITY_LINE_RE = re.compile(r"^Priority:\s*(?P<priority>.+?)\s*$")
_DRAFT_TYPE_LINE_RE = re.compile(r"^Type:\s*(?P<prd_type>.+?)\s*$")
_DRAFT_CREATED_LINE_RE = re.compile(r"^Created At:\s*(?P<created>.+?)\s*$")
_DRAFT_APPROVED_LINE_RE = re.compile(r"^Approved Pending Path:\s*(?P<path>.+?)\s*$")
_DRAFT_IDEA_REFS_LINE_RE = re.compile(r"^Source Idea Refs:\s*(?P<refs>.+?)\s*$")
_DRAFT_HEADER_RE = re.compile(
    r"^---\s*$\n"
    r"Draft ID:\s*(?P<draft_id>.+?)\s*\n"
    r"Draft Status:\s*(?P<status>.+?)\s*\n"
    r"Repo ID:\s*(?P<repo_id>.+?)\s*\n"
    r"Priority:\s*(?P<priority>.+?)\s*\n"
    r"Type:\s*(?P<prd_type>.+?)\s*\n"
    r"Created At:\s*(?P<created_at>.+?)\s*\n"
    r"Source Idea Refs:\s*(?P<idea_refs>.+?)\s*\n"
    r"(?:Approved Pending Path:\s*(?P<approved_path>.+?)\s*\n)?"
    r"---\s*$",
    re.MULTILINE,
)


def _parse_draft_header(text: str) -> PrdDraftMetadata | None:
    """从草稿文本解析结构化元数据块。"""
    match = _DRAFT_HEADER_RE.search(text)
    if not match:
        return None
    idea_refs = tuple(ref.strip() for ref in match.group("idea_refs").split(",") if ref.strip())
    return PrdDraftMetadata(
        draft_id=match.group("draft_id").strip(),
        status=PrdDraftStatus(match.group("status").strip()),
        repo_id=match.group("repo_id").strip(),
        source_idea_refs=idea_refs,
        priority=match.group("priority").strip(),
        prd_type=match.group("prd_type").strip(),
        created_at=match.group("created_at").strip(),
        approved_pending_path=(
            match.group("approved_path").strip() if match.group("approved_path") else None
        ),
    )


def parse_draft_header(text: str) -> PrdDraftMetadata | None:
    """公共入口：从草稿文本解析元数据。"""
    return _parse_draft_header(text)


def _extract_title(prd_text: str) -> str:
    """从草稿文本里提取 H1 标题，去掉 ``PRD:`` 前缀。"""
    for line in prd_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].lstrip("PRD:： ").strip() or "未命名草稿"
    return "未命名草稿"


def extract_draft_title(prd_text: str) -> str:
    """公共入口：从文本里提取 H1 标题（去掉 ``PRD:`` 前缀）。"""
    return _extract_title(prd_text)


def _build_excerpt(prd_text: str, max_chars: int = 240) -> str:
    """截取正文片段，去掉前导 metadata block。"""
    body_start = prd_text.find("---", prd_text.find("---") + 3)
    body = prd_text[body_start + 3 :] if body_start != -1 else prd_text
    body = body.strip()
    if len(body) <= max_chars:
        return body
    return body[: max_chars - 3] + "..."


def build_excerpt(prd_text: str, max_chars: int = 240) -> str:
    """公共入口：截取正文片段。"""
    return _build_excerpt(prd_text, max_chars)


def list_drafts(repo_path: Path) -> list[PrdDraftSummary]:
    """列出 ``prd-drafts/`` 下所有草稿，按 ``draft_id`` 时间戳倒序。"""
    drafts_dir = _abs_drafts_dir(repo_path)
    if not drafts_dir.exists():
        return []
    summaries: list[PrdDraftSummary] = []
    for path in sorted(drafts_dir.glob("*.md")):
        text = _read_text(path)
        metadata = _parse_draft_header(text)
        if metadata is None:
            continue
        summaries.append(
            PrdDraftSummary(
                metadata=metadata,
                draft_path=str(path.relative_to(repo_path)),
                title=_extract_title(text),
                body_excerpt=_build_excerpt(text),
            )
        )
    summaries.sort(key=lambda s: s.metadata.draft_id, reverse=True)
    return summaries
