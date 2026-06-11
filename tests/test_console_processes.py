"""Tests for console process control (argv whitelist, dedup, supervisor)."""

from __future__ import annotations

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
