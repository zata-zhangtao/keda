"""Tests for ``iar registry`` subcommand implementations."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


from backend.api.cli_registry import (
    _run_registry_list_command,
    _run_registry_start_command,
)
from backend.api.cli_takeover import _start_daemons_for_repo
from backend.core.shared.interfaces.runner_console import RunnerProcessKind


class _FakeArgs:
    """Lightweight namespace for CLI argument tests."""

    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def _make_record(
    *,
    process_id: str,
    repo_id: str,
    kind: str,
    status: str,
) -> MagicMock:
    record = MagicMock()
    record.process_id = process_id
    record.repo_id = repo_id
    record.kind = kind
    record.status = status
    return record


def _render_table(table) -> str:
    """Render a Rich Table to a plain string for assertions."""
    from rich.console import Console

    console = Console(force_terminal=False, width=200)
    with console.capture() as capture:
        console.print(table)
    return capture.get()


def test_registry_list_skips_non_running_records() -> None:
    """Exited/stopped records must not be displayed as running."""
    editor = MagicMock()
    editor.list_repositories.return_value = [
        MagicMock(
            repo_id="keda-main",
            display_name="Keda Main",
            path="/Users/zata/code/keda",
        )
    ]

    supervisor = MagicMock()
    supervisor.list_processes.return_value = [
        _make_record(
            process_id="abc123",
            repo_id="keda-main",
            kind=RunnerProcessKind.DAEMON.value,
            status="exited",
        ),
        _make_record(
            process_id="def456",
            repo_id="keda-main",
            kind=RunnerProcessKind.REVIEW_DAEMON.value,
            status="stopped",
        ),
    ]
    supervisor.list_unmanaged_processes.return_value = []

    captured_table = None

    def _capture_print(value) -> None:
        nonlocal captured_table
        captured_table = value

    with patch(
        "backend.api.cli_registry.create_registry_editor", return_value=editor
    ), patch(
        "backend.api.cli_registry.create_process_supervisor", return_value=supervisor
    ), patch("backend.api.cli_registry.console.print", side_effect=_capture_print):
        exit_code = _run_registry_list_command(MagicMock())

    assert exit_code == 0
    assert captured_table is not None
    output = _render_table(captured_table)
    assert "stopped" in output
    assert "running" not in output
    assert "abc123" not in output
    assert "def456" not in output


def test_registry_start_uses_config_directory_as_spawn_cwd(tmp_path: Path) -> None:
    """Daemon processes must be spawned from the config.toml directory."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("[agent_runner]\n", encoding="utf-8")
    config_dir = config_path.parent

    repo_path = tmp_path / "cloned-repo"
    repo_path.mkdir()

    settings = MagicMock()
    settings.repositories = {
        "zata-zhangtao-keda": MagicMock(path=str(repo_path), enabled=True),
    }
    settings.console.runner_command = ["iar"]

    context = MagicMock()
    context.repo_id = "zata-zhangtao-keda"

    start_mock = MagicMock()
    start_mock.return_value = MagicMock(process_id="proc123")

    parsed = _FakeArgs(
        repo_id="zata-zhangtao-keda",
        all=False,
        no_review_daemon=True,
    )

    with patch(
        "backend.api.cli_registry.load_fresh_agent_runner_settings",
        return_value=settings,
    ), patch("backend.api.cli_registry.create_process_supervisor"), patch(
        "backend.api.cli_registry.resolve_repository_targets_with_diagnostics",
        return_value=([context], []),
    ), patch(
        "backend.api.cli_registry.resolve_config_toml_path", return_value=config_path
    ), patch("backend.api.cli_registry.start_runner_process", new=start_mock):
        exit_code = _run_registry_start_command(parsed, MagicMock())

    assert exit_code == 0
    assert start_mock.call_count == 1
    call_kwargs = start_mock.call_args.kwargs
    assert call_kwargs["repo_id"] == "zata-zhangtao-keda"
    assert call_kwargs["spawn_cwd"] == config_dir
    assert call_kwargs["spawn_cwd"] != repo_path


