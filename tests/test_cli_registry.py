"""Tests for ``iar registry`` and ``iar logs`` subcommand implementations."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


from backend.api.cli_registry import (
    _run_daemon_status_command,
    _run_logs_command,
    _run_registry_list_command,
    _run_registry_start_command,
)
from backend.api.cli_takeover import _start_daemons_for_repo
from backend.core.shared.interfaces.runner_console import (
    ProcessLogChunk,
    RunnerProcessKind,
)


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
        "backend.api.cli_registry.resolve_registry_config_toml_path",
        return_value=config_path,
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
        "backend.api.cli_registry.resolve_registry_config_toml_path",
        return_value=config_path,
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
        "backend.api.cli_takeover.resolve_registry_config_toml_path",
        return_value=config_path,
    ), patch("backend.api.cli_takeover.start_runner_process", new=start_mock):
        _start_daemons_for_repo("zata-zhangtao-keda", repo_path)

    assert start_mock.call_count == 2
    for call in start_mock.call_args_list:
        assert call.kwargs["spawn_cwd"] == config_dir
        assert call.kwargs["spawn_cwd"] != repo_path


# ---------------------------------------------------------------------------
# iar logs tests
# ---------------------------------------------------------------------------


def _make_process_record(
    *,
    process_id: str,
    repo_id: str,
    kind: str,
    log_path: str,
    status: str = "running",
    started_at: str = "2026-06-23T00:00:00+00:00",
    pid: int = 1234,
) -> MagicMock:
    record = MagicMock()
    record.process_id = process_id
    record.repo_id = repo_id
    record.kind = kind
    record.pid = pid
    record.status = status
    record.exit_code = None
    record.log_path = log_path
    record.command = ("iar", f"{kind}", "--repo-id", repo_id)
    record.started_at = started_at
    record.stopped_at = None
    return record


def _make_log_file(parent: Path, name: str, lines: int) -> Path:
    """Write ``lines`` numbered log lines into a file and return its path."""
    log_file = parent / name
    with log_file.open("w", encoding="utf-8") as file_handle:
        for index in range(lines):
            file_handle.write(f"line {index:04d}: daemon step {index}\n")
    return log_file


def test_logs_command_prints_tail_lines(tmp_path: Path, capsys) -> None:
    """`iar logs --lines 5` prints the last 5 lines from the daemon log file."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = _make_log_file(log_dir, "daemon-abc123.log", 50)
    log_size = log_file.stat().st_size
    record = _make_process_record(
        process_id="abc123",
        repo_id="fixture-repo",
        kind=RunnerProcessKind.DAEMON.value,
        log_path=str(log_file),
    )

    context = MagicMock(repo_id="fixture-repo")
    supervisor = MagicMock()
    supervisor.list_processes.return_value = [record]
    tail_mock = MagicMock(
        side_effect=[
            ProcessLogChunk(
                content=log_file.read_text(encoding="utf-8")[
                    -min(log_size, 64 * 1024) :
                ],
                next_offset=log_size,
                eof=False,
            )
        ]
    )

    parsed = _FakeArgs(
        kind=RunnerProcessKind.DAEMON.value,
        lines=5,
        follow=False,
        repo_id="fixture-repo",
    )

    with patch(
        "backend.api.cli_registry.resolve_repository_targets",
        return_value=[context],
    ), patch(
        "backend.api.cli_registry.create_process_supervisor",
        return_value=supervisor,
    ), patch("backend.api.cli_registry.tail_runner_log", new=tail_mock):
        exit_code = _run_logs_command(
            parsed=parsed,
            process_runner=MagicMock(),
            runner_settings=MagicMock(),
            repo_id="fixture-repo",
            repo_override=None,
        )

    assert exit_code == 0
    output = capsys.readouterr().out
    out_lines = [line for line in output.splitlines() if line]
    assert out_lines[-1] == "line 0049: daemon step 49"
    assert len(out_lines) <= 5
    assert tail_mock.call_count == 1
    assert tail_mock.call_args.kwargs["process_id"] == "abc123"


def test_logs_command_fallback_when_no_records(tmp_path: Path) -> None:
    """No managed records prints the global app log fallback path."""
    context = MagicMock(repo_id="no-process-repo")
    supervisor = MagicMock()
    supervisor.list_processes.return_value = []

    parsed = _FakeArgs(
        kind=RunnerProcessKind.DAEMON.value,
        lines=10,
        follow=False,
        repo_id="no-process-repo",
    )

    printed_chunks: list[str] = []

    def _capture_console_print(*args, **kwargs) -> None:
        printable = args[0] if args else kwargs.get("__rich_object__", "")
        printed_chunks.append(str(printable))

    with patch(
        "backend.api.cli_registry.resolve_repository_targets",
        return_value=[context],
    ), patch(
        "backend.api.cli_registry.create_process_supervisor",
        return_value=supervisor,
    ), patch("backend.api.cli_registry.tail_runner_log") as tail_mock, patch(
        "backend.api.cli_registry.datetime"
    ) as datetime_mock, patch(
        "backend.api.cli_registry.console.print", side_effect=_capture_console_print
    ):
        datetime_mock.now.return_value.strftime.return_value = "2026-06-24"
        exit_code = _run_logs_command(
            parsed=parsed,
            process_runner=MagicMock(),
            runner_settings=MagicMock(),
            repo_id="no-process-repo",
            repo_override=None,
        )

    assert exit_code == 0
    tail_mock.assert_not_called()
    assert any("logs/app-2026-06-24.log" in chunk for chunk in printed_chunks)


