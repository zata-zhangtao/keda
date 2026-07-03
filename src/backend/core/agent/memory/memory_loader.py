"""长期记忆与已晋升 skill 的匹配与加载。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.core.agent.memory.protocols import (
    ILongTermMemoryStore,
    ISkillStore,
    LongTermFactRecord,
    SkillRecord,
)
from backend.core.shared.models.agent_runner import (
    IssueSummary,
    MemoryConfig,
)

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "this",
        "that",
        "issue",
        "github",
        "agent",
        "runner",
        "feature",
        "support",
        "add",
        "use",
        "when",
    }
)


@dataclass(frozen=True)
class RelevantMemory:
    """Result of loading memory and skills for a single Issue."""

    long_term_facts: tuple[LongTermFactRecord, ...] = field(default_factory=tuple)
    promoted_skills: tuple[SkillRecord, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.long_term_facts and not self.promoted_skills


def load_relevant_memory(
    issue: IssueSummary,
    worktree_path: Path,
    memory_config: MemoryConfig,
    *,
    long_term_store: ILongTermMemoryStore,
    skill_store: ISkillStore,
) -> RelevantMemory:
    """Load both long-term facts and promoted skills relevant to an issue.

    Args:
        issue: Issue currently being processed.
        worktree_path: Resolved worktree path used to anchor relative
            memory directories.
        memory_config: Effective memory configuration.
        long_term_store: Caller-injected long-term store.
        skill_store: Caller-injected skill store.

    Returns:
        A :class:`RelevantMemory` snapshot. Empty when ``memory_config.enabled``
        is ``False`` or when no relevant files exist on disk.
    """
    if not memory_config.enabled:
        return RelevantMemory()
    if not worktree_path:
        return RelevantMemory()
    promoted_skills = _load_promoted_skills(worktree_path, skill_store, memory_config)
    issue_tokens = _tokenize_issue(issue)
    scored_facts: list[tuple[float, LongTermFactRecord]] = []
    for fact in long_term_store.load_all():
        score = _score_fact(issue_tokens, fact)
        if score > 0:
            scored_facts.append((score, fact))
    scored_facts.sort(key=lambda pair: pair[0], reverse=True)
    top_facts = tuple(fact for _score, fact in scored_facts[: max(0, memory_config.top_k_facts)])
    scored_skills: list[tuple[float, SkillRecord]] = []
    issue_token_set = set(issue_tokens)
    for skill in promoted_skills:
        score = _score_skill(issue_token_set, skill)
        scored_skills.append((score, skill))
    scored_skills.sort(key=lambda pair: pair[0], reverse=True)
    top_skills = tuple(
        skill for _score, skill in scored_skills[: max(0, memory_config.top_k_skills)]
    )
    return RelevantMemory(long_term_facts=top_facts, promoted_skills=top_skills)


def match_skills_and_memory(
    issue: IssueSummary,
    failure_type: str,
    worktree_path: Path,
    memory_config: MemoryConfig,
    *,
    long_term_store: ILongTermMemoryStore,
    skill_store: ISkillStore,
) -> RelevantMemory:
    """Bias the relevance search toward the current failure type."""
    if not memory_config.enabled:
        return RelevantMemory()
    enriched_issue = IssueSummary(
        number=issue.number,
        title=issue.title,
        url=issue.url,
        body=issue.body + "\n\n" + failure_type.replace("_", " "),
        labels=issue.labels,
        state=issue.state,
    )
    return load_relevant_memory(
        enriched_issue,
        worktree_path,
        memory_config,
        long_term_store=long_term_store,
        skill_store=skill_store,
    )


def _load_promoted_skills(
    worktree_path: Path,
    skill_store: ISkillStore,
    memory_config: MemoryConfig,
) -> tuple[SkillRecord, ...]:
    if not memory_config.enabled:
        return ()
    # Promote relative ``promoted_skills_dirs`` to absolute paths rooted at
    # the worktree so the file-based skill store can locate them
    # regardless of the runner's current working directory.
    resolved_dirs = tuple(
        directory if Path(directory).is_absolute() else (worktree_path / directory)
        for directory in memory_config.promoted_skills_dirs
    )
    return tuple(skill_store.load_promoted_skills(resolved_dirs))


def _tokenize_issue(issue: IssueSummary) -> list[str]:
    parts: list[str] = []
    parts.extend(issue.labels)
    parts.append(issue.title)
    parts.append(issue.body[:1000])
    return _tokenize(*parts)


def _tokenize(*values: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        for raw in re.findall(r"[a-z0-9_\-]+", value.lower()):
            if len(raw) <= 1 or raw in _STOPWORDS or raw in seen:
                continue
            seen.add(raw)
            tokens.append(raw)
    return tokens


def _score_fact(issue_tokens: list[str], fact: LongTermFactRecord) -> float:
    if not issue_tokens:
        return 0.0
    issue_set = set(issue_tokens)
    fact_tokens = set(_tokenize(fact.content, *fact.tags))
    if not fact_tokens:
        return 0.0
    return _jaccard(issue_set, fact_tokens)


def _score_skill(issue_tokens: set[str], skill: SkillRecord) -> float:
    if not issue_tokens:
        return 0.0
    skill_tokens = set(_tokenize(skill.description, *skill.tags, skill.name))
    if not skill_tokens:
        return 0.0
    overlap = len(issue_tokens & skill_tokens)
    if overlap == 0:
        return 0.0
    union = len(issue_tokens | skill_tokens)
    return overlap / union if union else 0.0


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


__all__ = ["RelevantMemory", "load_relevant_memory", "match_skills_and_memory"]
