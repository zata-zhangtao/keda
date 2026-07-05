"""Typer commands under ``iar workflow``.

Holds :func:`workflow_install_command`, the bundled template installer
(GitHub Actions + preview deploy scripts).
"""

from __future__ import annotations

from typing import Annotated

import typer

from backend.api.cli_typer_app import (
    ConfigOption,
    RepoIdOption,
    RepoOption,
    _run_typer_command,
    _typer_selector_options,
    workflow_app,
)


@workflow_app.command("install")
def workflow_install_command(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Workflow template name (e.g. 'preview').")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite existing template files and the [preview] section.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the install plan without writing anything.",
        ),
    ] = False,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
) -> int:
    """Install a bundled workflow template into the current repository."""
    selector_options = _typer_selector_options(ctx, repo=repo, repo_id=repo_id, config=config)
    return _run_typer_command(
        "workflow install",
        **selector_options,
        name=name,
        force=force,
        dry_run=dry_run,
    )


__all__ = ["workflow_install_command"]