def test_logs_command_fallback_when_log_file_missing(tmp_path: Path) -> None:
    """If the per-process log file is gone, fall back to guidance."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stale_log = log_dir / "daemon-stale.log"
    stale_log.write_text("only here for stat() before being unlinked\n")
    record = _make_process_record(
        process_id="stale-1",
        repo_id="stale-repo",
        kind=RunnerProcessKind.DAEMON.value,
        log_path=str(stale_log),
    )

    context = MagicMock(repo_id="stale-repo")
    supervisor = MagicMock()
    supervisor.list_processes.return_value = [record]
    stale_log.unlink()

    parsed = _FakeArgs(
        kind=RunnerProcessKind.DAEMON.value,
        lines=10,
        follow=False,
        repo_id="stale-repo",
    )

    printed_chunks: list[str] = []

    def _capture_console_print(*args, **kwargs) -> None:
        printable = args[0] if args else kwargs.get("__rich_object__", "")
        printed_chunks.append(str(printable))

    with patch(
        "backend.api.cli_registry.resolve_repository_targets",
        return_value=[context],
    ), patch(
        "backend.api.cli_registry.create_process_supervisor",
        return_value=supervisor,
    ), patch("backend.api.cli_registry.tail_runner_log") as tail_mock, patch(
        "backend.api.cli_registry.datetime"
    ) as datetime_mock, patch(
        "backend.api.cli_registry.console.print", side_effect=_capture_console_print
    ):
        datetime_mock.now.return_value.strftime.return_value = "2026-06-24"
        exit_code = _run_logs_command(
            parsed=parsed,
            process_runner=MagicMock(),
            runner_settings=MagicMock(),
            repo_id="stale-repo",
            repo_override=None,
        )

    assert exit_code == 0
    tail_mock.assert_not_called()
    assert any("logs/app-2026-06-24.log" in chunk for chunk in printed_chunks)


def test_logs_command_follows_new_content_then_sigint(tmp_path: Path, capsys) -> None:
    """`--follow` keeps polling; KeyboardInterrupt exits with 0."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = _make_log_file(log_dir, "daemon-follow.log", 3)
    record = _make_process_record(
        process_id="follow-1",
        repo_id="follow-repo",
        kind=RunnerProcessKind.DAEMON.value,
        log_path=str(log_file),
    )

    context = MagicMock(repo_id="follow-repo")
    supervisor = MagicMock()
    supervisor.list_processes.return_value = [record]
    supervisor.get_process.return_value = record

    initial_size = log_file.stat().st_size

    chunks = [
        ProcessLogChunk(content="initial-tail\n", next_offset=initial_size, eof=False),
        ProcessLogChunk(
            content="appended-line\n", next_offset=initial_size + 14, eof=False
        ),
    ]
    tail_mock = MagicMock(side_effect=chunks)

    # Append to the real file so stat() in next loop sees a stable value.
    log_file.write_text(
        log_file.read_text(encoding="utf-8") + "appended-line\n",
        encoding="utf-8",
    )

    sleeps = [0.0]

    def _fake_sleep(_seconds: float) -> None:
        sleeps[0] += 1
        # Trigger KeyboardInterrupt after the second poll completes.
        if sleeps[0] >= 2:
            raise KeyboardInterrupt

    parsed = _FakeArgs(
        kind=RunnerProcessKind.DAEMON.value,
        lines=2,
        follow=True,
        repo_id="follow-repo",
    )

    with patch(
        "backend.api.cli_registry.resolve_repository_targets",
        return_value=[context],
    ), patch(
        "backend.api.cli_registry.create_process_supervisor",
        return_value=supervisor,
    ), patch("backend.api.cli_registry.tail_runner_log", new=tail_mock), patch(
        "backend.api.cli_registry.time.sleep", side_effect=_fake_sleep
    ):
        exit_code = _run_logs_command(
            parsed=parsed,
            process_runner=MagicMock(),
            runner_settings=MagicMock(),
            repo_id="follow-repo",
            repo_override=None,
        )

    assert exit_code == 0
    assert tail_mock.call_count >= 2
    output = capsys.readouterr().out
    assert "appended-line" in output


