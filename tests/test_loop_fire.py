"""End-to-end tests for ``fire_loop`` against an in-memory state store and
a fake GitHub client.

These tests exercise the full happy-path fire:

1. Render recipe + pre_command output → PRD text.
2. Write PRD into ``tasks/pending/`` under a timestamped filename.
3. Reuse :func:`create_issue_from_prd` to create a GitHub Issue.
4. Apply ``loop/<id>`` + extra recipe labels via ``edit_issue_labels``.
5. Persist updated ``last_fire_at`` / ``next_fire_at`` / ``fire_count``.

Dedup behaviour and dry-run mode are also exercised here so that the
slightly-more-orchestrated scenarios live next to the happy path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import CommandResult, IssueSummary
from backend.core.shared.models.loop import (
    LoopSchedule,
    LoopScheduleKind,
    LoopTask,
    LoopFireStatus,
)
from backend.core.use_cases.loop_fire import fire_loop
from backend.engines.agent_runner.persistence.loop_state_json import (
    JsonLoopStateStore,
)
from backend.engines.agent_runner.scheduler.loop_clock import FixedClock
from tests.conftest import FakeGitHubClient, FakeProcessRunner


# ---------------------------------------------------------------------------
# Recipe / task fixtures
# ---------------------------------------------------------------------------


_RECIPE_BODY = """---
id: github-trending
schedule: "0 8 * * *"
repo_id: fire-demo
priority: P2
issue_type: feature
labels:
  - area/discovery
---

# PRD: GitHub Trending digest for {{date}}

Body with loop_id={{loop_id}} repo={{repo_id}}.

## Acceptance Checklist

