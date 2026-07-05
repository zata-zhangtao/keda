"""Tests for the loop CLI surface (argparse + Typer) and dispatcher."""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backend.api import cli_loop
from backend.api.cli_parser import build_parser
from backend.api.cli_typer import app as typer_app
from backend.engines.agent_runner.persistence.loop_state_json import JsonLoopStateStore
from backend.engines.agent_runner.scheduler.loop_clock import FixedClock
from tests.conftest import FakeGitHubClient, FakeProcessRunner

# Click 8.2+ 在 CliRunner 中根据环境变量(FORCE_COLOR 等)插入 ANSI 颜色码,
# 会把 `--recipe` 拆成 `-\x1b[0m\x1b[1;36m-recipe\x1b[0m`,使连续字符串断言失败。
# help 内容测试只关心文本本身,与颜色无关,因此断言前先剥离 ANSI 转义码。
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ---------------------------------------------------------------------------
# Argparse parity
# ---------------------------------------------------------------------------


def _parser_for(args: list[str]) -> argparse.ArgumentParser:
    return build_parser()


def test_argparse_loop_list_loads() -> None:
    parser = _parser_for([])
    ns = parser.parse_args(["loop", "list"])
    assert ns.command == "loop list"


def test_argparse_loop_create_loads() -> None:
    parser = _parser_for([])
    ns = parser.parse_args(
        [
            "loop",
            "create",
            "github-trending",
            "--recipe",
            "tasks/loops/x.md",
            "--cron",
            "0 8 * * *",
        ]
    )
    assert ns.command == "loop create"
    assert ns.loop_id == "github-trending"
    assert ns.cron == "0 8 * * *"
    assert ns.recipe == "tasks/loops/x.md"


def test_argparse_loop_run_loads() -> None:
    parser = _parser_for([])
    ns = parser.parse_args(["loop", "run", "--now", "github-trending"])
    assert ns.command == "loop run"
    assert ns.loop_id == "github-trending"
    assert ns.now is True


def test_argparse_loop_daemon_loads() -> None:
    parser = _parser_for([])
    ns = parser.parse_args(["loop-daemon", "--interval", "10", "--dry-run"])
    assert ns.command == "loop-daemon"
    assert ns.interval == 10
    assert ns.dry_run is True


# ---------------------------------------------------------------------------
# Typer parity
# ---------------------------------------------------------------------------


runner = CliRunner()


def test_typer_loop_create_help_matches_argparse() -> None:
    result = runner.invoke(typer_app, ["loop", "create", "--help"])
    assert result.exit_code == 0
    stdout = _ANSI_ESCAPE_RE.sub("", result.stdout)
    assert "--recipe" in stdout
    assert "--cron" in stdout
    assert "--every" in stdout


def test_typer_loop_run_help_matches_argparse() -> None:
    result = runner.invoke(typer_app, ["loop", "run", "--help"])
    assert result.exit_code == 0
    stdout = _ANSI_ESCAPE_RE.sub("", result.stdout)
    assert "--now" in stdout
    assert "--dry-run" in stdout


def test_typer_loop_daemon_help_matches_argparse() -> None:
    result = runner.invoke(typer_app, ["loop-daemon", "--help"])
    assert result.exit_code == 0
    stdout = _ANSI_ESCAPE_RE.sub("", result.stdout)
    assert "--interval" in stdout
    assert "--dry-run" in stdout


# ---------------------------------------------------------------------------
# run_loop_*_command handlers
# ---------------------------------------------------------------------------


_RECIPE_BODY = """---
id: cli-demo
schedule: 0 8 * * *
repo_id: cli-demo-repo
issue_type: feature
---

# CLI demo {{date}}
"""


@pytest.fixture
def recipe_path(tmp_path: Path) -> Path:
    path = tmp_path / "cli-demo.md"
    path.write_text(_RECIPE_BODY, encoding="utf-8")
    return path


def test_run_loop_create_command_persists(
    recipe_path: Path,
    tmp_path: Path,
) -> None:
    state_store = JsonLoopStateStore(tmp_path / "loop-state.json")

    def state_store_factory() -> JsonLoopStateStore:
        return state_store

    parsed = argparse.Namespace(
        command="loop create",
        loop_id="cli-demo",
        recipe=str(recipe_path),
        cron=None,
        every=None,
        force=False,
        loop_repo_id=None,
        loop_repo=None,
    )
    rc = cli_loop.run_loop_create_command(parsed, state_store_factory=state_store_factory)
    assert rc == 0
    state_store.load()
    assert state_store.get_task("cli-demo") is not None


def test_run_loop_create_command_rejects_id_mismatch(
    recipe_path: Path,
    tmp_path: Path,
) -> None:
    state_store = JsonLoopStateStore(tmp_path / "loop-state.json")
    parsed = argparse.Namespace(
        command="loop create",
        loop_id="different-id",
        recipe=str(recipe_path),
        cron=None,
        every=None,
        force=False,
        loop_repo_id=None,
        loop_repo=None,
    )
    rc = cli_loop.run_loop_create_command(parsed, state_store_factory=lambda: state_store)
    assert rc == 1


