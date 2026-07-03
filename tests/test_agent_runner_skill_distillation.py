"""Tests for the skill distillation business rules."""

from __future__ import annotations

from pathlib import Path


from backend.core.agent.memory import (
    distill_skill,
    promote_draft_to_skills,
    save_skill_draft,
    should_auto_promote,
)
from backend.core.shared.models.agent_runner import (
    IssueSummary,
    MemoryConfig,
)
from backend.infrastructure.memory import build_memory_stores, resolve_memory_paths
from backend.infrastructure.memory.skill_draft_store import (
    SkillDraft,
    SkillDraftStore,
    SkillDraftUpdate,
)


def _make_store(tmp_path: Path):
    config = _make_config(tmp_path)
    paths = resolve_memory_paths(
        tmp_path,
        base_dir=config.base_dir,
        skill_drafts_dir=config.skill_drafts_dir,
        promoted_skills_dirs=config.promoted_skills_dirs,
    )
    return build_memory_stores().skill(paths["skill_drafts_dir"])


def _make_config(
    tmp_path: Path, *, auto_promote: bool = True, threshold: int = 3
) -> MemoryConfig:
    return MemoryConfig(
        enabled=True,
        base_dir=str(tmp_path / "memory"),
        skill_drafts_dir=str(tmp_path / "drafts"),
        promoted_skills_dirs=(str(tmp_path / "promoted"),),
        top_k_skills=3,
        top_k_facts=5,
        auto_promote=auto_promote,
        auto_promote_threshold=threshold,
        auto_promote_min_success_rate=1.0,
    )


def test_distill_skill_returns_candidate(tmp_path: Path) -> None:
    issue = IssueSummary(
        number=42,
        title="Fix ruff F401 unused imports",
        url="https://example/42",
        body="Lint flagged an unused import that breaks pre-commit.",
        labels=("area/lint", "agent/claude"),
    )
    candidate = distill_skill(
        issue=issue,
        diff_summary="- removed unused import in module x",
        recovery_history="agent dropped the import and re-ran just lint",
        worktree_path=tmp_path,
        memory_config=_make_config(tmp_path),
    )
    assert candidate is not None
    assert "ruff" in candidate.name
    assert any("lint" in tag for tag in candidate.tags)


def test_distill_skill_skips_project_specific_markers(tmp_path: Path) -> None:
    issue = IssueSummary(
        number=42,
        title="Generic recipe",
        url="https://example/42",
        body="body",
        labels=("area/general",),
    )
    candidate = distill_skill(
        issue=issue,
        diff_summary="added /Users/zata/local/file.py path lookup",
        recovery_history="",
        worktree_path=tmp_path,
        memory_config=_make_config(tmp_path),
    )
    assert candidate is None


def test_distill_skill_disabled_returns_none(tmp_path: Path) -> None:
    issue = IssueSummary(number=1, title="t", url="u", body="b", labels=())
    config = _make_config(tmp_path)
    object.__setattr__(config, "enabled", False)
    candidate = distill_skill(
        issue=issue,
        diff_summary="diff",
        recovery_history="hist",
        worktree_path=tmp_path,
        memory_config=config,
    )
    assert candidate is None


def test_save_skill_draft_dedupes_similar_entries(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    issue = IssueSummary(
        number=1,
        title="Fix lint F401 unused imports",
        url="https://example/1",
        body="b",
        labels=("area/lint",),
    )
    first = distill_skill(
        issue=issue,
        diff_summary="removed unused import",
        recovery_history="",
        worktree_path=tmp_path,
        memory_config=config,
    )
    assert first is not None
    store = _make_store(tmp_path)
    save_skill_draft(first, config, tmp_path, store)

    issue_two = IssueSummary(
        number=2,
        title="Fix lint F401 unused imports",
        url="https://example/2",
        body="b",
        labels=("area/lint",),
    )
    second = distill_skill(
        issue=issue_two,
        diff_summary="removed unused import again",
        recovery_history="",
        worktree_path=tmp_path,
        memory_config=config,
    )
    assert second is not None
    save_skill_draft(second, config, tmp_path, store)
    drafts_dir = tmp_path / "drafts"
    skill_files = list(drafts_dir.glob("*.md"))
    assert len(skill_files) == 1
    parsed = SkillDraftStore(drafts_dir).find_similar_draft(
        name=second.name,
        tags=second.tags,
        description=second.description,
    )
    assert parsed is not None
    assert parsed.usage_count == 2


def test_save_skill_draft_dedupes_with_multi_digit_issue_numbers(
    tmp_path: Path,
) -> None:
    """Regression test: the dedup must collapse consecutive multi-digit issues.

    Previously the tokenizer kept ``42`` and ``43`` as distinct tokens (they
    are longer than 1 char) and broke Jaccard above the 0.8 threshold.
    """
    config = _make_config(tmp_path)
    store = _make_store(tmp_path)
    issue_one = IssueSummary(
        number=42,
        title="Fix lint F401 unused imports",
        url="https://example/42",
        body="b",
        labels=("area/lint",),
    )
    first = distill_skill(
        issue=issue_one,
        diff_summary="removed unused import",
        recovery_history="",
        worktree_path=tmp_path,
        memory_config=config,
    )
    assert first is not None
    save_skill_draft(first, config, tmp_path, store)

    issue_two = IssueSummary(
        number=43,
        title="Fix lint F401 unused imports",
        url="https://example/43",
        body="b",
        labels=("area/lint",),
    )
    second = distill_skill(
        issue=issue_two,
        diff_summary="removed unused import again",
        recovery_history="",
        worktree_path=tmp_path,
        memory_config=config,
    )
    assert second is not None
    save_skill_draft(second, config, tmp_path, store)
    drafts_dir = tmp_path / "drafts"
    skill_files = list(drafts_dir.glob("*.md"))
    assert len(skill_files) == 1
    parsed = SkillDraftStore(drafts_dir).find_similar_draft(
        name=second.name,
        tags=second.tags,
        description=second.description,
    )
    assert parsed is not None
    assert parsed.usage_count == 2
    assert parsed.success_count == 2


def test_should_auto_promote_respects_threshold(tmp_path: Path) -> None:
    config = _make_config(tmp_path, threshold=3)
    candidate = SkillDraft(
        name="x",
        description="",
        tags=(),
        version="1.0.0",
        draft=True,
        updated="",
        usage_count=2,
        success_count=2,
        path=tmp_path / "x.md",
    )
    assert not should_auto_promote(candidate, config)
    promoted = SkillDraft(
        name=candidate.name,
        description=candidate.description,
        tags=candidate.tags,
        version=candidate.version,
        draft=candidate.draft,
        updated=candidate.updated,
        usage_count=3,
        success_count=3,
        path=candidate.path,
    )
    assert should_auto_promote(promoted, config)


def test_promote_draft_to_skills_moves_file(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    store = SkillDraftStore(tmp_path / "drafts")
    update = SkillDraftUpdate(
        name="ruff-f401",
        description="Remove unused imports.",
        tags=("ruff",),
        body="body",
        usage_count=3,
        success_count=3,
    )
    store.save_draft(update)
    loaded = store.find_similar_draft(
        name=update.name, tags=update.tags, description=update.description
    )
    assert loaded is not None
    promoted = promote_draft_to_skills(loaded, config, tmp_path, _make_store(tmp_path))
    assert promoted is not None
    assert (tmp_path / "promoted" / "ruff-f401.md").is_file()
    promoted_text = (tmp_path / "promoted" / "ruff-f401.md").read_text(encoding="utf-8")
    assert "draft: false" in promoted_text
