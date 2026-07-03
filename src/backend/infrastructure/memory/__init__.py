"""本地文件系统记忆存储层。

包含短期记忆、长期记忆、skill 草稿与已晋升 skill 的读写实现，
以及把它们包装为 ``core/shared/interfaces/agent_memory.py`` 协议
的适配器与 composition root 工厂。所有文件 I/O 显式使用
``encoding="utf-8"``。
"""

from __future__ import annotations

from .adapters import (
    MemoryStores,
    build_memory_stores,
    resolve_memory_paths,
)
from .long_term_store import LongTermFact, LongTermMemoryStore
from .short_term_store import (
    ShortTermAttemptRecord,
    ShortTermMemoryContext,
    ShortTermMemoryStore,
)
from .skill_draft_store import SkillDraft, SkillDraftStore, SkillDraftUpdate

__all__ = [
    "LongTermFact",
    "LongTermMemoryStore",
    "MemoryStores",
    "ShortTermAttemptRecord",
    "ShortTermMemoryContext",
    "ShortTermMemoryStore",
    "SkillDraft",
    "SkillDraftStore",
    "SkillDraftUpdate",
    "build_memory_stores",
    "resolve_memory_paths",
]
