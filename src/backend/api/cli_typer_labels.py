"""Typer commands under ``iar labels``.

Holds :func:`labels_sync_command`, the only labels subcommand.
"""

from __future__ import annotations

import typer

from backend.api.cli_typer_app import (
    AllRepositoriesOption,
    ConfigOption,
    RepoIdOption,
    RepoOption,
    _run_typer_repository_command,
    labels_app,
)


@labels_app.command("sync")
def labels_sync_command(
    ctx: typer.Context,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
    all_repositories: AllRepositoriesOption = False,
) -> int:
    """Sync standard labels to the target repository."""
    return _run_typer_repository_command(
        ctx,
        "labels",
        repo=repo,
        repo_id=repo_id,
        config=config,
        labels_command="sync",
        all_repositories=all_repositories,
    )


__all__ = ["labels_sync_command"]
