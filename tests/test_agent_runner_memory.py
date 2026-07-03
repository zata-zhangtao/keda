"""Tests for short-term / long-term memory stores and the runner hooks."""

from __future__ import annotations

import json
from pathlib import Path


from backend.core.agent.memory import (
    save_short_term_memory,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    AttemptResult,
    FailureType,
    IssueSummary,
    MemoryConfig,
)
from backend.core.use_cases.run_agent_once import (
    _persist_short_term_memory,
    _resolve_repo_id,
)
from backend.infrastructure.memory import (
    LongTermMemoryStore,
    ShortTermMemoryContext,
    ShortTermMemoryStore,
    build_memory_stores,
    resolve_memory_paths,
)
from backend.infrastructure.memory.skill_draft_store import (
    SkillDraftStore,
    SkillDraftUpdate,
)


def _make_config(tmp_path: Path, *, enabled: bool = True) -> AppConfig:
    base = str(tmp_path / ".iar" / "memory")
    drafts = str(tmp_path / ".iar" / "skills" / "drafts")
    promoted = (str(tmp_path / ".iar" / "skills"),)
    return AppConfig(
        memory=MemoryConfig(
            enabled=enabled,
            base_dir=base,
            skill_drafts_dir=drafts,
            promoted_skills_dirs=promoted,
            top_k_skills=3,
            top_k_facts=5,
            auto_promote=True,
            auto_promote_threshold=3,
            auto_promote_min_success_rate=1.0,
        ),
    )


def test_short_term_store_roundtrip(tmp_path: Path) -> None:
    store = ShortTermMemoryStore(tmp_path / "memory")
    context = ShortTermMemoryContext(
        repo_id="keda-main",
        issue_number=124,
        issue_title="Memory Persistence",
        issue_url="https://example/124",
        summary="first attempt",
    )
    saved = store.save("keda-main", 124, context)
    assert saved.is_file()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["repo_id"] == "keda-main"
    assert payload["issue_number"] == 124
    loaded = store.load("keda-main", 124)
    assert loaded is not None
    assert loaded.summary == "first attempt"


def test_long_term_store_append_and_load(tmp_path: Path) -> None:
    store = LongTermMemoryStore(tmp_path)
    store.append_fact(
        category="encoding",
        topic="python-text-io",
        content="Always pass encoding='utf-8' to text I/O.",
        tags=["python", "lint"],
    )
    store.append_fact(
        category="encoding",
        topic="windows-eol",
        content="Avoid Windows line endings in committed text.",
        tags=["lint", "convention"],
    )
    matches = store.load_by_tags(["lint"])
    topics = {fact.topic for fact in matches}
    assert "python-text-io" in topics
    assert "windows-eol" in topics


def test_skill_draft_store_merge_and_promote(tmp_path: Path) -> None:
    drafts_dir = tmp_path / "drafts"
    promoted_dir = tmp_path / "promoted"
    store = SkillDraftStore(drafts_dir)

    update_a = SkillDraftUpdate(
        name="fix-ruff-f401",
        description="Always remove unused imports flagged by ruff F401.",
        tags=("ruff", "lint"),
        body="## Trigger\n\nruff F401 unused import.",
        usage_count=1,
        success_count=1,
    )
    path_a = store.save_draft(update_a)
    assert path_a.is_file()

    similar = store.find_similar_draft(
        name="fix-ruff-f401",
        tags=("ruff", "lint"),
        description="Always remove unused imports flagged by ruff F401.",
    )
    assert similar is not None
    assert similar.usage_count == 1

    update_b = SkillDraftUpdate(
        name="fix-ruff-f401",
        description="Always remove unused imports flagged by ruff F401.",
        tags=("ruff", "lint"),
        body="## Trigger\n\nruff F401 unused import.\n## Recovery\n\ndrop the import.",
        usage_count=1,
        success_count=1,
    )
    merged_path = store.update_draft(similar, update_b)
    reloaded = SkillDraftStore(drafts_dir).find_similar_draft(
        name=update_b.name,
        tags=update_b.tags,
        description=update_b.description,
    )
    assert reloaded is not None
    assert reloaded.usage_count == 2
    assert reloaded.success_count == 2
    assert "Recovery" in reloaded.body
    assert merged_path.is_file()

    promoted = store.promote_draft(reloaded, [promoted_dir])
    assert promoted is not None
    assert (promoted_dir / promoted.name).is_file()
    promoted_text = (promoted_dir / promoted.name).read_text(encoding="utf-8")
    assert "draft: false" in promoted_text


def test_save_short_term_memory_writes_file(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://example/1",
        body="body",
        labels=(),
    )
    attempt = AttemptResult(
        attempt_number=1,
        failure_type=FailureType.VERIFICATION_FAILED,
        recovered=False,
        detail="ruff failed",
        agent="claude",
    )
    paths = resolve_memory_paths(
        tmp_path,
        base_dir=config.memory.base_dir,
        skill_drafts_dir=config.memory.skill_drafts_dir,
        promoted_skills_dirs=config.memory.promoted_skills_dirs,
    )
    store = build_memory_stores().short_term(paths["short_term_base"])
    saved = save_short_term_memory(
        repo_id="keda-main",
        issue=issue,
        attempt_result=attempt,
        worktree_path=tmp_path,
        memory_config=config.memory,
        store=store,
    )
    assert saved is not None
    assert saved.is_file()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["attempts"][0]["failure_type"] == "verification_failed"


def test_save_short_term_memory_disabled_returns_none(tmp_path: Path) -> None:
    config = _make_config(tmp_path, enabled=False)
    issue = IssueSummary(number=1, title="t", url="u", body="b", labels=())
    attempt = AttemptResult(
        attempt_number=1,
        failure_type=FailureType.SUCCESS,
        recovered=False,
        detail="",
        agent="claude",
    )
    paths = resolve_memory_paths(
        tmp_path,
        base_dir=config.memory.base_dir,
        skill_drafts_dir=config.memory.skill_drafts_dir,
        promoted_skills_dirs=config.memory.promoted_skills_dirs,
    )
    store = build_memory_stores().short_term(paths["short_term_base"])
    assert (
        save_short_term_memory(
            repo_id="keda-main",
            issue=issue,
            attempt_result=attempt,
            worktree_path=tmp_path,
            memory_config=config.memory,
            store=store,
        )
        is None
    )


def test_persist_short_term_memory_swallows_errors(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    issue = IssueSummary(number=2, title="t", url="u", body="b", labels=())
    attempt = AttemptResult(
        attempt_number=1,
        failure_type=FailureType.SUCCESS,
        recovered=False,
        detail="ok",
        agent="claude",
    )
    _persist_short_term_memory(
        config=config,
        issue=issue,
        worktree_path=tmp_path,
        attempt=attempt,
        repo_id="keda-main",
    )
    target = (
        tmp_path
        / config.memory.base_dir
        / "short_term"
        / "keda-main"
        / "2"
        / "context.json"
    )
    assert target.is_file()


def test_resolve_repo_id_falls_back_to_directory_name(tmp_path: Path) -> None:
    repo_id = _resolve_repo_id(
        IssueSummary(number=1, title="t", url="u", body="b", labels=()),
        tmp_path,
    )
    assert repo_id == tmp_path.resolve().name
