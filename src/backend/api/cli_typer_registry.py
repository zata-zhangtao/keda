"""Typer commands under ``iar registry``.

Holds every command that operates on the global repository registry
(:func:`registry_scan_command`, :func:`registry_sync_command`,
:func:`registry_reinit_command`, :func:`registry_remove_command`,
:func:`registry_list_command`, :func:`registry_start_command`,
:func:`registry_stop_command`).
"""

from __future__ import annotations

from typing import Annotated

import typer

from backend.api.cli import error_console
from backend.api.cli_typer_app import _run_typer_command, registry_app


@registry_app.command("scan")
def registry_scan_command(
    scan_root: Annotated[str, typer.Argument(help="Directory to scan.")] = ".",
) -> int:
    """Discover IAR-initialized git repositories under a path."""
    return _run_typer_command(
        "registry scan",
        scan_root=scan_root,
    )


@registry_app.command("sync")
def registry_sync_command(
    scan_root: Annotated[str, typer.Argument(help="Directory to scan.")] = ".",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print candidates without writing."),
    ] = False,
) -> int:
    """Discover and register all IAR repositories under a path."""
    return _run_typer_command(
        "registry sync",
        scan_root=scan_root,
        dry_run=dry_run,
    )


@registry_app.command("reinit")
def registry_reinit_command(
    repo_id: Annotated[str, typer.Option("--repo-id", help="Registry identifier to reinitialize.")],
    remote: Annotated[str, typer.Option("--remote", help="Git remote name to write.")] = "origin",
    base_branch: Annotated[
        str | None, typer.Option("--base-branch", help="Base branch to write.")
    ] = None,
    start_daemons: Annotated[
        bool,
        typer.Option("--start-daemons", help="Restart daemon processes."),
    ] = False,
) -> int:
    """Re-initialize an already registered repository's local config."""
    return _run_typer_command(
        "registry reinit",
        repo_id=repo_id,
        remote=remote,
        base_branch=base_branch,
        start_daemons=start_daemons,
    )


@registry_app.command("remove")
def registry_remove_command(
    repo_id: Annotated[str, typer.Option("--repo-id", help="Registry identifier to remove.")],
    delete: Annotated[
        bool,
        typer.Option("--delete", help="Also delete the cloned repository directory."),
    ] = False,
) -> int:
    """Remove a repository from the registry and stop its daemons."""
    return _run_typer_command(
        "registry remove",
        repo_id=repo_id,
        delete=delete,
    )


@registry_app.command("list")
def registry_list_command() -> int:
    """List registered repositories and their daemon status."""
    return _run_typer_command("registry list")


@registry_app.command("start")
def registry_start_command(
    repo_id: Annotated[
        str | None,
        typer.Option("--repo-id", help="Registry identifier to start daemons for."),
    ] = None,
    all: Annotated[
        bool,
        typer.Option("--all", help="Start daemons for all enabled repositories."),
    ] = False,
    no_review_daemon: Annotated[
        bool,
        typer.Option(
            "--no-review-daemon",
            help="Only start/stop the agent daemon, skip the review daemon.",
        ),
    ] = False,
) -> int:
    """Start daemon and review-daemon for registered repositories."""
    if not repo_id and not all:
        error_console.print("[red]Either --repo-id or --all is required for iar registry start.[/]")
        return 1
    if repo_id and all:
        error_console.print("[red]--repo-id and --all are mutually exclusive.[/]")
        return 1
    return _run_typer_command(
        "registry start",
        repo_id=repo_id,
        all=all,
        no_review_daemon=no_review_daemon,
    )


@registry_app.command("stop")
def registry_stop_command(
    repo_id: Annotated[
        str | None,
        typer.Option("--repo-id", help="Registry identifier to stop daemons for."),
    ] = None,
    all: Annotated[
        bool,
        typer.Option("--all", help="Stop daemons for all repositories with running processes."),
    ] = False,
    no_review_daemon: Annotated[
        bool,
        typer.Option(
            "--no-review-daemon",
            help="Only start/stop the agent daemon, skip the review daemon.",
        ),
    ] = False,
) -> int:
    """Stop daemon and review-daemon for registered repositories."""
    if not repo_id and not all:
        error_console.print("[red]Either --repo-id or --all is required for iar registry stop.[/]")
        return 1
    if repo_id and all:
        error_console.print("[red]--repo-id and --all are mutually exclusive.[/]")
        return 1
    return _run_typer_command(
        "registry stop",
        repo_id=repo_id,
        all=all,
        no_review_daemon=no_review_daemon,
    )


__all__ = [
    "registry_list_command",
    "registry_reinit_command",
    "registry_remove_command",
    "registry_scan_command",
    "registry_start_command",
    "registry_stop_command",
    "registry_sync_command",
]
