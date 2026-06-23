"""Tests for console process control (argv whitelist, dedup, supervisor)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from backend.core.shared.interfaces.runner_console import (
    RunnerProcessKind,
)
from backend.core.shared.models.agent_runner import AppConfig, RepositoryRunContext
from backend.core.use_cases.console_processes import (
    ConsoleProcessError,
    build_runner_argv,
    start_runner_process,
    stop_runner_process,
    tail_runner_log,
)
from backend.infrastructure.console.process_supervisor import (
    PidfileProcessSupervisor,
    _find_iar_command_index,
    _parse_repo_id_from_argv,
    _parse_unmanaged_kind,
    _resolve_repo_id_from_cwd,
)


def _make_context(repo_id: str = "keda-main") -> RepositoryRunContext:
    return RepositoryRunContext(
        repo_id=repo_id,
        display_name=repo_id,
        repo_path=Path("/tmp/repo"),
        config=AppConfig(),
    )


# ── argv 白名单 ──────────────────────────────────────────────────────────────


def test_build_runner_argv_daemon() -> None:
    """daemon kind should map to `iar daemon --repo-id <id>`."""
    argv = build_runner_argv(
        runner_command=["uv", "run", "iar"],
        kind=RunnerProcessKind.DAEMON,
        repo_id="keda-main",
    )
    assert argv == ("uv", "run", "iar", "daemon", "--repo-id", "keda-main")


def test_build_runner_argv_blocked_continue_requires_issue() -> None:
    """blocked_continue without an issue number must be rejected."""
    with pytest.raises(ConsoleProcessError):
        build_runner_argv(
            runner_command=["iar"],
            kind=RunnerProcessKind.BLOCKED_CONTINUE,
            repo_id="keda-main",
        )


def test_build_runner_argv_blocked_continue_with_issue() -> None:
    """blocked_continue maps to `iar blocked-continue --issue N --repo-id`."""
    argv = build_runner_argv(
        runner_command=["iar"],
        kind=RunnerProcessKind.BLOCKED_CONTINUE,
        repo_id="keda-main",
        issue_number=19,
    )
    assert argv == (
        "iar",
        "blocked-continue",
        "--issue",
        "19",
        "--repo-id",
        "keda-main",
    )


def test_build_runner_argv_rejects_empty_prefix() -> None:
    """An empty runner_command must be rejected."""
    with pytest.raises(ConsoleProcessError):
        build_runner_argv(
            runner_command=[],
            kind=RunnerProcessKind.DAEMON,
            repo_id="keda-main",
        )


# ── 真实 subprocess 生命周期 ─────────────────────────────────────────────────


def _make_supervisor(tmp_path: Path) -> PidfileProcessSupervisor:
    return PidfileProcessSupervisor(
        registry_path=tmp_path / "processes.json",
        log_dir=tmp_path / "logs",
    )


def _sleeper_argv(seconds: float = 30) -> list[str]:
    return [
        sys.executable,
        "-u",
        "-c",
        f"import time; print('runner started', flush=True); time.sleep({seconds})",
    ]


def test_spawn_list_stop_real_process(tmp_path: Path) -> None:
    """A spawned process is listed as running and SIGTERM-stopped."""
    supervisor = _make_supervisor(tmp_path)
    record = supervisor.spawn(
        repo_id="keda-main",
        kind=RunnerProcessKind.DAEMON,
        argv=_sleeper_argv(),
        cwd=tmp_path,
    )
    assert record.status == "running"

    listed = supervisor.list_processes()
    assert [entry.process_id for entry in listed] == [record.process_id]
    assert listed[0].status == "running"

    stopped = supervisor.stop(record.process_id, timeout_seconds=5)
    assert stopped.status in ("stopped", "exited")
    assert stopped.stopped_at is not None


def test_stop_escalates_to_sigkill_when_sigterm_ignored(tmp_path: Path) -> None:
    """A SIGTERM-ignoring process must be SIGKILLed after the timeout."""
    supervisor = _make_supervisor(tmp_path)
    record = supervisor.spawn(
        repo_id="keda-main",
        kind=RunnerProcessKind.DAEMON,
        argv=[
            sys.executable,
            "-u",
            "-c",
            (
                "import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ignoring SIGTERM', flush=True); "
                "time.sleep(60)"
            ),
        ],
        cwd=tmp_path,
    )
    # 等待子进程装好 SIGTERM handler，避免竞态。
    deadline = time.monotonic() + 10
    collected = ""
    offset = 0
    while time.monotonic() < deadline and "ignoring" not in collected:
        chunk = supervisor.read_log(record.process_id, offset=offset, max_bytes=1024)
        collected += chunk.content
        offset = chunk.next_offset
        time.sleep(0.05)

    stopped = supervisor.stop(record.process_id, timeout_seconds=1)
    assert stopped.status == "killed"
    assert stopped.stopped_at is not None


def test_registry_survives_supervisor_restart(tmp_path: Path) -> None:
    """A new supervisor instance must revive records from the pidfile."""
    first_supervisor = _make_supervisor(tmp_path)
    record = first_supervisor.spawn(
        repo_id="keda-main",
        kind=RunnerProcessKind.DAEMON,
        argv=_sleeper_argv(),
        cwd=tmp_path,
    )
    # 模拟后端重启：新建监管器实例读取同一 pidfile。
    second_supervisor = _make_supervisor(tmp_path)
    revived = second_supervisor.get_process(record.process_id)
    assert revived is not None
    assert revived.status == "running"
    second_supervisor.stop(record.process_id, timeout_seconds=5)


def test_exited_process_detected(tmp_path: Path) -> None:
    """A short-lived process should be reported as exited after it ends."""
    supervisor = _make_supervisor(tmp_path)
    record = supervisor.spawn(
        repo_id="keda-main",
        kind=RunnerProcessKind.RUN_ONCE,
        argv=[sys.executable, "-c", "print('done')"],
        cwd=tmp_path,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        refreshed = supervisor.get_process(record.process_id)
        assert refreshed is not None
        if refreshed.status == "exited":
            break
        time.sleep(0.1)
    else:
        pytest.fail("process never reported as exited")


def test_read_log_offset_resume(tmp_path: Path) -> None:
    """Log reads must resume from the returned offset."""
    supervisor = _make_supervisor(tmp_path)
    record = supervisor.spawn(
        repo_id="keda-main",
        kind=RunnerProcessKind.RUN_ONCE,
        argv=[sys.executable, "-u", "-c", "print('alpha'); print('beta')"],
        cwd=tmp_path,
    )
    deadline = time.monotonic() + 10
    collected = ""
    offset = 0
    while time.monotonic() < deadline:
        chunk = supervisor.read_log(record.process_id, offset=offset, max_bytes=4)
        collected += chunk.content
        offset = chunk.next_offset
        if "beta" in collected:
            break
        time.sleep(0.05)
    assert "alpha" in collected
    assert "beta" in collected


# ── use case 层校验 ──────────────────────────────────────────────────────────


def test_start_rejects_unknown_repo(tmp_path: Path) -> None:
    """Starting a process for an unknown repo_id must be rejected."""
    supervisor = _make_supervisor(tmp_path)
    with pytest.raises(ConsoleProcessError):
        start_runner_process(
            repo_id="ghost",
            kind=RunnerProcessKind.DAEMON,
            contexts=[_make_context("keda-main")],
            supervisor=supervisor,
            runner_command=[sys.executable, "-c", "pass"],
            spawn_cwd=tmp_path,
        )


def test_start_dedupes_persistent_kind(tmp_path: Path) -> None:
    """A second daemon for the same repo must be rejected while running."""
    supervisor = _make_supervisor(tmp_path)
    contexts = [_make_context("keda-main")]
    # runner_command 前缀指向 sleeper；额外的子命令参数会被忽略。
    runner_command = [
        sys.executable,
        "-u",
        "-c",
        "import time, sys; print('fake runner', sys.argv[1:], flush=True); time.sleep(30)",
    ]
    record = start_runner_process(
        repo_id="keda-main",
        kind=RunnerProcessKind.DAEMON,
        contexts=contexts,
        supervisor=supervisor,
        runner_command=runner_command,
        spawn_cwd=tmp_path,
    )
    try:
        with pytest.raises(ConsoleProcessError, match="already exists"):
            start_runner_process(
                repo_id="keda-main",
                kind=RunnerProcessKind.DAEMON,
                contexts=contexts,
                supervisor=supervisor,
                runner_command=runner_command,
                spawn_cwd=tmp_path,
            )
    finally:
        stop_runner_process(
            process_id=record.process_id,
            supervisor=supervisor,
            stop_timeout_seconds=5,
        )


def test_tail_unknown_process_rejected(tmp_path: Path) -> None:
    """Reading logs of an unregistered process must raise ConsoleProcessError."""
    supervisor = _make_supervisor(tmp_path)
    with pytest.raises(ConsoleProcessError):
        tail_runner_log(process_id="nope", offset=0, supervisor=supervisor)


# ── 未托管进程扫描辅助函数 ─────────────────────────────────────────────────────


def test_find_iar_command_index() -> None:
    """``iar`` may appear after wrappers or with an absolute path."""
    assert _find_iar_command_index(("uv", "run", "iar", "daemon")) == 2
    assert _find_iar_command_index(("/Users/x/.local/bin/iar", "daemon")) == 0
    assert _find_iar_command_index(("python", "-m", "backend.api.cli")) is None


def test_parse_unmanaged_kind() -> None:
    """Detect daemon and review-daemon subcommands, ignoring options."""
    assert (
        _parse_unmanaged_kind(("uv", "run", "iar", "daemon", "--repo-id", "keda"))
        == "daemon"
    )
    assert (
        _parse_unmanaged_kind(
            ("iar", "review-daemon", "--repo-id", "keda", "--agent", "claude")
        )
        == "review_daemon"
    )
    assert _parse_unmanaged_kind(("iar", "run", "--repo-id", "keda")) is None
    assert _parse_unmanaged_kind(("python", "-m", "backend.api.cli")) is None


def test_parse_repo_id_from_argv() -> None:
    """Extract --repo-id value from command line."""
    assert (
        _parse_repo_id_from_argv(("iar", "daemon", "--repo-id", "keda-main"))
        == "keda-main"
    )
    assert _parse_repo_id_from_argv(("iar", "daemon")) is None


def test_resolve_repo_id_from_cwd(tmp_path: Path) -> None:
    """Match process cwd against registry entry paths."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Entry:
        repo_id: str
        path: str

    repo_dir = tmp_path / "repo-a"
    repo_dir.mkdir()
    entries = [_Entry(repo_id="repo-a", path=str(repo_dir))]
    assert _resolve_repo_id_from_cwd(str(repo_dir), entries) == "repo-a"
    assert _resolve_repo_id_from_cwd(str(tmp_path), entries) is None
    assert _resolve_repo_id_from_cwd(None, entries) is None


