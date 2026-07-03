"""Agent Runner 记忆层的抽象端口（Protocols）。

具体实现位于 ``infrastructure/memory/``；本模块只定义 ``core``
层业务规则依赖的协议。``infrastructure/memory/`` 中的具体类
由 use case 在 composition root 注入到业务规则中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol


@dataclass
class ShortTermAttempt:
    """Attempt 精简记录（与 ``ShortTermAttemptRecord`` 等价的轻量协议类型）。"""

    attempt_number: int
    failure_type: str
    detail: str
    recovered: bool = False


@dataclass
class ShortTermContextPayload:
    """持久化用的短期记忆上下文载荷。"""

    repo_id: str
    issue_number: int
    issue_title: str
    issue_url: str
    summary: str = ""
    attempts: list[ShortTermAttempt] = field(default_factory=list)
    final_solution: str = ""
    key_files: tuple[str, ...] = ()
    updated_at: str = ""


@dataclass
class LongTermFactRecord:
    """长期记忆文件解析后的轻量记录。"""

    topic: str
    category: str
    content: str
    tags: tuple[str, ...]
    path: Path


@dataclass
class SkillRecord:
    """Skill 文件解析后的轻量记录。"""

    name: str
    description: str
    tags: tuple[str, ...]
    version: str
    draft: bool
    updated: str
    usage_count: int
    success_count: int
    path: Path
    body: str = ""


class IShortTermMemoryStore(Protocol):
    """短期记忆读写协议。"""

    def save(
        self,
        repo_id: str,
        issue_number: int,
        payload: ShortTermContextPayload,
    ) -> Path: ...

    def load(
        self, repo_id: str, issue_number: int
    ) -> ShortTermContextPayload | None: ...


class ILongTermMemoryStore(Protocol):
    """长期记忆读写协议。"""

    def append_fact(
        self,
        *,
        category: str,
        topic: str,
        content: str,
        tags: Iterable[str] = (),
    ) -> Path: ...

    def load_all(self) -> list[LongTermFactRecord]: ...


class ISkillStore(Protocol):
    """Skill 草稿与已晋升 skill 持久化协议。"""

    def save_draft(
        self,
        *,
        name: str,
        description: str,
        tags: tuple[str, ...],
        body: str,
        version: str = "1.0.0",
        draft: bool = True,
        usage_count: int = 0,
        success_count: int = 0,
    ) -> Path: ...

    def find_similar_draft(
        self,
        *,
        name: str,
        tags: Iterable[str],
        description: str = "",
        similarity_threshold: float = 0.8,
    ) -> SkillRecord | None: ...

    def update_draft(
        self,
        existing: SkillRecord,
        *,
        name: str,
        description: str,
        tags: tuple[str, ...],
        body: str,
        usage_count: int = 0,
        success_count: int = 0,
        version: str | None = None,
        draft: bool = True,
    ) -> Path: ...

    def load_promoted_skills(
        self, skills_dirs: Iterable[str | Path]
    ) -> list[SkillRecord]: ...

    def promote_draft(
        self,
        draft: SkillRecord,
        target_dirs: Iterable[str | Path],
    ) -> Path | None: ...


__all__ = [
    "IShortTermMemoryStore",
    "ILongTermMemoryStore",
    "ISkillStore",
    "LongTermFactRecord",
    "ShortTermAttempt",
    "ShortTermContextPayload",
    "SkillRecord",
]
