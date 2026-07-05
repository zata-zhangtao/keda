"""Typer commands under ``iar worktree``.

Holds every command that operates on IAR-owned Git worktrees for the
current repository:

- :func:`worktree_create_command`
- :func:`worktree_path_command`
- :func:`worktree_remove_command`
- :func:`worktree_cleanup_command`
"""

from __future__ import annotations

from typing import Annotated

import typer

from backend.api.cli_typer_app import _run_typer_command, worktree_app


@worktree_app.command("create")
def worktree_create_command(
    branch: Annotated[str, typer.Option("--branch", help="Branch name to create.")],
    base_branch: Annotated[
        str, typer.Option("--base-branch", help="Existing branch to fork from.")
    ],
) -> int:
    """Create a worktree at .iar-worktrees/<branch>."""
    return _run_typer_command(
        "worktree",
        repo=None,
        repo_id=None,
        config=None,
        worktree_command="create",
        branch=branch,
        base_branch=base_branch,
    )


@worktree_app.command("path")
def worktree_path_command(
    branch: Annotated[str, typer.Option("--branch", help="Branch name to resolve.")],
) -> int:
    """Print the absolute worktree path for a branch."""
    return _run_typer_command(
        "worktree",
        repo=None,
        repo_id=None,
        config=None,
        worktree_command="path",
        branch=branch,
    )


@worktree_app.command("remove")
def worktree_remove_command(
    branch: Annotated[str, typer.Option("--branch", help="Branch name whose worktree to remove.")],
) -> int:
    """Remove a worktree and prune Git metadata."""
    return _run_typer_command(
        "worktree",
        repo=None,
        repo_id=None,
        config=None,
        worktree_command="remove",
        branch=branch,
    )


@worktree_app.command("cleanup")
def worktree_cleanup_command(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview cleanup without deleting.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Actually delete eligible branches.")] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Also delete dirty or unmerged eligible branches.",
        ),
    ] = False,
) -> int:
    """Delete stale local issue branches whose Issue is closed."""
    return _run_typer_command(
        "worktree",
        repo=None,
        repo_id=None,
        config=None,
        worktree_command="cleanup",
        dry_run=dry_run,
        yes=yes,
        force=force,
    )


__all__ = [
    "worktree_cleanup_command",
    "worktree_create_command",
    "worktree_path_command",
    "worktree_remove_command",
]
