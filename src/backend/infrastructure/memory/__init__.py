"""本地文件系统记忆存储层。

包含短期记忆、长期记忆、skill 草稿与已晋升 skill 的读写实现，
以及把它们包装为 ``core/agent/memory/protocols.py`` 协议
的适配器与 composition root 工厂。所有文件 I/O 显式使用
``encoding="utf-8"``；所有 save 路径通过共享的
``infrastructure/memory/_atomic_io.atomic_write_text`` 完成原子落盘。
"""

from __future__ import annotations

from ._atomic_io import atomic_write_text
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
    "atomic_write_text",
    "build_memory_stores",
    "resolve_memory_paths",
]
