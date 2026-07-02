"""Tests for the daemon single-instance lock helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.core.use_cases import daemon_single_instance
from backend.core.use_cases.daemon_single_instance import (
    DaemonAlreadyRunningError,
    acquire_daemon_lock,
    acquire_daemon_locks,
    daemon_lock_dir,
    daemon_lock_path,
    release_daemon_lock,
    release_daemon_locks,
)

_FOREIGN_PID = 424242


def test_daemon_lock_dir_derives_from_registry_path() -> None:
    """The lock directory sits next to the process registry under the iar home."""
    lock_dir = daemon_lock_dir("~/.iar/processes.json")

    assert lock_dir == Path("~/.iar").expanduser() / "daemon-locks"


def test_acquire_writes_current_pid(tmp_path: Path) -> None:
    """Acquiring a free lock records the calling process PID."""
    lock_path = daemon_lock_path(tmp_path, "keda-main")

    acquire_daemon_lock(lock_path, "keda-main")

    assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_acquire_refused_when_live_owner_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock held by a different, alive process blocks a second acquire."""
    lock_path = daemon_lock_path(tmp_path, "keda-main")
    lock_path.write_text(f"{_FOREIGN_PID}\n", encoding="utf-8")
    monkeypatch.setattr(daemon_single_instance, "_is_process_alive", lambda pid: True)

    with pytest.raises(DaemonAlreadyRunningError) as excinfo:
        acquire_daemon_lock(lock_path, "keda-main")

    assert excinfo.value.repo_id == "keda-main"
    assert excinfo.value.owner_pid == _FOREIGN_PID


def test_acquire_steals_stale_lock_from_dead_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock left by a dead process is reclaimed without raising."""
    lock_path = daemon_lock_path(tmp_path, "keda-main")
    lock_path.write_text(f"{_FOREIGN_PID}\n", encoding="utf-8")
    monkeypatch.setattr(daemon_single_instance, "_is_process_alive", lambda pid: False)

    acquire_daemon_lock(lock_path, "keda-main")

    assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_release_removes_only_owned_lock(tmp_path: Path) -> None:
    """Releasing a lock owned by the current process deletes the file."""
    lock_path = daemon_lock_path(tmp_path, "keda-main")
    acquire_daemon_lock(lock_path, "keda-main")

    release_daemon_lock(lock_path)

    assert not lock_path.exists()


def test_release_keeps_lock_owned_by_other(tmp_path: Path) -> None:
    """Releasing never deletes a lock owned by a different process."""
    lock_path = daemon_lock_path(tmp_path, "keda-main")
    lock_path.write_text(f"{_FOREIGN_PID}\n", encoding="utf-8")

    release_daemon_lock(lock_path)

    assert lock_path.exists()


def test_acquire_many_serves_distinct_repositories(tmp_path: Path) -> None:
    """Locks for different repositories coexist and are all acquired."""
    acquired = acquire_daemon_locks(tmp_path, ["keda-main", "zata-zhangtao-freshai"])

    assert [path.name for path in acquired] == [
        "keda-main.lock",
        "zata-zhangtao-freshai.lock",
    ]
    assert all(path.exists() for path in acquired)


def test_acquire_many_rolls_back_on_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflict on any repository releases locks already taken this call."""
    contended_lock = daemon_lock_path(tmp_path, "freshai")
    contended_lock.write_text(f"{_FOREIGN_PID}\n", encoding="utf-8")
    monkeypatch.setattr(daemon_single_instance, "_is_process_alive", lambda pid: True)

    with pytest.raises(DaemonAlreadyRunningError):
        acquire_daemon_locks(tmp_path, ["keda-main", "freshai", "other"])

    # The lock taken before the conflict is rolled back; the contended one is
    # left untouched and the never-attempted one is absent.
    assert not daemon_lock_path(tmp_path, "keda-main").exists()
    assert contended_lock.read_text(encoding="utf-8").strip() == str(_FOREIGN_PID)
    assert not daemon_lock_path(tmp_path, "other").exists()


def test_release_many_clears_all_owned_locks(tmp_path: Path) -> None:
    """Releasing the acquired set removes every owned lock file."""
    acquired = acquire_daemon_locks(tmp_path, ["keda-main", "freshai"])

    release_daemon_locks(acquired)

    assert not any(path.exists() for path in acquired)
