"""Idea Inbox 领域模型。

DTO 全部为 frozen dataclass，便于在 core use case / API route 之间传
递，并由 FastAPI route 层的 ``_serialize`` 辅助函数统一转 JSON。

约定：

- ``IdeaInboxSource`` 枚举记录想法的来源（前端、外部 inbound、飞书、
  手工）。
- ``PrdDraftStatus`` 枚举记录 PRD 草稿的状态机。
- 草稿文件名 / pending 文件名遵循
  ``<PRIORITY>-<TYPE>-<YYYYMMDD-HHMMSS>-<slug>.md``。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class IdeaInboxSource(str, Enum):
    """想法的来源渠道。"""

    FRONTEND = "frontend"
    INBOUND = "inbound"
    FEISHU = "feishu"
    MANUAL = "manual"


class PrdDraftStatus(str, Enum):
    """PRD 草稿状态机。"""

    PENDING_REVIEW = "pending-review"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class IdeaInboxPaths:
    """Idea Inbox 涉及的所有文件路径（相对仓库根）。"""

    ideas_file: str  # tasks/inbox/ideas.md
    summary_file: str  # tasks/inbox/summary.md
    drafts_dir: str  # tasks/inbox/prd-drafts/


@dataclass(frozen=True)
class IdeaEntry:
    """一条被 append 的想法记录。"""

    entry_id: str
    occurred_at: str
    source: IdeaInboxSource
    author: str
    text: str


@dataclass(frozen=True)
class PrdDraftMetadata:
    """草稿文件顶部解析出的元数据。"""

    draft_id: str
    status: PrdDraftStatus
    repo_id: str
    source_idea_refs: tuple[str, ...]
    priority: str
    prd_type: str
    created_at: str
    approved_pending_path: str | None


@dataclass(frozen=True)
class PrdDraftSummary:
    """列表 / 详情展示用的草稿摘要。"""

    metadata: PrdDraftMetadata
    draft_path: str
    title: str
    body_excerpt: str


@dataclass(frozen=True)
class IdeaInboxSnapshot:
    """Idea Inbox 完整快照，供前端渲染。"""

    repo_id: str
    ideas_path: str
    summary_path: str
    drafts_dir: str
    ideas_raw: str
    summary_raw: str
    entries: tuple[IdeaEntry, ...]
    drafts: tuple[PrdDraftSummary, ...]


@dataclass(frozen=True)
class AppendIdeaResult:
    """append 操作的结果。"""

    entry: IdeaEntry
    ideas_path: str


@dataclass(frozen=True)
class RefreshSummaryResult:
    """refresh summary 操作的结果。"""

    summary_path: str
    summary_text: str
    source: str  # agent / template / fallback


@dataclass(frozen=True)
class CreateDraftResult:
    """create_prd_draft 操作的结果。"""

    draft: PrdDraftSummary
    draft_path: str


@dataclass(frozen=True)
class ApproveDraftResult:
    """approve_prd_draft 操作的结果。"""

    draft: PrdDraftSummary
    pending_path: str