def test_registry_start_rejects_missing_repo_path(tmp_path: Path) -> None:
    """Starting a daemon for a missing repository path should fail gracefully."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("[agent_runner]\n", encoding="utf-8")

    missing_path = tmp_path / "does-not-exist"
    settings = MagicMock()
    settings.repositories = {
        "ghost-repo": MagicMock(path=str(missing_path), enabled=True),
    }
    settings.console.runner_command = ["iar"]

    start_mock = MagicMock()
    parsed = _FakeArgs(
        repo_id="ghost-repo",
        all=False,
        no_review_daemon=True,
    )

    with patch(
        "backend.api.cli_registry.load_fresh_agent_runner_settings",
        return_value=settings,
    ), patch("backend.api.cli_registry.create_process_supervisor"), patch(
        "backend.api.cli_registry.resolve_repository_targets_with_diagnostics",
        return_value=([], []),
    ), patch(
        "backend.api.cli_registry.resolve_config_toml_path", return_value=config_path
    ), patch("backend.api.cli_registry.start_runner_process", new=start_mock):
        exit_code = _run_registry_start_command(parsed, MagicMock())

    assert exit_code == 1
    start_mock.assert_not_called()


def test_registry_list_includes_running_records() -> None:
    """Running records must still appear as running."""
    editor = MagicMock()
    editor.list_repositories.return_value = [
        MagicMock(
            repo_id="keda-main",
            display_name="Keda Main",
            path="/Users/zata/code/keda",
        )
    ]

    supervisor = MagicMock()
    supervisor.list_processes.return_value = [
        _make_record(
            process_id="abc123",
            repo_id="keda-main",
            kind=RunnerProcessKind.DAEMON.value,
            status="running",
        ),
    ]
    supervisor.list_unmanaged_processes.return_value = []

    captured_table = None

    def _capture_print(value) -> None:
        nonlocal captured_table
        captured_table = value

    with patch(
        "backend.api.cli_registry.create_registry_editor", return_value=editor
    ), patch(
        "backend.api.cli_registry.create_process_supervisor", return_value=supervisor
    ), patch("backend.api.cli_registry.console.print", side_effect=_capture_print):
        exit_code = _run_registry_list_command(MagicMock())

    assert exit_code == 0
    assert captured_table is not None
    output = _render_table(captured_table)
    assert "running (abc123)" in output


def test_registry_list_shows_unmanaged_running() -> None:
    """Unmanaged running processes must be shown as running (unmanaged)."""
    editor = MagicMock()
    editor.list_repositories.return_value = [
        MagicMock(
            repo_id="keda-main",
            display_name="Keda Main",
            path="/Users/zata/code/keda",
        )
    ]

    supervisor = MagicMock()
    supervisor.list_processes.return_value = []
    supervisor.list_unmanaged_processes.return_value = [
        _make_record(
            process_id="unmanaged-12345",
            repo_id="keda-main",
            kind=RunnerProcessKind.DAEMON.value,
            status="running",
        ),
    ]

    captured_table = None

    def _capture_print(value) -> None:
        nonlocal captured_table
        captured_table = value

    with patch(
        "backend.api.cli_registry.create_registry_editor", return_value=editor
    ), patch(
        "backend.api.cli_registry.create_process_supervisor", return_value=supervisor
    ), patch("backend.api.cli_registry.console.print", side_effect=_capture_print):
        exit_code = _run_registry_list_command(MagicMock())

    assert exit_code == 0
    assert captured_table is not None
    output = _render_table(captured_table)
    assert "running" in output
    assert "unmanaged" in output
    assert "unmanaged-12345" not in output


def test_registry_list_prefers_managed_over_unmanaged() -> None:
    """When both managed and unmanaged processes exist, show managed status."""
    editor = MagicMock()
    editor.list_repositories.return_value = [
        MagicMock(
            repo_id="keda-main",
            display_name="Keda Main",
            path="/Users/zata/code/keda",
        )
    ]

    supervisor = MagicMock()
    supervisor.list_processes.return_value = [
        _make_record(
            process_id="managed-abc",
            repo_id="keda-main",
            kind=RunnerProcessKind.DAEMON.value,
            status="running",
        ),
    ]
    supervisor.list_unmanaged_processes.return_value = [
        _make_record(
            process_id="unmanaged-12345",
            repo_id="keda-main",
            kind=RunnerProcessKind.DAEMON.value,
            status="running",
        ),
    ]

    captured_table = None

    def _capture_print(value) -> None:
        nonlocal captured_table
        captured_table = value

    with patch(
        "backend.api.cli_registry.create_registry_editor", return_value=editor
    ), patch(
        "backend.api.cli_registry.create_process_supervisor", return_value=supervisor
    ), patch("backend.api.cli_registry.console.print", side_effect=_capture_print):
        exit_code = _run_registry_list_command(MagicMock())

    assert exit_code == 0
    assert captured_table is not None
    output = _render_table(captured_table)
    assert "managed-abc" in output
    assert "unmanaged" not in output


def test_takeover_start_daemons_uses_config_directory_as_spawn_cwd(
    tmp_path: Path,
) -> None:
    """Takeover daemon start must spawn from the config.toml directory."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("[agent_runner]\n", encoding="utf-8")
    config_dir = config_path.parent

    repo_path = tmp_path / "cloned-repo"
    repo_path.mkdir()

    settings = MagicMock()
    settings.console.runner_command = ["iar"]

    context = MagicMock()
    context.repo_id = "zata-zhangtao-keda"

    start_mock = MagicMock()
    start_mock.return_value = MagicMock(process_id="proc456")

    with patch(
        "backend.api.cli_takeover.load_fresh_agent_runner_settings",
        return_value=settings,
    ), patch("backend.api.cli_takeover.create_process_supervisor"), patch(
        "backend.api.cli_takeover.resolve_repository_targets_with_diagnostics",
        return_value=([context], []),
    ), patch(
        "backend.api.cli_takeover.resolve_config_toml_path", return_value=config_path
    ), patch("backend.api.cli_takeover.start_runner_process", new=start_mock):
        _start_daemons_for_repo("zata-zhangtao-keda", repo_path)

    assert start_mock.call_count == 2
    for call in start_mock.call_args_list:
        assert call.kwargs["spawn_cwd"] == config_dir
        assert call.kwargs["spawn_cwd"] != repo_path
