"""Tests for the loop recipe parser and template renderer."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.core.use_cases.loop_recipe import (
    parse_loop_recipe,
    parse_loop_recipe_text,
    parse_pre_command_output,
    render_loop_recipe,
    render_loop_recipe_title,
)


def _write_recipe(tmp_path: Path, body: str = "## Summary\n\nBody {{date}}") -> Path:
    recipe_path = tmp_path / "test-loop.md"
    recipe_path.write_text(body, encoding="utf-8")
    return recipe_path


def test_parse_loop_recipe_minimal(tmp_path: Path) -> None:
    recipe_path = _write_recipe(
        tmp_path,
        """---
id: github-trending
schedule: 0 8 * * *
repo_id: keda-main
---

# GitHub Trending {{date}}
""",
    )
    recipe = parse_loop_recipe(recipe_path)
    assert recipe.id == "github-trending"
    assert recipe.schedule.expression == "0 8 * * *"
    assert recipe.schedule.kind.value == "cron"
    assert recipe.repo_id == "keda-main"
    assert "# GitHub Trending" in recipe.body_template
    assert recipe.issue_type == "feature"
    assert recipe.agent == "auto"
    assert recipe.publish_prd is True
    assert recipe.queue_ready is True
    assert recipe.run_now is False
    assert recipe.default_labels() == ("loop/github-trending",)


def test_parse_loop_recipe_with_labels_and_interval(tmp_path: Path) -> None:
    recipe_path = _write_recipe(
        tmp_path,
        """---
id: hourly
schedule: 30m
repo_id: keda-main
issue_type: refactor
agent: codex
labels:
  - area/docs
  - priority/high
publish_prd: false
queue_ready: false
run_now: true
priority: P1
slug: hourly-trending
pre_command: echo "ready=ok"
timezone: UTC
---

Body
""",
    )
    recipe = parse_loop_recipe(recipe_path)
    assert recipe.schedule.kind.value == "interval"
    assert recipe.schedule.expression == "30m"
    assert recipe.issue_type == "refactor"
    assert recipe.agent == "codex"
    assert recipe.labels == ("area/docs", "priority/high")
    assert recipe.publish_prd is False
    assert recipe.queue_ready is False
    assert recipe.run_now is True
    assert recipe.priority == "P1"
    assert recipe.slug == "hourly-trending"
    assert recipe.pre_command == 'echo "ready=ok"'
    assert recipe.timezone_name == "UTC"
    assert recipe.effective_slug() == "hourly-trending"
    assert recipe.all_labels() == ("loop/hourly", "area/docs", "priority/high")


def test_parse_loop_recipe_comma_separated_labels(tmp_path: Path) -> None:
    recipe_path = _write_recipe(
        tmp_path,
        """---
id: csv
schedule: 1h
repo_id: keda-main
labels: "a, b, c"
---

Body
""",
    )
    recipe = parse_loop_recipe(recipe_path)
    assert recipe.labels == ("a", "b", "c")


def test_parse_loop_recipe_missing_field_raises(tmp_path: Path) -> None:
    recipe_path = _write_recipe(
        tmp_path,
        """---
id: missing-schedule
repo_id: keda-main
---
""",
    )
    with pytest.raises(ValueError, match="schedule"):
        parse_loop_recipe(recipe_path)


def test_parse_loop_recipe_invalid_id_raises(tmp_path: Path) -> None:
    recipe_path = _write_recipe(
        tmp_path,
        """---
id: "Not_Kebab"
schedule: 0 8 * * *
repo_id: keda-main
---
""",
    )
    with pytest.raises(ValueError, match="kebab-case"):
        parse_loop_recipe(recipe_path)


def test_parse_loop_recipe_invalid_issue_type_raises(tmp_path: Path) -> None:
    recipe_path = _write_recipe(
        tmp_path,
        """---
id: typo
schedule: 0 8 * * *
repo_id: keda-main
issue_type: typo
---
""",
    )
    with pytest.raises(ValueError, match="issue_type"):
        parse_loop_recipe(recipe_path)


def test_parse_loop_recipe_invalid_schedule_raises(tmp_path: Path) -> None:
    recipe_path = _write_recipe(
        tmp_path,
        """---
