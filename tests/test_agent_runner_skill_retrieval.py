"""Tests for skill/long-term memory matching and prompt injection."""

from __future__ import annotations

from pathlib import Path


from backend.core.agent.memory import (
    format_skill_catalog,
    load_relevant_memory,
    match_skills_and_memory,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    MemoryConfig,
    PromptConfig,
)
from backend.core.use_cases.agent_runner_feedback import (
    build_prompt,
)
from backend.infrastructure.memory import build_memory_stores, resolve_memory_paths


def _make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        memory=MemoryConfig(
            enabled=True,
            base_dir=str(tmp_path / "memory"),
            skill_drafts_dir=str(tmp_path / "drafts"),
            promoted_skills_dirs=(str(tmp_path / "skills"),),
            top_k_skills=2,
            top_k_facts=2,
        ),
    )


def _make_stores(tmp_path: Path, config: AppConfig):
    paths = resolve_memory_paths(
        tmp_path,
        base_dir=config.memory.base_dir,
        skill_drafts_dir=config.memory.skill_drafts_dir,
        promoted_skills_dirs=config.memory.promoted_skills_dirs,
    )
    bundle = build_memory_stores()
    return bundle.long_term(paths["long_term_base"]), bundle.skill(
        paths["skill_drafts_dir"]
    )


def test_load_relevant_memory_returns_empty_when_disabled(tmp_path: Path) -> None:
    config = AppConfig(
        memory=MemoryConfig(
            enabled=False,
            base_dir=str(tmp_path / "memory"),
            skill_drafts_dir=str(tmp_path / "drafts"),
            promoted_skills_dirs=(str(tmp_path / "skills"),),
        )
    )
    issue = IssueSummary(number=1, title="t", url="u", body="b", labels=())
    long_term, skill = _make_stores(tmp_path, config)
    snapshot = load_relevant_memory(
        issue,
        tmp_path,
        config.memory,
        long_term_store=long_term,
        skill_store=skill,
    )
    assert snapshot.is_empty


def test_load_relevant_memory_picks_matching_fact(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    long_term, skill = _make_stores(tmp_path, config)
    long_term.append_fact(
        category="encoding",
        topic="python-text-io",
        content="Always pass encoding='utf-8' to text I/O.",
        tags=["python", "lint"],
    )
    long_term.append_fact(
        category="encoding",
        topic="windows-eol",
        content="Avoid Windows line endings in committed text.",
        tags=["lint", "convention"],
    )
    issue = IssueSummary(
        number=2,
        title="Make sure lint always uses utf-8",
        url="https://example/2",
        body="lint complains about text io",
        labels=("lint",),
    )
    snapshot = load_relevant_memory(
        issue,
        tmp_path,
        config.memory,
        long_term_store=long_term,
        skill_store=skill,
    )
    assert not snapshot.is_empty
    assert any(fact.topic == "python-text-io" for fact in snapshot.long_term_facts)


def test_load_relevant_memory_returns_promoted_skills(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "ruff-f401.md").write_text(
        "---\n"
        "name: ruff-f401\n"
        "description: Remove unused imports flagged by ruff.\n"
        "tags: [ruff, lint]\n"
        "version: 1.0.0\n"
        "draft: false\n"
        "updated: 2026-01-01T00:00:00Z\n"
        "usage_count: 3\n"
        "success_count: 3\n"
        "---\n\nbody",
        encoding="utf-8",
    )
    long_term, skill = _make_stores(tmp_path, config)
    issue = IssueSummary(
        number=3,
        title="Fix ruff lint failure on unused import",
        url="https://example/3",
        body="ruff says import is unused",
        labels=("lint", "ruff"),
    )
    snapshot = load_relevant_memory(
        issue,
        tmp_path,
        config.memory,
        long_term_store=long_term,
        skill_store=skill,
    )
    skill_names = {s.name for s in snapshot.promoted_skills}
    assert "ruff-f401" in skill_names


def test_load_relevant_memory_resolves_relative_promoted_skills_dir(
    tmp_path: Path,
) -> None:
    """The store should find skills under worktree-relative directories."""
    # Worktree is tmp_path; the relative skill dir is ".iar/skills" — ensure
    # the loader resolves it relative to the worktree, not CWD.
    config = AppConfig(
        memory=MemoryConfig(
            enabled=True,
            base_dir=".iar/memory",
            skill_drafts_dir=".iar/skills/drafts",
            promoted_skills_dirs=(".iar/skills",),
            top_k_skills=2,
            top_k_facts=2,
        ),
    )
    skills_dir = tmp_path / ".iar" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "ruff-f401.md").write_text(
        "---\n"
        "name: ruff-f401\n"
        "description: Remove unused imports flagged by ruff.\n"
        "tags: [ruff, lint]\n"
        "version: 1.0.0\n"
        "draft: false\n"
        "updated: 2026-01-01T00:00:00Z\n"
        "usage_count: 3\n"
        "success_count: 3\n"
        "---\n\nbody",
        encoding="utf-8",
    )
    long_term, skill = _make_stores(tmp_path, config)
    issue = IssueSummary(
        number=4,
        title="Fix ruff lint failure on unused import",
        url="https://example/4",
        body="ruff says import is unused",
        labels=("lint", "ruff"),
    )
    snapshot = load_relevant_memory(
        issue,
        tmp_path,
        config.memory,
        long_term_store=long_term,
        skill_store=skill,
    )
    assert any(s.name == "ruff-f401" for s in snapshot.promoted_skills)