def test_list_unmanaged_processes_skips_managed_pids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pid already present in processes.json must not be reported as unmanaged."""
    supervisor = _make_supervisor(tmp_path)
    fake_cmdline = ["iar", "daemon", "--repo-id", "keda-main"]

    class _FakeProc:
        info = {
            "pid": 12345,
            "username": "testuser",
            "cmdline": fake_cmdline,
            "create_time": 1_700_000_000.0,
        }

        def cwd(self) -> str:
            return str(tmp_path)

    fake_current_user = "testuser"
    monkeypatch.setattr(
        "backend.infrastructure.console.process_supervisor.psutil.Process",
        lambda: type(
            "_FakeCurrent", (), {"username": lambda self: fake_current_user}
        )(),
    )
    monkeypatch.setattr(
        "backend.infrastructure.console.process_supervisor.psutil.process_iter",
        lambda attrs: [_FakeProc()],
    )

    # First, register the same pid as a managed process.
    managed_record = supervisor.spawn(
        repo_id="keda-main",
        kind=RunnerProcessKind.DAEMON,
        argv=fake_cmdline,
        cwd=tmp_path,
    )
    # Force the managed record to use the same pid as the fake scanner.
    registry = json.loads(
        tmp_path.joinpath("processes.json").read_text(encoding="utf-8")
    )
    registry[managed_record.process_id]["pid"] = 12345
    tmp_path.joinpath("processes.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    registry_entries = [
        type("_Entry", (), {"repo_id": "keda-main", "path": str(tmp_path)})()
    ]
    unmanaged = supervisor.list_unmanaged_processes(registry_entries)
    assert unmanaged == []


def test_list_unmanaged_processes_reports_unmanaged_by_repo_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmanaged iar daemon with --repo-id is reported as running unmanaged."""
    supervisor = _make_supervisor(tmp_path)

    class _FakeProc:
        info = {
            "pid": 12345,
            "username": "testuser",
            "cmdline": ["iar", "daemon", "--repo-id", "keda-main"],
            "create_time": 1_700_000_000.0,
        }

        def cwd(self) -> str:
            return str(tmp_path)

    fake_current_user = "testuser"
    monkeypatch.setattr(
        "backend.infrastructure.console.process_supervisor.psutil.Process",
        lambda: type(
            "_FakeCurrent", (), {"username": lambda self: fake_current_user}
        )(),
    )
    monkeypatch.setattr(
        "backend.infrastructure.console.process_supervisor.psutil.process_iter",
        lambda attrs: [_FakeProc()],
    )

    registry_entries = [
        type("_Entry", (), {"repo_id": "keda-main", "path": str(tmp_path)})()
    ]
    unmanaged = supervisor.list_unmanaged_processes(registry_entries)
    assert len(unmanaged) == 1
    assert unmanaged[0].process_id == "unmanaged-12345"
    assert unmanaged[0].repo_id == "keda-main"
    assert unmanaged[0].kind == "daemon"
    assert unmanaged[0].status == "running"
