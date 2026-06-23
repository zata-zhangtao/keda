"""Typer commands for the ``iar loop`` sub-app.

These wrappers live in their own module to keep ``cli_typer.py`` under the
project's 1000 non-blank line cap.
"""

from __future__ import annotations

from typing import Annotated

import typer

from backend.api.cli_typer import _run_typer_command, app, loop_app


@loop_app.command("create")
def loop_create_command(
    loop_id: Annotated[str, typer.Argument(help="Short kebab-case identifier.")],
    recipe: Annotated[
        str, typer.Option("--recipe", help="Path to the loop recipe Markdown file.")
    ],
    cron: Annotated[
        str | None,
        typer.Option("--cron", help="5-field cron expression overriding the recipe."),
    ] = None,
    every: Annotated[
        str | None,
        typer.Option(
            "--every",
            help="Interval shorthand ('10m'/'1h'/'1d') overriding the recipe.",
        ),
    ] = None,
    repo_id: Annotated[
        str | None,
        typer.Option(
            "--repo-id",
            help="Override the recipe's repo_id when registering the loop.",
        ),
    ] = None,
    repo: Annotated[
        str | None, typer.Option("--repo", help="Override the target repository path.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing loop entry.")
    ] = False,
) -> int:
    """Register a loop recipe as a persistent scheduler entry."""
    return _run_typer_command(
        "loop create",
        loop_id=loop_id,
        recipe=recipe,
        cron=cron,
        every=every,
        loop_repo_id=repo_id,
        loop_repo=repo,
        force=force,
    )


@loop_app.command("list")
def loop_list_command() -> int:
    """List all registered loops with their schedules and next fires."""
    return _run_typer_command("loop list")


@loop_app.command("cancel")
def loop_cancel_command(
    loop_id: Annotated[str, typer.Argument(help="Identifier of the loop to cancel.")],
) -> int:
    """Remove a loop entry from the local scheduler state."""
    return _run_typer_command("loop cancel", loop_id=loop_id)


@loop_app.command("run")
def loop_run_command(
    loop_id: Annotated[str, typer.Argument(help="Identifier of the loop to fire.")],
    now: Annotated[
        bool,
        typer.Option("--now", help="Fire the loop immediately (required by the MVP)."),
    ] = True,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Render the PRD and report what would happen, no side effects.",
        ),
    ] = False,
    repo_id: Annotated[
        str | None, typer.Option("--repo-id", help="Override the recipe's repo_id.")
    ] = None,
    repo: Annotated[
        str | None, typer.Option("--repo", help="Override the target repository path.")
    ] = None,
) -> int:
    """Trigger a loop manually for testing or recovery."""
    return _run_typer_command(
        "loop run",
        loop_id=loop_id,
        now=now,
        dry_run=dry_run,
        loop_repo_id=repo_id,
        loop_repo=repo,
    )


@app.command("loop-daemon")
def loop_daemon_command(
    interval: Annotated[
        int | None,
        typer.Option(
            "--interval",
            help="Seconds between polling passes (default: 60).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Inspect the next fire plan once and exit without writing anything.",
        ),
    ] = False,
    repo_id: Annotated[
        str | None,
        typer.Option(
            "--repo-id",
            help="Override the repository used to resolve all loop targets.",
        ),
    ] = None,
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo", help="Override the local path of the target repository."
        ),
    ] = None,
) -> int:
    """Run the loop scheduler continuously (polls ~/.iar/loop-state.json)."""
    return _run_typer_command(
        "loop-daemon",
        interval=interval,
        dry_run=dry_run,
        loop_repo_id=repo_id,
        loop_repo=repo,
    )
