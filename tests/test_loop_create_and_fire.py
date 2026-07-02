"""Tests for loop registration and fire use cases."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    IssueSummary,
    LabelConfig,
)
from backend.core.shared.models.loop import (
    LoopSchedule,
    LoopScheduleKind,
)
from backend.core.use_cases.loop_create import (
    LoopAlreadyExistsError,
    cancel_loop,
    create_loop_from_recipe,
    list_loops,
    update_loop_schedule,
)
from backend.core.use_cases.loop_fire import build_prd_path, fire_loop
from backend.core.use_cases.loop_recipe import parse_loop_recipe
from backend.engines.agent_runner.persistence.loop_state_json import (
    JsonLoopStateStore,
    resolve_loop_state_path,
)
from backend.engines.agent_runner.scheduler.loop_clock import FixedClock
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo so ``current_git_branch`` works."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    # Initial commit so HEAD points to main.
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", ".gitkeep"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_RECIPE_BODY = """---
id: github-trending
schedule: 0 8 * * *
repo_id: keda-main
issue_type: feature
agent: auto
priority: P2
slug: trending
labels:
  - area/docs
---

# GitHub Trending {{date}}

Run summary for {{loop_id}} in repo {{repo_id}}.
"""


@pytest.fixture
def recipe_path(tmp_path: Path) -> Path:
    path = tmp_path / "tasks" / "loops" / "github-trending.md"
    path.parent.mkdir(parents=True)
    path.write_text(_RECIPE_BODY, encoding="utf-8")
    return path


@pytest.fixture
def state_store(tmp_path: Path) -> JsonLoopStateStore:
    return JsonLoopStateStore(tmp_path / "loop-state.json")


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 6, 23, 7, 30, 0, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_create_loop_from_recipe_writes_state(
    recipe_path: Path,
    state_store: JsonLoopStateStore,
) -> None:
    task = create_loop_from_recipe(recipe_path, state_store=state_store)
    assert task.id == "github-trending"
    assert task.repo_id == "keda-main"
    assert task.schedule.expression == "0 8 * * *"
    assert task.priority == "P2"
    assert task.slug == "trending"
    assert task.labels == ("area/docs",)
    assert task.fire_count == 0

    # Persisted and re-loadable.
    state_store.load()
    fetched = state_store.get_task("github-trending")
    assert fetched is not None
    assert fetched.id == "github-trending"


def test_create_loop_idempotent_without_force(
    recipe_path: Path,
    state_store: JsonLoopStateStore,
) -> None:
    create_loop_from_recipe(recipe_path, state_store=state_store)
    with pytest.raises(LoopAlreadyExistsError):
        create_loop_from_recipe(recipe_path, state_store=state_store)


def test_create_loop_force_overwrite(
    recipe_path: Path,
    state_store: JsonLoopStateStore,
) -> None:
    create_loop_from_recipe(recipe_path, state_store=state_store)
    task = create_loop_from_recipe(recipe_path, state_store=state_store, overwrite=True)
    assert task.id == "github-trending"


def test_create_loop_with_schedule_override(
    recipe_path: Path,
    state_store: JsonLoopStateStore,
) -> None:
    task = create_loop_from_recipe(
        recipe_path,
        state_store=state_store,
        schedule=LoopSchedule(kind=LoopScheduleKind.INTERVAL, expression="1h"),
    )
    assert task.schedule.kind == LoopScheduleKind.INTERVAL


def test_cancel_loop_removes_entry(
    recipe_path: Path,
    state_store: JsonLoopStateStore,
) -> None:
    create_loop_from_recipe(recipe_path, state_store=state_store)
    assert cancel_loop("github-trending", state_store=state_store) is True
    assert cancel_loop("github-trending", state_store=state_store) is False
    assert list_loops(state_store=state_store) == []


def test_update_loop_schedule_recomputes_next_fire(
    recipe_path: Path,
    state_store: JsonLoopStateStore,
) -> None:
    create_loop_from_recipe(recipe_path, state_store=state_store)
    updated = update_loop_schedule(
        "github-trending",
        state_store=state_store,
        new_schedule=LoopSchedule(kind=LoopScheduleKind.INTERVAL, expression="1h"),
    )
    assert updated.schedule.kind == LoopScheduleKind.INTERVAL
    assert updated.next_fire_at is not None


def test_update_loop_schedule_missing_raises(
    recipe_path: Path,
    state_store: JsonLoopStateStore,
) -> None:
    with pytest.raises(KeyError):
        update_loop_schedule(
            "missing",
            state_store=state_store,
            new_schedule=LoopSchedule(
                kind=LoopScheduleKind.CRON, expression="0 8 * * *"
            ),
        )


# ---------------------------------------------------------------------------
# fire_loop
# ---------------------------------------------------------------------------


def test_fire_loop_dry_run_does_not_write_files(
    recipe_path: Path,
    state_store: JsonLoopStateStore,
    clock: FixedClock,
    tmp_path: Path,
) -> None:
    create_loop_from_recipe(recipe_path, state_store=state_store)
    task = state_store.get_task("github-trending")
    assert task is not None

    github = FakeGitHubClient()
    process_runner = FakeProcessRunner()
    result = fire_loop(
        task,
        repo_path=tmp_path,
        github_client=github,
        process_runner=process_runner,
        state_store=state_store,
        clock=clock,
        dry_run=True,
    )
    assert result.status.value == "dry_run"
    assert "Would render" in (result.skipped_reason or "")
    assert not list((tmp_path / "tasks" / "pending").glob("*"))
    assert github.calls == []
    # State was not mutated.
    state_store.load()
    reloaded = state_store.get_task("github-trending")
    assert reloaded.fire_count == 0


def test_fire_loop_renders_prd_and_creates_issue(
    recipe_path: Path,
    state_store: JsonLoopStateStore,
    clock: FixedClock,
    tmp_path: Path,
) -> None:
    create_loop_from_recipe(recipe_path, state_store=state_store)
    task = state_store.get_task("github-trending")
    assert task is not None

    # Override the publish_prd flag so create_issue_from_prd does not
    # attempt to push the PRD. The test environment has no real remote.
    task = replace(task, publish_prd=False, queue_ready=False, run_now=True)

    repo_dir = tmp_path / "repo"
    _init_git_repo(repo_dir)
    github = FakeGitHubClient(issue_url="https://github.com/x/y/issues/77")
    process_runner = FakeProcessRunner()
    result = fire_loop(
        task,
        repo_path=repo_dir,
        github_client=github,
        process_runner=process_runner,
        state_store=state_store,
        clock=clock,
        labels_config=LabelConfig(),
    )
    assert result.status.value == "fired"
    assert result.issue_url == "https://github.com/x/y/issues/77"
    assert result.issue_number == 77
    assert result.prd_path is not None
    assert result.prd_path.exists()
    rendered = result.prd_path.read_text(encoding="utf-8")
    assert "GitHub Trending 2026-06-23" in rendered
    assert "Run summary for github-trending" in rendered

    # The default ``loop/<id>`` label and the recipe's extra labels were
    # added on top of the create_issue defaults.
    label_call = next(
        call for call in github.calls if call.get("method") == "edit_issue_labels"
    )
    assert "loop/github-trending" in label_call["add"]
    assert "area/docs" in label_call["add"]

    # State store updated.
    state_store.load()
    reloaded = state_store.get_task("github-trending")
    assert reloaded.fire_count == 1
    assert reloaded.last_fire_at is not None
    assert reloaded.next_fire_at is not None


def test_fire_loop_dedupes_same_day(
    recipe_path: Path,
    state_store: JsonLoopStateStore,
    clock: FixedClock,
    tmp_path: Path,
) -> None:
    create_loop_from_recipe(recipe_path, state_store=state_store)
    task = state_store.get_task("github-trending")
    assert task is not None

    repo_dir = tmp_path / "repo"
    _init_git_repo(repo_dir)
    github = FakeGitHubClient()
    github.set_list_issues_by_label_result(
        [
            IssueSummary(
                number=99,
                title="[Feature] GitHub Trending 2026-06-23",
                url="https://github.com/x/y/issues/99",
                body="",
                labels=("loop/github-trending",),
                state="OPEN",
            )
        ]
    )
    process_runner = FakeProcessRunner()
    result = fire_loop(
        task,
        repo_path=repo_dir,
        github_client=github,
        process_runner=process_runner,
        state_store=state_store,
        clock=clock,
        labels_config=LabelConfig(),
    )
    assert result.status.value == "skipped_duplicate"
    # No create_issue call.
    assert not [c for c in github.calls if c.get("method") == "create_issue"]
    state_store.load()
    reloaded = state_store.get_task("github-trending")
    assert reloaded.fire_count == 0


def test_fire_loop_executes_pre_command(
    recipe_path: Path,
    state_store: JsonLoopStateStore,
    clock: FixedClock,
    tmp_path: Path,
) -> None:
    create_loop_from_recipe(recipe_path, state_store=state_store)
    task = state_store.get_task("github-trending")
    assert task is not None
    task = replace(
        task,
        pre_command='printf "greeting=hello\\ncount=3\\n"',
        publish_prd=False,
        queue_ready=False,
        run_now=True,
    )

    repo_dir = tmp_path / "repo"
    _init_git_repo(repo_dir)
    github = FakeGitHubClient()
    process_runner = FakeProcessRunner()
    result = fire_loop(
        task,
        repo_path=repo_dir,
        github_client=github,
        process_runner=process_runner,
        state_store=state_store,
        clock=clock,
        labels_config=LabelConfig(),
    )
    assert result.status.value == "fired"
    assert result.prd_path is not None
    # The pre_command output is injected as variables during render.
    # The recipe body has no {{greeting}} placeholders by default, so
    # the call is recorded on the process runner instead.
    assert any(call[:2] == ["/bin/sh", "-c"] for call in process_runner.calls)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_build_prd_path_uses_timestamp(
    tmp_path: Path,
) -> None:
    fire_at = datetime(2026, 6, 23, 8, 0, 0)
    prd_path = build_prd_path(
        tmp_path,
        loop_id="trending",
        priority="P2",
        fire_at=fire_at,
    )
    assert prd_path.name.startswith("P2-FEAT-20260623-080000-trending.md")
    assert prd_path.parent == (tmp_path / "tasks" / "pending").resolve()


def test_resolve_loop_state_path_default() -> None:
    """Default state path lives under ``~/.iar/loop-state.json``."""
    assert str(resolve_loop_state_path()).endswith(".iar/loop-state.json")


def test_parse_loop_recipe_round_trip(recipe_path: Path) -> None:
    recipe = parse_loop_recipe(recipe_path)
    assert recipe.id == "github-trending"
    assert recipe.repo_id == "keda-main"