id: bad
schedule: "every 5 minutes"
repo_id: keda-main
---
""",
    )
    with pytest.raises(ValueError, match="Unsupported loop schedule"):
        parse_loop_recipe(recipe_path)


def test_parse_loop_recipe_no_frontmatter(tmp_path: Path) -> None:
    recipe_path = _write_recipe(tmp_path, "# Just a title\n\nbody")
    with pytest.raises(ValueError, match="missing required field"):
        parse_loop_recipe(recipe_path)


def test_render_loop_recipe_built_in_variables(tmp_path: Path) -> None:
    recipe_path = _write_recipe(
        tmp_path,
        """---
id: render
schedule: 0 8 * * *
repo_id: keda-main
---
# Loop {{loop_id}} for {{repo_id}} on {{date}} at {{timestamp}}
""",
    )
    recipe = parse_loop_recipe(recipe_path)
    fire_at = datetime(2026, 6, 23, 8, 0, 0)
    rendered = render_loop_recipe(recipe, fire_at=fire_at)
    assert "Loop render for keda-main on 2026-06-23 at 20260623-080000" in rendered


def test_render_loop_recipe_extra_variables(tmp_path: Path) -> None:
    recipe_path = _write_recipe(
        tmp_path,
        """---
id: ext
schedule: 0 8 * * *
repo_id: keda-main
---
Trending repo: {{repo}} (count={{count}})
""",
    )
    recipe = parse_loop_recipe(recipe_path)
    rendered = render_loop_recipe(
        recipe,
        fire_at=datetime(2026, 1, 1, 0, 0, 0),
        extra_variables={"repo": "demo/demo", "count": "5"},
    )
    assert "Trending repo: demo/demo (count=5)" in rendered


def test_render_loop_recipe_undefined_variable_raises(tmp_path: Path) -> None:
    recipe_path = _write_recipe(
        tmp_path,
        """---
id: undef
schedule: 0 8 * * *
repo_id: keda-main
---
{{missing}}
""",
    )
    recipe = parse_loop_recipe(recipe_path)
    with pytest.raises(ValueError, match="undefined variable"):
        render_loop_recipe(recipe, fire_at=datetime(2026, 1, 1, 0, 0, 0))


def test_render_loop_recipe_title_extracts_h1(tmp_path: Path) -> None:
    recipe_path = _write_recipe(
        tmp_path,
        """---
id: titled
schedule: 0 8 * * *
repo_id: keda-main
---
# Daily Report {{date}}

rest
""",
    )
    recipe = parse_loop_recipe(recipe_path)
    title = render_loop_recipe_title(recipe, fire_at=datetime(2026, 6, 23, 0, 0, 0))
    assert title == "Daily Report 2026-06-23"


def test_render_loop_recipe_title_falls_back_to_id(tmp_path: Path) -> None:
    recipe_path = _write_recipe(
        tmp_path,
        """---
id: fallback
schedule: 0 8 * * *
repo_id: keda-main
---
No h1 line here.
""",
    )
    recipe = parse_loop_recipe(recipe_path)
    assert render_loop_recipe_title(recipe, fire_at=datetime(2026, 1, 1, 0, 0, 0)) == "fallback"


def test_parse_pre_command_output_handles_quotes_and_comments() -> None:
    stdout = """
# pre-command output
greeting = "hello world"
count = 3
name=alice
not_a_kv_line
flag = true
"""
    parsed = parse_pre_command_output(stdout)
    assert parsed == {
        "greeting": "hello world",
        "count": "3",
        "name": "alice",
        "flag": "true",
    }


def test_parse_loop_recipe_text_requires_source_path(tmp_path: Path) -> None:
    recipe = parse_loop_recipe_text(
        """---
id: t
schedule: 0 8 * * *
repo_id: keda-main
---
Body
""",
        source_path=tmp_path / "t.md",
    )
    assert recipe.source_path == tmp_path / "t.md"