def test_format_skill_catalog_skips_when_empty() -> None:
    assert format_skill_catalog([]) == ""


def test_format_skill_catalog_lists_paths_only() -> None:
    from backend.infrastructure.memory.skill_draft_store import SkillDraft

    skill = SkillDraft(
        name="ruff-f401",
        description="Remove unused imports flagged by ruff.",
        tags=("ruff",),
        version="1.0.0",
        draft=False,
        updated="2026-01-01T00:00:00Z",
        usage_count=3,
        success_count=3,
        path=Path("/tmp/ruff-f401.md"),
        body="full body content",
    )
    block = format_skill_catalog([skill])
    assert "ruff-f401" in block
    assert "/tmp/ruff-f401.md" in block
    assert "full body content" not in block


def test_build_prompt_includes_memory_block(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "ruff-f401.md").write_text(
        "---\n"
        "name: ruff-f401\n"
        "description: Remove unused imports flagged by ruff.\n"
        "tags: [ruff, lint]\n"
        "version: 1.0.0\n"
        "draft: false\n"
        "updated: 2026-01-01T00:00:00Z\n"
        "usage_count: 3\n"
        "success_count: 3\n"
        "---\n\nbody",
        encoding="utf-8",
    )
    issue = IssueSummary(
        number=1,
        title="Fix ruff lint failure on unused import",
        url="https://example/1",
        body="body",
        labels=("lint", "ruff"),
    )
    long_term, skill = _make_stores(tmp_path, config)
    prompt = build_prompt(
        issue,
        tmp_path,
        PromptConfig(),
        memory_config=config.memory,
        long_term_store=long_term,
        skill_store=skill,
    )
    assert "Available skills" in prompt
    assert "ruff-f401" in prompt
    assert "skills/ruff-f401.md" in prompt


def test_build_prompt_no_memory_block_when_disabled(tmp_path: Path) -> None:
    config = AppConfig(
        memory=MemoryConfig(
            enabled=False,
            base_dir=str(tmp_path / "memory"),
            skill_drafts_dir=str(tmp_path / "drafts"),
            promoted_skills_dirs=(str(tmp_path / "skills"),),
        )
    )
    issue = IssueSummary(number=1, title="t", url="u", body="b", labels=())
    prompt = build_prompt(
        issue,
        tmp_path,
        PromptConfig(),
        memory_config=config.memory,
    )
    assert "Available skills" not in prompt


def test_match_skills_and_memory_includes_failure_tokens(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "verification-recovery.md").write_text(
        "---\n"
        "name: verification-recovery\n"
        "description: Re-run the verification chain after staged changes.\n"
        "tags: [verification, recovery]\n"
        "version: 1.0.0\n"
        "draft: false\n"
        "updated: 2026-01-01T00:00:00Z\n"
        "usage_count: 5\n"
        "success_count: 5\n"
        "---\n\nbody",
        encoding="utf-8",
    )
    long_term, skill = _make_stores(tmp_path, config)
    issue = IssueSummary(
        number=4,
        title="Random unrelated work",
        url="https://example/4",
        body="nothing to do with verification",
        labels=(),
    )
    snapshot = match_skills_and_memory(
        issue,
        "verification_failed",
        tmp_path,
        config.memory,
        long_term_store=long_term,
        skill_store=skill,
    )
    assert any(s.name == "verification-recovery" for s in snapshot.promoted_skills)
