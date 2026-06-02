"""Adapter exposing :class:`WorktreeManager` to the CLI layer.

Keeps the architecture rule ``api -> core -> engines -> infrastructure``
intact: the CLI layer cannot import from ``infrastructure`` directly,
so this module re-exports the manager while adding the small amount of
orchestration the CLI needs.
"""

from __future__ import annotations

from pathlib import Path

from backend.infrastructure.git.worktree import (
    WORKTREE_DIR_NAME,
    WorktreeManager,
)
from backend.infrastructure.process_runner import SubprocessRunner


def build_worktree_manager(
    repo_root_path: Path,
    process_runner: SubprocessRunner | None = None,
) -> WorktreeManager:
    """Construct a :class:`WorktreeManager` for the CLI dispatch path.

    Args:
        repo_root_path: Absolute path to the target Git repository root.
        process_runner: Optional subprocess runner; defaults to a real
            :class:`SubprocessRunner` so production CLI invocations work
            without dependency wiring.

    Returns:
        A :class:`WorktreeManager` ready to be used by ``iar worktree``.
    """
    return WorktreeManager(repo_root_path, process_runner)


__all__ = [
    "WORKTREE_DIR_NAME",
    "WorktreeManager",
    "build_worktree_manager",
]
