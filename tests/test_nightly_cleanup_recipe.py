"""Tests for the nightly-cleanup loop recipes.

The two recipes (keda and product) are content-only additions to
``tasks/loops/``. They must:

* parse successfully via :func:`parse_loop_recipe`;
* expose the fields the iar-loop subsystem requires
  (``run_now=true`` / ``queue_ready=true`` / ``publish_prd=true`` /
  ``labels=[loop/cleanup]`` / ``slug``);
* render the built-in variables (``{{date}}`` / ``{{timestamp}}`` /
  ``{{loop_id}}`` / ``{{repo_id}}``) when fired;
* carry the four scope H2 sections plus the ``Triage 优先级`` section in
  the body — the body is the spec the agent reads at fire time.

These tests run against the real recipe files in ``tasks/loops/`` and
through the real ``parse_loop_recipe`` / ``render_loop_recipe`` entry
points. They also include negative-control cases (deliberately
malformed recipe) to prove the parser actually fails when given bad
input.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.core.use_cases.loop_recipe import (
    parse_loop_recipe,
    render_loop_recipe,
    render_loop_recipe_title,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
KEDA_RECIPE = REPO_ROOT / "tasks" / "loops" / "nightly-cleanup-keda.md"
PRODUCT_RECIPE = REPO_ROOT / "tasks" / "loops" / "nightly-cleanup-product.md"

EXPECTED_SCOPE_HEADINGS: tuple[str, ...] = (
    "## 1. CI 失败",
    "## 2. 重复代码",
    "## 3. 文档",
    "## 4. 依赖",
    "## Triage 优先级",
)

EXPECTED_SCOPE_LABELS: tuple[str, ...] = (
    "scope/ci",
    "scope/refactor",
    "scope/docs",
    "scope/deps",
)

# The recipes use the ``scope/<x>`` template form (where x is the variable
# that agent fills in at fire time), so the body must show the template plus
# the enumeration of valid x values.
EXPECTED_SCOPE_TEMPLATE = "scope/<x>"
EXPECTED_SCOPE_X_VALUES: tuple[str, ...] = ("ci", "refactor", "docs", "deps")


def _extra_vars() -> dict[str, str]:
    """Sample pre_command output for render tests."""
    return {"last_ci_status": "success", "outdated_count": "3"}


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def test_parse_keda_recipe_succeeds() -> None:
    recipe = parse_loop_recipe(KEDA_RECIPE)
    assert recipe.id == "nightly-cleanup-keda"
    assert recipe.schedule.kind.value == "cron"
    assert recipe.schedule.expression == "0 2 * * *"
    assert recipe.repo_id == "keda"
    assert recipe.run_now is True
    assert recipe.queue_ready is True
    assert recipe.publish_prd is True
    assert recipe.issue_type == "feature"
    assert recipe.agent == "auto"
    assert "loop/cleanup" in recipe.labels
    assert recipe.slug == "nightly-cleanup-keda"
    assert recipe.timezone_name == "Asia/Shanghai"
    assert recipe.priority == "P1"
    assert recipe.pre_command is not None
    assert "gh run list" in recipe.pre_command
    assert "outdated_count" in recipe.pre_command


def test_parse_product_recipe_succeeds() -> None:
    recipe = parse_loop_recipe(PRODUCT_RECIPE)
    assert recipe.id == "nightly-cleanup-product"
    assert recipe.schedule.kind.value == "cron"
    assert recipe.schedule.expression == "30 2 * * *"
    assert recipe.run_now is True
    assert recipe.queue_ready is True
    assert recipe.publish_prd is True
    assert "loop/cleanup" in recipe.labels
    assert recipe.slug == "nightly-cleanup-product"
    assert recipe.timezone_name == "Asia/Shanghai"
    assert recipe.priority == "P1"
    assert recipe.pre_command is not None
    assert "gh run list" in recipe.pre_command
    # Product recipe should be tech-stack aware (handle uv / npm / pnpm).
    assert "uv.lock" in recipe.pre_command
    assert "package-lock.json" in recipe.pre_command
    assert "pnpm-lock.yaml" in recipe.pre_command


# ---------------------------------------------------------------------------
# Built-in variable rendering
# ---------------------------------------------------------------------------


def test_render_keda_recipe_built_in_variables() -> None:
    recipe = parse_loop_recipe(KEDA_RECIPE)
    fire_at = datetime(2026, 7, 2, 2, 0, 0)
    rendered = render_loop_recipe(recipe, fire_at=fire_at, extra_variables=_extra_vars())
    assert "Trigger date: `2026-07-02`" in rendered
    assert "Loop id: `nightly-cleanup-keda`" in rendered
    assert "Target repository: `keda`" in rendered


def test_render_product_recipe_built_in_variables() -> None:
    recipe = parse_loop_recipe(PRODUCT_RECIPE)
    fire_at = datetime(2026, 7, 2, 2, 30, 0)
    rendered = render_loop_recipe(recipe, fire_at=fire_at, extra_variables=_extra_vars())
    assert "Trigger date: `2026-07-02`" in rendered
    assert "Loop id: `nightly-cleanup-product`" in rendered


# ---------------------------------------------------------------------------
# pre_command variable rendering
# ---------------------------------------------------------------------------


def test_render_keda_recipe_pre_command_variables() -> None:
    recipe = parse_loop_recipe(KEDA_RECIPE)
    rendered = render_loop_recipe(
        recipe,
        fire_at=datetime(2026, 7, 2, 2, 0, 0),
        extra_variables={"last_ci_status": "failure", "outdated_count": "12"},
    )
    assert "Last CI status: `failure`" in rendered
    assert "Outdated dependency count: `12`" in rendered


def test_render_product_recipe_pre_command_variables() -> None:
    recipe = parse_loop_recipe(PRODUCT_RECIPE)
    rendered = render_loop_recipe(
        recipe,
        fire_at=datetime(2026, 7, 2, 2, 30, 0),
        extra_variables={"last_ci_status": "timed_out", "outdated_count": "0"},
    )
    assert "Last CI status: `timed_out`" in rendered
    assert "Outdated dependency count: `0`" in rendered


# ---------------------------------------------------------------------------
# Body shape — 4 scope sections + Triage 优先级
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe_path", [KEDA_RECIPE, PRODUCT_RECIPE])
def test_body_has_four_scope_sections(recipe_path: Path) -> None:
    recipe = parse_loop_recipe(recipe_path)
    rendered = render_loop_recipe(
        recipe,
        fire_at=datetime(2026, 7, 2, 2, 0, 0),
        extra_variables=_extra_vars(),
    )
    for heading in EXPECTED_SCOPE_HEADINGS:
        assert (
            heading in rendered
        ), f"Recipe {recipe_path.name} body missing scope heading: {heading!r}"


@pytest.mark.parametrize("recipe_path", [KEDA_RECIPE, PRODUCT_RECIPE])
def test_body_references_all_four_scope_labels(recipe_path: Path) -> None:
    """The body must show how to apply ``scope/<x>`` and enumerate the
    four valid ``x`` values so the agent knows which label to add once
    it picks a scope."""
    recipe = parse_loop_recipe(recipe_path)
    rendered = render_loop_recipe(
        recipe,
        fire_at=datetime(2026, 7, 2, 2, 0, 0),
        extra_variables=_extra_vars(),
    )
    assert EXPECTED_SCOPE_TEMPLATE in rendered, (
        f"Recipe {recipe_path.name} body missing scope template: " f"{EXPECTED_SCOPE_TEMPLATE!r}"
    )
    for scope_value in EXPECTED_SCOPE_X_VALUES:
        assert scope_value in rendered, (
            f"Recipe {recipe_path.name} body missing scope value: " f"{scope_value!r}"
        )


@pytest.mark.parametrize("recipe_path", [KEDA_RECIPE, PRODUCT_RECIPE])
def test_body_documents_pr_title_prefix(recipe_path: Path) -> None:
    """Both recipes must instruct the agent to prefix PR titles with
    ``cleanup(<scope>):`` so the maintainer can filter on it."""
    recipe = parse_loop_recipe(recipe_path)
    rendered = render_loop_recipe(
        recipe,
        fire_at=datetime(2026, 7, 2, 2, 0, 0),
        extra_variables=_extra_vars(),
    )
    assert "cleanup(<scope>)" in rendered


# ---------------------------------------------------------------------------
# Title rendering (used by fire_loop for the GitHub Issue title)
# ---------------------------------------------------------------------------


def test_render_keda_recipe_title() -> None:
    recipe = parse_loop_recipe(KEDA_RECIPE)
    title = render_loop_recipe_title(
        recipe,
        fire_at=datetime(2026, 7, 2, 2, 0, 0),
        extra_variables=_extra_vars(),
    )
    assert "夜间清理" in title
    assert "2026-07-02" in title


def test_render_product_recipe_title() -> None:
    recipe = parse_loop_recipe(PRODUCT_RECIPE)
    title = render_loop_recipe_title(
        recipe,
        fire_at=datetime(2026, 7, 2, 2, 30, 0),
        extra_variables=_extra_vars(),
    )
    assert "夜间清理" in title
    assert "2026-07-02" in title


# ---------------------------------------------------------------------------
# Negative control — prove the parser actually fails on bad input
# ---------------------------------------------------------------------------


def test_parse_loop_recipe_missing_schedule_fails(tmp_path: Path) -> None:
    """If we remove the schedule field, parsing must fail loudly.

    This is the negative-control half of rv-1: a green test is only
    meaningful if the same path reddens when the contract is broken.
    """
    bad_recipe = tmp_path / "broken-keda.md"
    bad_recipe.write_text(
        """---
