"""Single-instance locks for the queue-runner daemon.

Every ``iar daemon`` process polls the issue queue and spawns its own agent
subprocesses, so two daemons serving the same repository multiply token spend.
These helpers provide a filesystem single-instance guard keyed by ``repo_id``:
a second daemon for an already-served repository refuses to start, while
daemons for different repositories run side by side. The lock mirrors the
stale-PID stealing idiom of
:mod:`backend.core.use_cases.agent_runner_blocked_claim` so a daemon killed
without cleanup (e.g. ``kill -9``) never wedges future starts.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path

_logger = logging.getLogger(__name__)

# Subdirectory under the iar home (the process registry's parent directory)
# that holds one lock file per repository-scoped daemon instance.
DAEMON_LOCK_DIR_NAME = "daemon-locks"


class DaemonAlreadyRunningError(RuntimeError):
    """Raised when a live daemon already owns a repository's single-instance lock."""

    def __init__(self, repo_id: str, owner_pid: int) -> None:
        self.repo_id = repo_id
        self.owner_pid = owner_pid
        super().__init__(
            f"A daemon for repository '{repo_id}' is already running "
            f"(PID {owner_pid}); refusing to start a second instance."
        )


def daemon_lock_dir(registry_path: str | Path) -> Path:
    """Return the daemon lock directory derived from the process registry path.

    Args:
        registry_path: Console ``process_registry_path`` setting (for example
            ``~/.iar/processes.json``); ``~`` is expanded.

    Returns:
        Absolute path to the directory that holds per-repository lock files,
        co-located with the existing iar runtime state.
    """
    return Path(registry_path).expanduser().parent / DAEMON_LOCK_DIR_NAME


def daemon_lock_path(lock_dir: Path, repo_id: str) -> Path:
    """Return the lock-file path for a single repository's daemon instance.

    Args:
        lock_dir: Directory returned by :func:`daemon_lock_dir`.
        repo_id: Repository identifier the daemon serves.

    Returns:
        Path to the repository's lock file under ``lock_dir``.
    """
    return lock_dir / f"{repo_id}.lock"


def _is_process_alive(pid: int) -> bool:
    """Return ``True`` when a process with ``pid`` is currently alive."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _write_lock_owner(lock_path: Path) -> None:
    """Create ``lock_path`` exclusively and record the current PID.

    Raises:
        FileExistsError: The lock file already exists.
    """
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(lock_fd, "w", encoding="utf-8") as lock_file:
        lock_file.write(f"{os.getpid()}\n")


def _read_lock_owner(lock_path: Path) -> int | None:
    """Return the PID recorded in ``lock_path`` or ``None`` when unreadable."""
    try:
        owner_text = lock_path.read_text(encoding="utf-8").strip()
        return int(owner_text.splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


def acquire_daemon_lock(lock_path: Path, repo_id: str) -> None:
    """Acquire the single-instance daemon lock for one repository.

    Args:
        lock_path: Lock-file path from :func:`daemon_lock_path`.
        repo_id: Repository identifier used for diagnostics and the raised error.

    Raises:
        DaemonAlreadyRunningError: A live daemon already owns the lock.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_lock_owner(lock_path)
        return
    except FileExistsError:
        pass

    owner_pid = _read_lock_owner(lock_path)
    if owner_pid is not None and owner_pid != os.getpid() and _is_process_alive(owner_pid):
        _logger.info(
            "Daemon for repository '%s' already owned by alive PID %d.",
            repo_id,
            owner_pid,
        )
        raise DaemonAlreadyRunningError(repo_id, owner_pid)

    # The recorded owner is dead (or the record is unreadable): steal the stale
    # lock so a previous ``kill -9`` does not block legitimate restarts.
    _logger.warning(
        "Reclaiming stale daemon lock for repository '%s' (previous PID %s).",
        repo_id,
        owner_pid,
    )
    try:
        lock_path.unlink()
    except OSError:
        pass
    try:
        _write_lock_owner(lock_path)
    except FileExistsError as exc:
        raise DaemonAlreadyRunningError(repo_id, owner_pid or -1) from exc


def release_daemon_lock(lock_path: Path) -> None:
    """Release ``lock_path`` only when it is owned by the current process.

    Args:
        lock_path: Lock-file path to release.
    """
    if _read_lock_owner(lock_path) == os.getpid():
        try:
            lock_path.unlink()
        except OSError:
            pass


def acquire_daemon_locks(lock_dir: Path, repo_ids: Iterable[str]) -> list[Path]:
    """Acquire single-instance locks for every repository the daemon serves.

    Locks are acquired in order; if any repository is already served by a live
    daemon, every lock acquired so far is released before re-raising so a
    refused start never leaves partial locks behind.

    Args:
        lock_dir: Directory holding per-repository lock files.
        repo_ids: Repository identifiers the daemon will poll.

    Returns:
        The lock-file paths acquired, in acquisition order.

    Raises:
        DaemonAlreadyRunningError: A repository already has a live daemon.
    """
    acquired_lock_paths: list[Path] = []
    for repo_id in repo_ids:
        lock_path = daemon_lock_path(lock_dir, repo_id)
        try:
            acquire_daemon_lock(lock_path, repo_id)
        except DaemonAlreadyRunningError:
            release_daemon_locks(acquired_lock_paths)
            raise
        acquired_lock_paths.append(lock_path)
    return acquired_lock_paths


def release_daemon_locks(lock_paths: Iterable[Path]) -> None:
    """Release every lock in ``lock_paths`` that the current process owns.

    Args:
        lock_paths: Lock-file paths returned by :func:`acquire_daemon_locks`.
    """
    for lock_path in lock_paths:
        release_daemon_lock(lock_path)