- [ ] Fetch trending list.
"""


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    """Initialise a minimal git repo so ``publish_prd`` can be exercised."""
    import subprocess

    subprocess.run(
        ["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


@pytest.fixture
def recipe_path(tmp_path: Path) -> Path:
    path = tmp_path / "github-trending.md"
    path.write_text(_RECIPE_BODY, encoding="utf-8")
    return path


@pytest.fixture
def task(recipe_path: Path) -> LoopTask:
    return LoopTask(
        id="github-trending",
        recipe_path=recipe_path,
        repo_id="fire-demo",
        schedule=LoopSchedule(kind=LoopScheduleKind.CRON, expression="0 8 * * *"),
        publish_prd=False,
        queue_ready=True,
    )


# ---------------------------------------------------------------------------
# Happy-path fire
# ---------------------------------------------------------------------------


def test_fire_loop_writes_prd_and_creates_issue(
    task: LoopTask, repo_path: Path, tmp_path: Path
) -> None:
    """A real fire writes the PRD file, creates an Issue, and updates state."""
    fake_github = FakeGitHubClient(
        issue_url="https://github.com/example/fire-demo/issues/101"
    )
    fake_github.set_list_issues_by_label_result([])  # no duplicate today

    state_store = JsonLoopStateStore(tmp_path / "loop-state.json")
    clock = FixedClock(datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc))
    process_runner = FakeProcessRunner()

    result = fire_loop(
        task,
        repo_path=repo_path,
        github_client=fake_github,
        process_runner=process_runner,
        state_store=state_store,
        clock=clock,
        content_generator=None,
        dry_run=False,
    )

    assert result.status is LoopFireStatus.FIRED
    assert result.issue_url == "https://github.com/example/fire-demo/issues/101"
    assert result.issue_number == 101

    # The PRD was written into tasks/pending/ with the timestamped filename.
    relative_prd = result.relative_prd_path
    assert relative_prd is not None
    prd_on_disk = repo_path / relative_prd
    assert prd_on_disk.is_file()
    prd_text = prd_on_disk.read_text(encoding="utf-8")
    assert "GitHub Trending digest for 2026-06-23" in prd_text
    assert "loop_id=github-trending" in prd_text
    assert "repo=fire-demo" in prd_text

    # The Issue was created with the standard runner labels.
    create_calls = [c for c in fake_github.calls if c["method"] == "create_issue"]
    assert len(create_calls) == 1
    create_call = create_calls[0]
    assert "type/feature" in create_call["labels"]
    assert "agent/ready" in create_call["labels"]

    # Loop-specific labels are applied in a follow-up edit_issue_labels call.
    label_edit_calls = [
        c for c in fake_github.calls if c["method"] == "edit_issue_labels"
    ]
    assert len(label_edit_calls) == 1
    edit_call = label_edit_calls[0]
    assert edit_call["issue_number"] == 101
    assert "loop/github-trending" in edit_call["add"]
    assert "area/discovery" in edit_call["add"]

    # The state was persisted with updated counters / next fire.
    state_store.load()
    persisted = state_store.get_task("github-trending")
    assert persisted is not None
    assert persisted.fire_count == 1
    assert persisted.last_fire_at is not None
    assert persisted.next_fire_at is not None
    # next fire should be after the fire time
    assert persisted.next_fire_at > persisted.last_fire_at


def test_fire_loop_dry_run_writes_nothing(
    task: LoopTask, repo_path: Path, tmp_path: Path
) -> None:
    """``dry_run=True`` renders but never touches disk or GitHub."""
    fake_github = FakeGitHubClient()
    state_store = JsonLoopStateStore(tmp_path / "loop-state.json")
    clock = FixedClock(datetime(2026, 6, 23, 7, 30, tzinfo=timezone.utc))

    result = fire_loop(
        task,
        repo_path=repo_path,
        github_client=fake_github,
        process_runner=FakeProcessRunner(),
        state_store=state_store,
        clock=clock,
        dry_run=True,
    )

    assert result.status is LoopFireStatus.DRY_RUN
    assert result.skipped_reason and "Would render PRD" in result.skipped_reason
    create_calls = [c for c in fake_github.calls if c["method"] == "create_issue"]
    assert create_calls == []
    assert not (repo_path / "tasks" / "pending").exists()
    state_store.load()
    assert state_store.get_task("github-trending") is None


def test_fire_loop_skips_when_duplicate_exists(
    task: LoopTask, repo_path: Path, tmp_path: Path
) -> None:
    """An existing open Issue for today on ``loop/<id>`` aborts creation."""
    fake_github = FakeGitHubClient()
    fake_github.set_list_issues_by_label_result(
        [
            IssueSummary(
                number=42,
                title="GitHub Trending digest for 2026-06-23",
                url="https://example/issues/42",
                body="",
                labels=("loop/github-trending",),
            )
        ]
    )
    state_store = JsonLoopStateStore(tmp_path / "loop-state.json")
    clock = FixedClock(datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc))

    result = fire_loop(
        task,
        repo_path=repo_path,
        github_client=fake_github,
        process_runner=FakeProcessRunner(),
        state_store=state_store,
        clock=clock,
    )

    assert result.status is LoopFireStatus.SKIPPED_DUPLICATE
    assert "skipped creation" in (result.skipped_reason or "")
    create_calls = [c for c in fake_github.calls if c["method"] == "create_issue"]
    assert create_calls == []
    # State still records the skip so the daemon doesn't immediately re-fire.
    state_store.load()
    persisted = state_store.get_task("github-trending")
    assert persisted is not None
    assert persisted.last_fire_at is not None


def test_fire_loop_runs_pre_command_for_extra_variables(
    tmp_path: Path, repo_path: Path
) -> None:
    """pre_command ``KEY=value`` stdout is injected into the template."""
    recipe_path = tmp_path / "github-trending.md"
    recipe_path.write_text(
        """---
id: github-trending
schedule: "0 8 * * *"
repo_id: fire-demo
---

Trending repo: {{trending_repo}} (count={{count}})
""",
        encoding="utf-8",
    )
    task = LoopTask(
        id="github-trending",
        recipe_path=recipe_path,
        repo_id="fire-demo",
        schedule=LoopSchedule(kind=LoopScheduleKind.CRON, expression="0 8 * * *"),
        pre_command="printf 'trending_repo=demo/x\\ncount=7\\n'",
        publish_prd=False,
    )

    fake_github = FakeGitHubClient(
        issue_url="https://github.com/example/fire-demo/issues/123"
    )
    fake_github.set_list_issues_by_label_result([])
    state_store = JsonLoopStateStore(tmp_path / "loop-state.json")
    clock = FixedClock(datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc))
    process_runner = FakeProcessRunner(
        responses={
            ("/bin/sh", "-c", task.pre_command): CommandResult(
                command=("/bin/sh", "-c"),
                return_code=0,
                stdout="trending_repo=demo/x\ncount=7\n",
                stderr="",
            )
        }
    )

    result = fire_loop(
        task,
        repo_path=repo_path,
        github_client=fake_github,
        process_runner=process_runner,
        state_store=state_store,
        clock=clock,
    )
    assert result.status is LoopFireStatus.FIRED
    prd_text = (repo_path / result.relative_prd_path).read_text(encoding="utf-8")
    assert "Trending repo: demo/x (count=7)" in prd_text