id: nightly-cleanup-keda
repo_id: keda
run_now: true
---

body
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schedule"):
        parse_loop_recipe(bad_recipe)


def test_parse_loop_recipe_missing_run_now_defaults_to_false(tmp_path: Path) -> None:
    """run_now has a default of ``False`` in :class:`LoopRecipe`; the
    recipe must opt in by setting it explicitly. This guards against
    an accidental regression where someone deletes the ``run_now: true``
    line — the parser should still succeed (the field is optional) but
    the resulting :class:`LoopRecipe` must report ``run_now=False``."""
    bad_recipe = tmp_path / "no-run-now.md"
    bad_recipe.write_text(
        """---
id: nightly-cleanup-keda
schedule: "0 2 * * *"
repo_id: keda
---

body
""",
        encoding="utf-8",
    )
    recipe = parse_loop_recipe(bad_recipe)
    assert recipe.run_now is False


# ---------------------------------------------------------------------------
# Integration: real fire with mocked GitHub client + process runner
# ---------------------------------------------------------------------------


def test_fire_keda_recipe_writes_prd_creates_issue_and_calls_runner(
    tmp_path: Path,
) -> None:
    """End-to-end check that the real :file:`tasks/loops/nightly-cleanup-keda.md`
    recipe, fed through the real :func:`fire_loop` with a mocked GitHub
    client and a mocked process runner, produces:

    1. a PRD file under ``tasks/pending/``;
    2. a GitHub Issue with the ``loop/cleanup`` / ``loop/<id>`` labels and
       the standard runner labels (``type/feature`` / ``status/backlog`` /
       ``source/prd``);
    3. an updated ``loop-state.json`` (fire_count == 1).

    This is the unit-level evidence backing rv-3 (real fire writes PRD +
    creates Issue + calls runner). The actual ``run_agent_repositories_once``
    call happens via the daemon after the Issue is created; here we verify
    the prerequisite conditions (PRD + Issue with correct labels).
    """
    from datetime import datetime, timezone

    from backend.core.shared.models.agent_runner import (
        CommandResult,
        LabelConfig,
    )
    from backend.core.shared.models.loop import (
        LoopSchedule,
        LoopScheduleKind,
        LoopTask,
    )
    from backend.core.use_cases.loop_fire import fire_loop
    from backend.engines.agent_runner.persistence.loop_state_json import (
        JsonLoopStateStore,
    )
    from backend.engines.agent_runner.scheduler.loop_clock import FixedClock

    # Re-use the same FakeGitHubClient / FakeProcessRunner that
    # test_loop_fire.py uses so the test exercises the production fire
    # path with a real in-memory collaborator (not a hand-rolled stub).
    from tests.conftest import FakeGitHubClient, FakeProcessRunner

    repo_path = tmp_path / "repo"
    (repo_path / "tasks" / "pending").mkdir(parents=True)
    recipe = parse_loop_recipe(KEDA_RECIPE)

    fake_github = FakeGitHubClient(issue_url="https://github.com/example/keda/issues/777")
    fake_github.set_list_issues_by_label_result([])  # no duplicate today

    state_store = JsonLoopStateStore(tmp_path / "loop-state.json")
    clock = FixedClock(datetime(2026, 7, 3, 2, 0, tzinfo=timezone.utc))
    process_runner = FakeProcessRunner(
        responses={
            (
                "/bin/sh",
                "-c",
                recipe.pre_command,
            ): CommandResult(
                command=("/bin/sh", "-c"),
                return_code=0,
                stdout="last_ci_status=success\noutdated_count=3\n",
                stderr="",
            )
        }
    )

    task = LoopTask(
        id=recipe.id,
        recipe_path=KEDA_RECIPE,
        repo_id=recipe.repo_id,
        schedule=LoopSchedule(kind=LoopScheduleKind.CRON, expression=recipe.schedule.expression),
        pre_command=recipe.pre_command,
        # publish_prd=True requires a real git checkout on the base branch;
        # the unit-level fire path uses publish_prd=False to avoid that
        # coupling. The recipe still records publish_prd=True for real fires.
        publish_prd=False,
        queue_ready=recipe.queue_ready,
        run_now=recipe.run_now,
        priority=recipe.priority,
        labels=recipe.labels,
        slug=recipe.slug,
        timezone_name=recipe.timezone_name,
    )

    result = fire_loop(
        task,
        repo_path=repo_path,
        github_client=fake_github,
        process_runner=process_runner,
        state_store=state_store,
        clock=clock,
        labels_config=LabelConfig(),
    )

    # 1) PRD file written with rendered body and all four scope sections.
    assert result.status.value == "fired"
    assert result.prd_path is not None
    assert result.prd_path.is_file()
    prd_text = result.prd_path.read_text(encoding="utf-8")
    for heading in EXPECTED_SCOPE_HEADINGS:
        assert heading in prd_text
    # pre_command variables were rendered into the body.
    assert "Last CI status: `success`" in prd_text
    assert "Outdated dependency count: `3`" in prd_text

    # 2) GitHub Issue created with the expected base + loop-specific labels.
    create_calls = [c for c in fake_github.calls if c["method"] == "create_issue"]
    assert len(create_calls) == 1
    create_labels = create_calls[0]["labels"]
    assert "type/feature" in create_labels
    assert "agent/ready" in create_labels  # queue_ready=True
    # loop-specific label is added in a follow-up edit_issue_labels call.
    edit_calls = [c for c in fake_github.calls if c["method"] == "edit_issue_labels"]
    assert len(edit_calls) == 1
    assert "loop/nightly-cleanup-keda" in edit_calls[0]["add"]
    assert "loop/cleanup" in edit_calls[0]["add"]

    # 3) Loop state updated.
    state_store.load()
    persisted = state_store.get_task("nightly-cleanup-keda")
    assert persisted is not None
    assert persisted.fire_count == 1
    assert persisted.last_fire_at is not None
    assert persisted.next_fire_at is not None
