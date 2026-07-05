"""Typer commands under ``iar takeover``.

Holds :func:`takeover_command`, the bulk-import command used to clone,
initialize, register, and start daemons across GitHub repositories.
"""

from __future__ import annotations

from typing import Annotated

import typer

from backend.api.cli_typer_app import _run_typer_command, app


@app.command("takeover")
def takeover_command(
    owner: Annotated[
        str | None,
        typer.Option("--owner", help="GitHub user or organization whose repositories to list."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum number of repositories to fetch from GitHub."),
    ] = 100,
    clone_root: Annotated[
        str | None,
        typer.Option("--clone-root", help="Directory where repositories will be cloned."),
    ] = None,
    repos: Annotated[
        list[str] | None,
        typer.Option(
            "--repos",
            help="Non-interactive mode: owner/repo names to take over.",
        ),
    ] = None,
    no_start: Annotated[
        bool,
        typer.Option("--no-start", help="Take over without starting daemon processes."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview the takeover plan without making changes."),
    ] = False,
) -> int:
    """Take over GitHub repositories: clone, init, register, and start daemons."""
    return _run_typer_command(
        "takeover",
        owner=owner,
        limit=limit,
        clone_root=clone_root,
        repos=tuple(repos or ()),
        start_daemons=not no_start,
        dry_run=dry_run,
    )


__all__ = ["takeover_command"]
