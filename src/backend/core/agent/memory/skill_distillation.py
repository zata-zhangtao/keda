"""Skill 蒸馏业务规则。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from backend.core.agent.memory.protocols import ISkillStore, SkillRecord
from backend.core.shared.models.agent_runner import (
    IssueSummary,
    MemoryConfig,
)

_logger = logging.getLogger(__name__)

# Patterns that almost always indicate a project-specific value we should
# NOT bake into a reusable skill. The list is intentionally narrow — the
# conservative strategy is to *skip* distillation when any of these match.
_PROJECT_SPECIFIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/[A-Za-z]:/"),  # Windows absolute path
    re.compile(r"/Users/[^/\s]+/"),  # macOS /Users/<user>
    re.compile(r"/home/[^/\s]+/"),  # Linux /home/<user>
    re.compile(r"\bissue-\d+\b"),  # issue-123 etc.
    re.compile(r"\b#\d{2,}\b"),  # issue/PR numbers in prose
    re.compile(r"\bcommit [0-9a-f]{7,40}\b"),  # commit SHAs
    re.compile(r"\bSHA[-_ ]?[0-9a-f]{7,40}\b"),
    re.compile(r"\bsha[-_ ]?\d{7,40}\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class DistilledSkill:
    """A candidate skill extracted from a successful issue execution."""

    name: str
    description: str
    tags: tuple[str, ...]
    body: str
    usage_count: int = 1
    success_count: int = 1


def distill_skill(
    issue: IssueSummary,
    diff_summary: str,
    recovery_history: str = "",
    worktree_path: Path | None = None,
    memory_config: MemoryConfig | None = None,
) -> DistilledSkill | None:
    """Build a candidate skill from the current successful execution."""
    if memory_config is not None and not memory_config.enabled:
        return None
    if not issue.title:
        return None
    body = _build_skill_body(issue, diff_summary, recovery_history)
    if not body or not body.strip():
        return None
    if _contains_project_specific_marker(body) or _contains_project_specific_marker(
        diff_summary
    ):
        _logger.info(
            "Skipping skill distillation for Issue #%d: project-specific marker found.",
            issue.number,
        )
        return None
    if len(body) < 40:
        return None
    name = _derive_skill_name(issue)
    description = _derive_description(issue)
    tags = _derive_tags(issue)
    return DistilledSkill(
        name=name,
        description=description,
        tags=tags,
        body=body,
        usage_count=1,
        success_count=1,
    )


def save_skill_draft(
    skill: DistilledSkill,
    memory_config: MemoryConfig,
    worktree_path: Path,
    skill_store: ISkillStore,
) -> Path:
    """Persist a distilled skill draft, merging with any similar existing one."""
    if not memory_config.enabled:
        raise RuntimeError("memory_config.enabled must be True to save a draft")
    similar = skill_store.find_similar_draft(
        name=skill.name,
        tags=skill.tags,
        description=skill.description,
    )
    if similar is not None:
        return skill_store.update_draft(
            similar,
            name=skill.name,
            description=skill.description or similar.description,
            tags=_merge_tag_tuples(similar.tags, skill.tags),
            body=skill.body or similar.body,
            usage_count=skill.usage_count,
            success_count=skill.success_count,
            version=similar.version,
            draft=similar.draft and True,
        )
    return skill_store.save_draft(
        name=skill.name,
        description=skill.description,
        tags=skill.tags,
        body=skill.body,
        version="1.0.0",
        draft=True,
        usage_count=skill.usage_count,
        success_count=skill.success_count,
    )


def find_similar_draft(
    skill: DistilledSkill,
    memory_config: MemoryConfig,
    worktree_path: Path,
    skill_store: ISkillStore,
) -> SkillRecord | None:
    """Find a similar existing draft, if any."""
    if not memory_config.enabled:
        return None
    return skill_store.find_similar_draft(
        name=skill.name,
        tags=skill.tags,
        description=skill.description,
    )


def update_draft(
    existing: SkillRecord,
    skill: DistilledSkill,
    memory_config: MemoryConfig,
    worktree_path: Path,
    skill_store: ISkillStore,
) -> Path:
    """Merge new evidence into the existing draft."""
    return skill_store.update_draft(
        existing,
        name=existing.name,
        description=skill.description or existing.description,
        tags=_merge_tag_tuples(existing.tags, skill.tags),
        body=skill.body or existing.body,
        usage_count=skill.usage_count,
        success_count=skill.success_count,
        version=existing.version,
        draft=existing.draft,
    )


def should_auto_promote(skill: SkillRecord, memory_config: MemoryConfig) -> bool:
    """Return ``True`` when the draft meets the configured promotion thresholds."""
    if not memory_config.auto_promote:
        return False
    if skill.usage_count < memory_config.auto_promote_threshold:
        return False
    if skill.success_count <= 0 or skill.usage_count <= 0:
        return False
    if (
        skill.success_count / skill.usage_count
    ) < memory_config.auto_promote_min_success_rate:
        return False
    return True


def promote_draft_to_skills(
    skill: SkillRecord,
    memory_config: MemoryConfig,
    worktree_path: Path,
    skill_store: ISkillStore,
) -> Path | None:
    """Move the draft into the first writable promoted-skills directory.

    Relative ``promoted_skills_dirs`` are resolved against the worktree
    so the file-based store can locate the destination regardless of the
    runner's current working directory.
    """
    if not memory_config.enabled:
        return None
    resolved_dirs = tuple(
        directory if Path(directory).is_absolute() else (worktree_path / directory)
        for directory in memory_config.promoted_skills_dirs
    )
    return skill_store.promote_draft(skill, resolved_dirs)


def _merge_tag_tuples(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for tag in group:
            if not tag or tag in seen:
                continue
            seen.add(tag)
            merged.append(tag)
    return tuple(merged)


def _build_skill_body(
    issue: IssueSummary,
    diff_summary: str,
    recovery_history: str,
) -> str:
    sections: list[str] = []
    sections.append(f"## Trigger\n\nObserved in Issue #{issue.number}: {issue.title}.")
    if recovery_history.strip():
        sections.append("## Recovery Path\n\n" + recovery_history.strip())
    if diff_summary.strip():
        sections.append("## Evidence\n\n" + diff_summary.strip())
    sections.append(
        "## How To Apply\n\n"
        "1. Read the issue context.\n"
        "2. Apply the recovery path above.\n"
        "3. Verify with the project's `just test` and `just lint --full` gates."
    )
    return "\n\n".join(sections)


def _derive_skill_name(issue: IssueSummary) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", issue.title.lower()).strip("-")
    if not slug:
        slug = f"issue-{issue.number}"
    return f"issue-{issue.number}-{slug[:60]}"


def _derive_description(issue: IssueSummary) -> str:
    return f"Reusable pattern from Issue #{issue.number}: {issue.title}"


def _derive_tags(issue: IssueSummary) -> tuple[str, ...]:
    tags: list[str] = []
    seen: set[str] = set()
    for label in issue.labels:
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        if slug and slug not in seen:
            seen.add(slug)
            tags.append(slug)
    if not tags:
        tags.append("general")
    return tuple(tags[:6])


def _contains_project_specific_marker(text: str) -> bool:
    if not text:
        return False
    for pattern in _PROJECT_SPECIFIC_PATTERNS:
        if pattern.search(text):
            return True
    return False


__all__ = [
    "DistilledSkill",
    "distill_skill",
    "find_similar_draft",
    "promote_draft_to_skills",
    "save_skill_draft",
    "should_auto_promote",
    "update_draft",
]