def test_run_loop_list_command(
    recipe_path: Path,
    tmp_path: Path,
) -> None:
    state_store = JsonLoopStateStore(tmp_path / "loop-state.json")

    def state_store_factory() -> JsonLoopStateStore:
        return state_store

    parsed = argparse.Namespace(
        command="loop create",
        loop_id="cli-demo",
        recipe=str(recipe_path),
        cron=None,
        every=None,
        force=False,
        loop_repo_id=None,
        loop_repo=None,
    )
    cli_loop.run_loop_create_command(parsed, state_store_factory=state_store_factory)
    rc = cli_loop.run_loop_list_command(state_store_factory=state_store_factory)
    assert rc == 0


def test_run_loop_cancel_command(
    recipe_path: Path,
    tmp_path: Path,
) -> None:
    state_store = JsonLoopStateStore(tmp_path / "loop-state.json")

    def state_store_factory() -> JsonLoopStateStore:
        return state_store

    parsed = argparse.Namespace(
        command="loop create",
        loop_id="cli-demo",
        recipe=str(recipe_path),
        cron=None,
        every=None,
        force=False,
        loop_repo_id=None,
        loop_repo=None,
    )
    cli_loop.run_loop_create_command(parsed, state_store_factory=state_store_factory)
    cancel_ns = argparse.Namespace(command="loop cancel", loop_id="cli-demo")
    rc = cli_loop.run_loop_cancel_command(cancel_ns, state_store_factory=state_store_factory)
    assert rc == 0
    missing_ns = argparse.Namespace(command="loop cancel", loop_id="missing")
    rc = cli_loop.run_loop_cancel_command(missing_ns, state_store_factory=state_store_factory)
    assert rc == 1


def test_run_loop_run_now_dry_run(
    recipe_path: Path,
    tmp_path: Path,
) -> None:
    state_store = JsonLoopStateStore(tmp_path / "loop-state.json")

    def state_store_factory() -> JsonLoopStateStore:
        return state_store

    parsed = argparse.Namespace(
        command="loop create",
        loop_id="cli-demo",
        recipe=str(recipe_path),
        cron=None,
        every=None,
        force=False,
        loop_repo_id=None,
        loop_repo=None,
    )
    cli_loop.run_loop_create_command(parsed, state_store_factory=state_store_factory)

    run_ns = argparse.Namespace(
        command="loop run",
        loop_id="cli-demo",
        now=True,
        dry_run=True,
        loop_repo_id=None,
        loop_repo=None,
    )
    rc = cli_loop.run_loop_run_now_command(
        run_ns,
        state_store_factory=state_store_factory,
        github_client_factory=lambda path: FakeGitHubClient(),
        process_runner=FakeProcessRunner(),
        clock=FixedClock(datetime(2026, 6, 23, 7, 30, tzinfo=timezone.utc)),
        repo_resolver=lambda task: tmp_path,
    )
    assert rc == 0


def test_run_loop_run_now_missing_loop(tmp_path: Path) -> None:
    state_store = JsonLoopStateStore(tmp_path / "loop-state.json")
    run_ns = argparse.Namespace(
        command="loop run",
        loop_id="missing",
        now=True,
        dry_run=True,
        loop_repo_id=None,
        loop_repo=None,
    )
    rc = cli_loop.run_loop_run_now_command(
        run_ns,
        state_store_factory=lambda: state_store,
        github_client_factory=lambda path: FakeGitHubClient(),
        process_runner=FakeProcessRunner(),
        clock=FixedClock(datetime(2026, 6, 23, 7, 30, tzinfo=timezone.utc)),
        repo_resolver=lambda task: tmp_path,
    )
    assert rc == 1


def test_run_loop_daemon_command_dry_run(
    recipe_path: Path,
    tmp_path: Path,
) -> None:
    state_store = JsonLoopStateStore(tmp_path / "loop-state.json")

    def state_store_factory() -> JsonLoopStateStore:
        return state_store

    parsed = argparse.Namespace(
        command="loop create",
        loop_id="cli-demo",
        recipe=str(recipe_path),
        cron=None,
        every=None,
        force=False,
        loop_repo_id=None,
        loop_repo=None,
    )
    cli_loop.run_loop_create_command(parsed, state_store_factory=state_store_factory)

    parsed = argparse.Namespace(
        command="loop-daemon",
        interval=10,
        dry_run=True,
        loop_repo_id=None,
        loop_repo=None,
    )
    # Use a clock well after the registered task's next fire so the loop is due.
    clock = FixedClock(datetime(2026, 6, 23, 9, 30, tzinfo=timezone.utc))
    rc = cli_loop.run_loop_daemon_command(
        parsed,
        state_store_factory=state_store_factory,
        github_client_factory=lambda path: FakeGitHubClient(),
        process_runner=FakeProcessRunner(),
        clock=clock,
        repo_resolver=lambda task: tmp_path,
    )
    assert rc == 0


def test_run_loop_daemon_rejects_non_positive_interval(tmp_path: Path) -> None:
    parsed = argparse.Namespace(
        command="loop-daemon",
        interval=0,
        dry_run=True,
        loop_repo_id=None,
        loop_repo=None,
    )
    rc = cli_loop.run_loop_daemon_command(
        parsed,
        state_store_factory=lambda: JsonLoopStateStore(tmp_path / "loop-state.json"),
        github_client_factory=lambda path: FakeGitHubClient(),
        process_runner=FakeProcessRunner(),
        clock=FixedClock(datetime(2026, 6, 23, 9, 30, tzinfo=timezone.utc)),
        repo_resolver=lambda task: tmp_path,
    )
    assert rc == 1
