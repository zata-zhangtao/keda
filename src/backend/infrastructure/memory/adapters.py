"""记忆存储的协议适配器与组合根。

将 ``infrastructure/memory/`` 中的具体存储实现包装为
``backend.core.agent.memory.protocols`` 中定义的协议对象，供
``core/agent/memory/`` 业务规则使用。

本文件**不**导入 ``backend.core`` —— 严格遵守架构依赖方向
（infrastructure 不得依赖 core）。协议类型由 ``core`` 拥有并
由本文件的 adapter 在 composition root 通过字段复制来满足
（Python 的结构化子类型 / 鸭子类型足够），无需运行时
``isinstance`` 校验，也不需要引用 ``core`` 类型。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Protocol


class MemoryStores(Protocol):
    """Bundle of factory callables that produce the three memory stores."""

    def short_term(self, base_dir: str | Path) -> object: ...
    def long_term(self, base_dir: str | Path) -> object: ...
    def skill(self, drafts_dir: str | Path) -> object: ...


def build_memory_stores() -> MemoryStores:
    """Construct the production ``MemoryStores`` bundle backed by the local FS."""
    from backend.infrastructure.memory.long_term_store import (
        LongTermMemoryStore,
    )
    from backend.infrastructure.memory.short_term_store import (
        ShortTermMemoryStore,
    )
    from backend.infrastructure.memory.skill_draft_store import (
        SkillDraftStore,
    )

    class _Bundle:
        def short_term(self, base_dir):
            return _ShortTermAdapter(ShortTermMemoryStore(base_dir))

        def long_term(self, base_dir):
            return _LongTermAdapter(LongTermMemoryStore(base_dir))

        def skill(self, drafts_dir):
            return _SkillAdapter(SkillDraftStore(drafts_dir))

    return _Bundle()


def resolve_memory_paths(
    worktree_path: Path,
    *,
    base_dir: str,
    skill_drafts_dir: str,
    promoted_skills_dirs: Iterable[str],
) -> dict[str, object]:
    """Resolve the absolute memory-related paths under the worktree."""

    def anchor(rel: str) -> Path:
        if Path(rel).is_absolute():
            return Path(rel)
        return worktree_path / rel

    return {
        "short_term_base": anchor(base_dir),
        "long_term_base": anchor(base_dir),
        "skill_drafts_dir": anchor(skill_drafts_dir),
        "promoted_skills_dirs": tuple(anchor(rel) for rel in promoted_skills_dirs),
    }


class _ShortTermAdapter:
    """Adapt :class:`ShortTermMemoryStore` to the short-term-memory protocol.

    The conversion between the concrete ``ShortTermMemoryContext`` and the
    core protocol payload uses field-by-field access (no core import).
    """

    def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
        self._inner = inner

    def save(
        self,
        repo_id: str,
        issue_number: int,
        payload,  # ShortTermContextPayload from core
    ) -> Path:
        from backend.infrastructure.memory.short_term_store import (
            ShortTermAttemptRecord,
            ShortTermMemoryContext,
        )

        attempts = [
            ShortTermAttemptRecord(
                attempt_number=a.attempt_number,
                failure_type=a.failure_type,
                detail=a.detail,
                recovered=a.recovered,
            )
            for a in payload.attempts
        ]
        context = ShortTermMemoryContext(
            repo_id=payload.repo_id,
            issue_number=payload.issue_number,
            issue_title=payload.issue_title,
            issue_url=payload.issue_url,
            summary=payload.summary,
            attempts=attempts,
            final_solution=payload.final_solution,
            key_files=payload.key_files,
            updated_at=payload.updated_at,
        )
        return self._inner.save(repo_id, issue_number, context)

    def load(self, repo_id: str, issue_number: int):
        context = self._inner.load(repo_id, issue_number)
        if context is None:
            return None
        return _short_term_context_to_protocol(context)


class _LongTermAdapter:
    def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
        self._inner = inner

    def append_fact(
        self,
        *,
        category: str,
        topic: str,
        content: str,
        tags: Iterable[str] = (),
    ) -> Path:
        return self._inner.append_fact(
            category=category,
            topic=topic,
            content=content,
            tags=tags,
        )

    def load_all(self) -> list:
        facts = self._inner.load_all()
        return [_long_term_fact_to_protocol(f) for f in facts]


class _SkillAdapter:
    def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
        self._inner = inner

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
    ) -> Path:
        from backend.infrastructure.memory.skill_draft_store import (
            SkillDraftUpdate,
        )

        update = SkillDraftUpdate(
            name=name,
            description=description,
            tags=tuple(tags),
            body=body,
            usage_count=usage_count,
            success_count=success_count,
            version=version,
            draft=draft,
        )
        return self._inner.save_draft(update)

    def find_similar_draft(
        self,
        *,
        name: str,
        tags: Iterable[str],
        description: str = "",
        similarity_threshold: float = 0.8,
    ):
        draft = self._inner.find_similar_draft(
            name=name,
            tags=tags,
            description=description,
            similarity_threshold=similarity_threshold,
        )
        return _draft_to_record(draft)

    def update_draft(
        self,
        existing,
        *,
        name: str,
        description: str,
        tags: tuple[str, ...],
        body: str,
        usage_count: int = 0,
        success_count: int = 0,
        version: str | None = None,
        draft: bool = True,
    ) -> Path:
        from backend.infrastructure.memory.skill_draft_store import (
            SkillDraftUpdate,
        )

        update = SkillDraftUpdate(
            name=name,
            description=description,
            tags=tuple(tags),
            body=body,
            usage_count=usage_count,
            success_count=success_count,
            version=version or existing.version,
            draft=draft,
        )
        existing_concrete = _record_to_draft(existing)
        return self._inner.update_draft(existing_concrete, update)

    def load_promoted_skills(self, skills_dirs: Iterable[str | Path]) -> list:
        drafts = self._inner.load_promoted_skills(skills_dirs)
        return [_draft_to_record(d) for d in drafts if d is not None]

    def promote_draft(
        self,
        draft,
        target_dirs: Iterable[str | Path],
    ) -> Path | None:
        existing = _record_to_draft(draft)
        return self._inner.promote_draft(existing, target_dirs)


def _short_term_context_to_protocol(context):
    """Convert concrete ``ShortTermMemoryContext`` to a protocol-shaped payload.

    Constructs a fresh object whose class is structurally compatible with the
    core ``ShortTermContextPayload`` Protocol — same field names and types.
    """

    class _Payload:
        pass

    payload = _Payload()
    payload.repo_id = context.repo_id
    payload.issue_number = context.issue_number
    payload.issue_title = context.issue_title
    payload.issue_url = context.issue_url
    payload.summary = context.summary
    payload.attempts = [_attempt_to_protocol(a) for a in context.attempts]
    payload.final_solution = context.final_solution
    payload.key_files = context.key_files
    payload.updated_at = context.updated_at
    return payload


def _attempt_to_protocol(record):
    """Convert a concrete ``ShortTermAttemptRecord`` to a protocol-shaped object."""

    class _Attempt:
        pass

    attempt = _Attempt()
    attempt.attempt_number = record.attempt_number
    attempt.failure_type = record.failure_type
    attempt.detail = record.detail
    attempt.recovered = record.recovered
    return attempt


def _long_term_fact_to_protocol(fact):
    """Convert concrete ``LongTermFact`` to a protocol-shaped record."""

    class _Record:
        pass

    record = _Record()
    record.topic = fact.topic
    record.category = fact.category
    record.content = fact.content
    record.tags = fact.tags
    record.path = fact.path
    return record


def _draft_to_record(draft):
    """Convert concrete ``SkillDraft`` to a protocol-shaped record."""
    if draft is None:
        return None

    class _Record:
        pass

    record = _Record()
    record.name = draft.name
    record.description = draft.description
    record.tags = tuple(draft.tags)
    record.version = draft.version
    record.draft = draft.draft
    record.updated = draft.updated
    record.usage_count = draft.usage_count
    record.success_count = draft.success_count
    record.path = draft.path
    record.body = draft.body
    return record


def _record_to_draft(record):
    """Convert a protocol-shaped record back to the concrete ``SkillDraft``."""
    from backend.infrastructure.memory.skill_draft_store import SkillDraft

    return SkillDraft(
        name=record.name,
        description=record.description,
        tags=tuple(record.tags),
        version=record.version,
        draft=record.draft,
        updated=record.updated,
        usage_count=record.usage_count,
        success_count=record.success_count,
        path=record.path,
        body=record.body,
    )


__all__ = [
    "MemoryStores",
    "build_memory_stores",
    "resolve_memory_paths",
]