def test_logs_command_kind_review_daemon(tmp_path: Path, capsys) -> None:
    """--kind review_daemon is forwarded to the supervisor filter."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = _make_log_file(log_dir, "review-daemon-xyz.log", 4)
    record = _make_process_record(
        process_id="xyz",
        repo_id="mixed-repo",
        kind=RunnerProcessKind.REVIEW_DAEMON.value,
        log_path=str(log_file),
    )

    context = MagicMock(repo_id="mixed-repo")
    supervisor = MagicMock()
    supervisor.list_processes.return_value = [
        _make_process_record(
            process_id="daemon-only",
            repo_id="mixed-repo",
            kind=RunnerProcessKind.DAEMON.value,
            log_path="",
        ),
        record,
    ]

    parsed = _FakeArgs(
        kind=RunnerProcessKind.REVIEW_DAEMON.value,
        lines=2,
        follow=False,
        repo_id="mixed-repo",
    )

    with patch(
        "backend.api.cli_registry.resolve_repository_targets",
        return_value=[context],
    ), patch(
        "backend.api.cli_registry.create_process_supervisor",
        return_value=supervisor,
    ), patch(
        "backend.api.cli_registry.tail_runner_log",
        return_value=ProcessLogChunk(
            content="line 0002: daemon step 2\nline 0003: daemon step 3\n",
            next_offset=log_file.stat().st_size,
            eof=False,
        ),
    ):
        exit_code = _run_logs_command(
            parsed=parsed,
            process_runner=MagicMock(),
            runner_settings=MagicMock(),
            repo_id="mixed-repo",
            repo_override=None,
        )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "line 0003: daemon step 3" in output


def test_logs_command_omitted_lines_uses_default(tmp_path: Path, capsys) -> None:
    """Default --lines 200 keeps all current content when below threshold."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = _make_log_file(log_dir, "daemon-default.log", 3)
    record = _make_process_record(
        process_id="default-1",
        repo_id="default-repo",
        kind=RunnerProcessKind.DAEMON.value,
        log_path=str(log_file),
    )

    context = MagicMock(repo_id="default-repo")
    supervisor = MagicMock()
    supervisor.list_processes.return_value = [record]

    parsed = _FakeArgs(
        kind=RunnerProcessKind.DAEMON.value,
        lines=None,  # simulate argparse default-missing
        follow=False,
        repo_id="default-repo",
    )

    with patch(
        "backend.api.cli_registry.resolve_repository_targets",
        return_value=[context],
    ), patch(
        "backend.api.cli_registry.create_process_supervisor",
        return_value=supervisor,
    ), patch(
        "backend.api.cli_registry.tail_runner_log",
        return_value=ProcessLogChunk(
            content=log_file.read_text(encoding="utf-8"),
            next_offset=log_file.stat().st_size,
            eof=False,
        ),
    ):
        exit_code = _run_logs_command(
            parsed=parsed,
            process_runner=MagicMock(),
            runner_settings=MagicMock(),
            repo_id="default-repo",
            repo_override=None,
        )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "line 0002: daemon step 2" in output


# ---------------------------------------------------------------------------
# iar daemon status log_path column
# ---------------------------------------------------------------------------


def test_daemon_status_table_includes_log_path_column(tmp_path: Path) -> None:
    """`iar daemon status` must add a `log_path` column for managed records."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "daemon-abc.log"
    log_file.write_text("hello", encoding="utf-8")

    managed = _make_process_record(
        process_id="managed-1",
        repo_id="daemon-repo",
        kind=RunnerProcessKind.DAEMON.value,
        log_path=str(log_file),
    )
    unmanaged = _make_process_record(
        process_id="unmanaged-1",
        repo_id="daemon-repo",
        kind=RunnerProcessKind.DAEMON.value,
        log_path="",
    )

    editor = MagicMock()
    editor.list_repositories.return_value = []
    context = MagicMock(repo_id="daemon-repo")
    supervisor = MagicMock()
    supervisor.list_processes.return_value = [managed]
    supervisor.list_unmanaged_processes.return_value = [unmanaged]

    parsed = _FakeArgs(repo_id="daemon-repo", all_repositories=False)

    captured_table = None

    def _capture_print(value) -> None:
        nonlocal captured_table
        captured_table = value

    with patch(
        "backend.api.cli_registry.resolve_repository_targets",
        return_value=[context],
    ), patch(
        "backend.api.cli_registry.create_process_supervisor",
        return_value=supervisor,
    ), patch(
        "backend.api.cli_registry.create_registry_editor",
        return_value=editor,
    ), patch("backend.api.cli_registry.console.print", side_effect=_capture_print):
        exit_code = _run_daemon_status_command(
            parsed=parsed,
            process_runner=MagicMock(),
            runner_settings=MagicMock(),
            repo_id="daemon-repo",
            repo_override=None,
        )

    assert exit_code == 0
    assert captured_table is not None
    column_names = [column.header for column in captured_table.columns]
    assert "log_path" in column_names

    rendered = _render_table(captured_table)
    assert "log_path" in rendered
    # Unmanaged record has empty log_path and must render as "-" placeholder.
    assert " - " in rendered or rendered.endswith("-")
