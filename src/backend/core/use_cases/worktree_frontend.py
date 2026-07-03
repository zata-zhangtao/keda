"""Ensure frontend ``node_modules`` are usable inside worktrees.

``git worktree add`` only materializes tracked files, so gitignored
``node_modules`` directories never reach a fresh worktree. This module makes
frontend builds work by installing dependencies directly in the worktree
whenever a lockfile is present. If no lockfile is found (or the install fails),
it falls back to symlinking the project's ``node_modules`` from the main
checkout, mirroring the legacy ``symlink-from-main`` strategy.

Design decisions:

- Lockfile-driven install is the default. Real ``node_modules`` directories
  are the only form every frontend toolchain (including Next.js/Turbopack)
  is guaranteed to support.
- Package manager is auto-detected from lockfiles:
  ``pnpm-lock.yaml`` → pnpm, ``package-lock.json`` → npm,
  ``yarn.lock`` → yarn, ``bun.lock``/``bun.lockb`` → bun.
  A project with ``package.json`` but no lockfile falls back to ``npm install``.
- When install is unavailable or fails, the symlink fallback reuses the main
  checkout's already-installed dependencies with zero extra disk cost.
- Discovery walks the **worktree**, not the main checkout, so only frontend
  projects that actually exist on the branch are handled.
- Existing ``node_modules`` in the worktree (directory or symlink) are never
  overwritten — re-running on an existing worktree only heals gaps.
- Symlinks created by the fallback are kept out of git via ``info/exclude``
  (:func:`exclude_frontend_node_modules_from_git`). A repository that ignores
  ``node_modules/`` with a trailing slash only matches directories, so the
  symlink would otherwise show as untracked and an agent's ``git add -A``
  could commit a machine-local absolute symlink.
- Both install and link are best effort per project. A single failure is
  logged and skipped; it must never abort an agent run.
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

# Lockfile name → package manager name.
_LOCKFILE_PACKAGE_MANAGERS = {
    "pnpm-lock.yaml": "pnpm",
    "package-lock.json": "npm",
    "yarn.lock": "yarn",
    "bun.lock": "bun",
    "bun.lockb": "bun",
}

__all__ = [
    "FRONTEND_SCAN_PRUNED_DIR_NAMES",
    "NODE_MODULES_DIR_NAME",
    "PACKAGE_MANIFEST_FILE_NAME",
    "ensure_frontend_node_modules",
    "exclude_frontend_node_modules_from_git",
    "link_frontend_node_modules",
]


def _detect_package_manager(project_dir_path: Path) -> str | None:
    """Return the package manager name detected from lockfiles.

    Args:
        project_dir_path: Directory containing a ``package.json``.

    Returns:
        Package manager name, or ``None`` when no lockfile is present.
    """
    for lockfile_name, package_manager in _LOCKFILE_PACKAGE_MANAGERS.items():
        if (project_dir_path / lockfile_name).exists():
            return package_manager
    return None


def _build_install_command(package_manager: str) -> list[str]:
    """Build an install command for the detected package manager.

    The commands mirror the ``install_frontend_dependencies_in_current_directory``
    helper in ``scripts/shared/worktree/create.sh`` so the Python and shell
    worktree paths behave identically.

    Args:
        package_manager: Name of the package manager (``pnpm``, ``npm``,
            ``yarn``, or ``bun``).

    Returns:
        Argument vector for the install command.
    """
    if package_manager == "pnpm":
        return ["pnpm", "install", "--ignore-scripts"]
    if package_manager == "npm":
        return ["npm", "ci", "--ignore-scripts"]
    if package_manager == "yarn":
        return ["yarn", "install", "--ignore-scripts"]
    if package_manager == "bun":
        return ["bun", "install", "--ignore-scripts"]
    return ["npm", "install", "--ignore-scripts"]


def _try_install_frontend_dependencies(
    project_dir_path: Path,
    relative_project_path: Path,
    process_runner: IProcessRunner,
) -> bool:
    """Attempt a lockfile-driven install in the worktree project directory.

    Args:
        project_dir_path: Absolute path to the frontend project in the worktree.
        relative_project_path: Project path relative to the worktree root (for
            logging).
        process_runner: Runner used to execute the package manager install.

    Returns:
        ``True`` when the install command exits with code 0; ``False`` when no
        lockfile is present, the package manager is unavailable, or the install
        fails.
    """
    package_manager = _detect_package_manager(project_dir_path)
    if package_manager is None:
        _logger.info(
            "No lockfile found for frontend project %s; will attempt symlink fallback.",
            relative_project_path,
        )
        return False

    install_command = _build_install_command(package_manager)
    _logger.info(
        "Installing frontend dependencies for %s with %s: %s",
        relative_project_path,
        package_manager,
        " ".join(install_command),
    )
    install_result = process_runner.run(
        install_command,
        cwd=project_dir_path,
        check=False,
    )
    if install_result.return_code == 0:
        return True

    _logger.warning(
        "%s install failed for %s (exit %d)%s; falling back to symlink.",
        package_manager,
        relative_project_path,
        install_result.return_code,
        (f": {install_result.stderr.strip()}" if install_result.stderr.strip() else ""),
    )
    return False


def _try_link_frontend_node_modules(
    repo_root_path: Path,
    worktree_path: Path,
    project_dir_path: Path,
    relative_project_path: Path,
) -> bool:
    """Create a symlink from the main checkout's node_modules as fallback.

    Args:
        repo_root_path: Absolute path to the main repository checkout.
        worktree_path: Absolute path to the target worktree.
        project_dir_path: Absolute path to the frontend project in the worktree.
        relative_project_path: Project path relative to the worktree root.

    Returns:
        ``True`` when a symlink was created; ``False`` otherwise.
    """
    source_node_modules_path = repo_root_path / relative_project_path / NODE_MODULES_DIR_NAME
    target_node_modules_path = project_dir_path / NODE_MODULES_DIR_NAME
    if not source_node_modules_path.is_dir():
        _logger.warning(
            "Main checkout has no node_modules for frontend project %s; "
            "cannot link it into worktree %s. Install dependencies under "
            "%s, or the worktree build will fail with a missing binary.",
            relative_project_path,
            worktree_path,
            source_node_modules_path.parent,
        )
        return False
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
        return False
    return True


def ensure_frontend_node_modules(
    repo_root_path: Path,
    worktree_path: Path,
    process_runner: IProcessRunner,
) -> tuple[list[Path], list[Path]]:
    """Ensure frontend ``node_modules`` exist in a worktree.

    Walks ``worktree_path`` (pruning :data:`FRONTEND_SCAN_PRUNED_DIR_NAMES`)
    for directories holding a ``package.json``. For each such frontend project
    whose ``node_modules`` does not yet exist in the worktree:

    1. If a lockfile is present, run the matching package manager's install
       command inside the worktree project directory.
    2. If the install is skipped (no lockfile) or fails, and the main checkout
       has ``node_modules`` for the same project, create an absolute symlink
       pointing at the main checkout as a fallback.

    Existing ``node_modules`` in the worktree are never overwritten.

    Args:
        repo_root_path: Absolute path to the main repository checkout whose
            frontend projects may already have installed dependencies.
        worktree_path: Absolute path to the target worktree.
        process_runner: Runner used to execute package manager install
            commands.

    Returns:
        Tuple of ``(installed_relative_paths, linked_relative_paths)`` — two
        lists of worktree-relative frontend project directories handled by
        each strategy, in walk order. Empty when nothing was actionable.
    """
    repo_root_path = repo_root_path.resolve()
    worktree_path = worktree_path.resolve()
    if worktree_path == repo_root_path:
        return [], []

    installed_relative_paths: list[Path] = []
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

        if _try_install_frontend_dependencies(
            current_dir_path,
            relative_project_path,
            process_runner,
        ):
            installed_relative_paths.append(relative_project_path)
            continue

        if _try_link_frontend_node_modules(
            repo_root_path,
            worktree_path,
            current_dir_path,
            relative_project_path,
        ):
            linked_relative_paths.append(relative_project_path)

    return installed_relative_paths, linked_relative_paths


def link_frontend_node_modules(
    repo_root_path: Path,
    worktree_path: Path,
) -> list[Path]:
    """Symlink frontend ``node_modules`` from the main checkout into a worktree.

    This is the legacy ``symlink-from-main`` fallback. New code should call
    :func:`ensure_frontend_node_modules`, which tries a lockfile-driven install
    first and only falls back to symlinking when install is unavailable or
    fails.

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
        if _try_link_frontend_node_modules(
            repo_root_path,
            worktree_path,
            current_dir_path,
            relative_project_path,
        ):
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

    Real ``node_modules`` directories created by install are already covered
    by the standard ``node_modules/`` gitignore rule, so they do not need to
    be added here.

    The ``info/exclude`` file is local (it resolves to the shared common
    checkout and is never versioned), so this produces no code diff. The
    write is idempotent — already-present lines are not duplicated.

    Args:
        worktree_path: Absolute path to the worktree to update.
        linked_relative_paths: Worktree-relative frontend project paths whose
            ``node_modules`` are symlinks and must be excluded from git.
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
            "Resolved info/exclude path is a directory (%s); skipping " "node_modules exclusion.",
            exclude_path,
        )
        return
    existing_text = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
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
    exclude_path.write_text(prefix_text + "\n".join(appended_lines) + "\n", encoding="utf-8")
