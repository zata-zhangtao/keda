"""Link frontend ``node_modules`` from the main checkout into worktrees.

``git worktree add`` only materializes tracked files, so gitignored
``node_modules`` directories never reach a fresh worktree. Agents that run
``vite`` or any lockfile-driven build inside a worktree then fail with
``vite: command not found`` because the dependencies were never installed
there.

The legacy ``scripts/worktree/create.sh`` solved this for the manual
``just worktree`` flow via its ``symlink-from-main`` strategy, but the
agent-runner worktree path (``iar worktree create`` ->
:class:`~backend.infrastructure.git.worktree.WorktreeManager`) had no
equivalent: it only ran ``git worktree add`` and copied ``.env`` files (see
:mod:`backend.core.use_cases.worktree_env`). This module restores parity by
symlinking each frontend project's ``node_modules`` in the worktree to the
corresponding directory in the main checkout, which already holds installed
dependencies.

Design decisions:

- Symlink, never copy or install: the main checkout already has installed
  dependencies, so an absolute symlink exposes them in the worktree
  instantly with zero install cost. This mirrors the ``symlink-from-main``
  strategy of the legacy shell script.
- Discovery walks the **worktree**, not the main checkout, so only frontend
  projects that actually exist on the branch are linked; a directory is
  never fabricated for a project the branch does not contain.
- Only projects whose ``node_modules`` is **missing** in the worktree are
  linked. An existing directory or symlink (a real install, or a previous
  run) is never overwritten — re-running on an existing worktree only heals
  gaps.
- When the main checkout lacks a project's ``node_modules`` there is nothing
  to link; this is logged at warning level rather than silently skipped, so
  a later ``vite: command not found`` is traceable to an un-installed main
  checkout instead of looking like a keda bug.
- The created symlinks are kept out of git via ``info/exclude``
  (:func:`exclude_frontend_node_modules_from_git`). A repository that
  ignores ``node_modules/`` with a *trailing slash* only matches
  directories, so the symlink would otherwise show as untracked and an
  agent's ``git add -A`` could commit a machine-local absolute symlink.
- Linking is best effort per project. A single failure (permission error,
  race) is logged and skipped; it must never abort an agent run.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner

_logger = logging.getLogger(__name__)

PACKAGE_MANIFEST_FILE_NAME = "package.json"
NODE_MODULES_DIR_NAME = "node_modules"

# Directories never descended into while scanning for frontend projects: VCS
# internals, embedded worktree containers (so one worktree's tree can never
# leak into another), language/build caches, and existing dependency or build
# output directories.
FRONTEND_SCAN_PRUNED_DIR_NAMES = frozenset(
    {
        ".git",
        ".iar-worktrees",
        ".venv",
        "venv",
        ".uv-cache",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "site",
    }
)

__all__ = [
    "FRONTEND_SCAN_PRUNED_DIR_NAMES",
    "NODE_MODULES_DIR_NAME",
    "PACKAGE_MANIFEST_FILE_NAME",
    "exclude_frontend_node_modules_from_git",
    "link_frontend_node_modules",
]


def link_frontend_node_modules(
    repo_root_path: Path,
    worktree_path: Path,
) -> list[Path]:
    """Symlink frontend ``node_modules`` from the main checkout into a worktree.

    Walks ``worktree_path`` (pruning :data:`FRONTEND_SCAN_PRUNED_DIR_NAMES`)
    for directories holding a ``package.json``. For each such frontend project
    whose ``node_modules`` does not yet exist in the worktree, creates an
    absolute symlink pointing at the same project's ``node_modules`` under
    ``repo_root_path``. Existing ``node_modules`` in the worktree are never
    overwritten.

    Args:
        repo_root_path: Absolute path to the main repository checkout whose
            frontend projects already have installed dependencies.
        worktree_path: Absolute path to the target worktree.

    Returns:
        Worktree-relative paths of the frontend project directories whose
        ``node_modules`` was linked, in walk order. Empty when nothing was
        linkable.
    """
    repo_root_path = repo_root_path.resolve()
    worktree_path = worktree_path.resolve()
    if worktree_path == repo_root_path:
        return []

    linked_relative_paths: list[Path] = []
    for current_dir_text, child_dir_names, child_file_names in os.walk(worktree_path):
        child_dir_names[:] = [
            child_dir_name
            for child_dir_name in child_dir_names
            if child_dir_name not in FRONTEND_SCAN_PRUNED_DIR_NAMES
        ]
        if PACKAGE_MANIFEST_FILE_NAME not in child_file_names:
            continue
        current_dir_path = Path(current_dir_text)
        relative_project_path = current_dir_path.relative_to(worktree_path)
        target_node_modules_path = current_dir_path / NODE_MODULES_DIR_NAME
        # is_symlink() guards broken links: exists() follows the link target,
        # so a dangling symlink would otherwise look absent and get clobbered.
        if target_node_modules_path.exists() or target_node_modules_path.is_symlink():
            continue
        source_node_modules_path = (
            repo_root_path / relative_project_path / NODE_MODULES_DIR_NAME
        )
        if not source_node_modules_path.is_dir():
            _logger.warning(
                "Main checkout has no node_modules for frontend project %s; "
                "cannot link it into worktree %s. Install dependencies under "
                "%s, or the worktree build will fail with a missing binary.",
                relative_project_path,
                worktree_path,
                source_node_modules_path.parent,
            )
            continue
        try:
            os.symlink(
                source_node_modules_path,
                target_node_modules_path,
                target_is_directory=True,
            )
        except OSError as symlink_error:
            _logger.warning(
                "Skipped linking node_modules for %s into worktree %s: %s",
                relative_project_path,
                worktree_path,
                symlink_error,
            )
            continue
        linked_relative_paths.append(relative_project_path)
    return linked_relative_paths


def exclude_frontend_node_modules_from_git(
    worktree_path: Path,
    linked_relative_paths: list[Path],
    process_runner: IProcessRunner,
) -> None:
    """Keep symlinked ``node_modules`` out of the worktree's git status.

    Adds each linked project's ``node_modules`` to the worktree's
    ``info/exclude`` as an **anchored, no-trailing-slash** pattern (e.g.
    ``/frontend/node_modules``). Unlike a ``node_modules/`` rule, this form
    matches a symlink as well as a directory, so the symlinks created by
    :func:`link_frontend_node_modules` never show as untracked and can never
    be committed by an agent's ``git add -A``.

    The ``info/exclude`` file is local (it resolves to the shared common
    checkout and is never versioned), so this produces no code diff. The
    write is idempotent — already-present lines are not duplicated.

    Args:
        worktree_path: Absolute path to the worktree to update.
        linked_relative_paths: Worktree-relative frontend project paths
            returned by :func:`link_frontend_node_modules`.
        process_runner: Runner used to resolve the ``info/exclude`` location
            via ``git rev-parse``.
    """
    if not linked_relative_paths:
        return
    exclude_path_result = process_runner.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=worktree_path,
        check=False,
    )
    exclude_path_text = exclude_path_result.stdout.strip()
    if exclude_path_result.return_code != 0 or not exclude_path_text:
        _logger.warning(
            "Could not resolve git info/exclude for %s; symlinked "
            "node_modules may show as untracked.",
            worktree_path,
        )
        return
    exclude_path = Path(exclude_path_text)
    if not exclude_path.is_absolute():
        exclude_path = worktree_path / exclude_path
    if exclude_path.is_dir():
        _logger.warning(
            "Resolved info/exclude path is a directory (%s); skipping "
            "node_modules exclusion.",
            exclude_path,
        )
        return
    existing_text = (
        exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    )
    known_lines = set(existing_text.splitlines())
    appended_lines: list[str] = []
    for relative_project_path in linked_relative_paths:
        posix_project_path = relative_project_path.as_posix().strip("/")
        if posix_project_path in {"", "."}:
            exclude_line = f"/{NODE_MODULES_DIR_NAME}"
        else:
            exclude_line = f"/{posix_project_path}/{NODE_MODULES_DIR_NAME}"
        if exclude_line not in known_lines:
            appended_lines.append(exclude_line)
            known_lines.add(exclude_line)
    if not appended_lines:
        return
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    prefix_text = existing_text
    if prefix_text and not prefix_text.endswith("\n"):
        prefix_text += "\n"
    exclude_path.write_text(
        prefix_text + "\n".join(appended_lines) + "\n", encoding="utf-8"
    )
