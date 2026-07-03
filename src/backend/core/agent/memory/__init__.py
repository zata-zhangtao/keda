"""记忆与 skill 蒸馏的核心业务规则。

本目录承载 ``core/agent/memory/`` 的业务规则：
- 加载/匹配长期记忆与已晋升 skill（``memory_loader``）
- 写入/更新短期记忆（``short_term_memory``）
- skill 蒸馏与草稿维护（``skill_distillation``）
- skill 注入 catalog 文本生成（``skill_catalog``）

业务规则通过 ``core/agent/memory/protocols.py`` 定义的协议
访问底层存储；具体文件存储由 ``infrastructure/memory/`` 实现，
并由 use cases 在 composition root 注入。
"""

from __future__ import annotations

from .memory_loader import (
    RelevantMemory,
    load_relevant_memory,
    match_skills_and_memory,
)
from .short_term_memory import save_short_term_memory
from .skill_catalog import format_skill_catalog
from .skill_distillation import (
    DistilledSkill,
    distill_skill,
    find_similar_draft,
    promote_draft_to_skills,
    save_skill_draft,
    should_auto_promote,
    update_draft,
)

__all__ = [
    "DistilledSkill",
    "RelevantMemory",
    "distill_skill",
    "find_similar_draft",
    "format_skill_catalog",
    "load_relevant_memory",
    "match_skills_and_memory",
    "promote_draft_to_skills",
    "save_short_term_memory",
    "save_skill_draft",
    "should_auto_promote",
    "update_draft",
]
