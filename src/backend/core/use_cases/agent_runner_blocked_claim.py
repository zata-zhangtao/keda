"""Filesystem claim locks for blocked issue recovery."""

from __future__ import annotations

import logging
import os
from pathlib import Path

_logger = logging.getLogger(__name__)


class BlockedWorktreeClaimedError(RuntimeError):
    """Raised when another runner is already processing the blocked worktree."""


def _acquire_blocked_claim_lock(lock_path: Path, issue_number: int) -> None:
    """Acquire an atomic filesystem lock for blocked issue processing.

    Args:
        lock_path: Path to the lock file under the target worktree.
        issue_number: GitHub Issue number used for diagnostics.

    Raises:
        BlockedWorktreeClaimedError: Another live process owns the claim.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            lock_file.write(f"{os.getpid()}\n")
        return
    except FileExistsError:
        pass

    try:
        raw_text = lock_path.read_text(encoding="utf-8").strip()
        owner_pid = int(raw_text.splitlines()[0])
    except (OSError, ValueError, IndexError):
        owner_pid = None

    if owner_pid is not None:
        try:
            os.kill(owner_pid, 0)
            _logger.info(
                "Blocked Issue #%d worktree already claimed by alive process %d.",
                issue_number,
                owner_pid,
            )
            raise BlockedWorktreeClaimedError(
                f"Blocked Issue #{issue_number} worktree is already being processed."
            )
        except OSError:
            _logger.warning(
                "Stealing stale blocked claim lock for Issue #%d from dead PID %d.",
                issue_number,
                owner_pid,
            )

    try:
        lock_path.unlink()
    except OSError:
        pass
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            lock_file.write(f"{os.getpid()}\n")
    except FileExistsError:
        raise BlockedWorktreeClaimedError(
            f"Blocked Issue #{issue_number} worktree claim race lost after lock steal attempt."
        )


def _release_blocked_claim_lock(lock_path: Path) -> None:
    """Release the blocked claim lock if it belongs to the current process.

    Args:
        lock_path: Path to the lock file under the target worktree.
    """
    try:
        raw_text = lock_path.read_text(encoding="utf-8").strip()
        owner_pid = int(raw_text.splitlines()[0])
        if owner_pid == os.getpid():
            lock_path.unlink()
    except (OSError, ValueError, IndexError):
        pass
