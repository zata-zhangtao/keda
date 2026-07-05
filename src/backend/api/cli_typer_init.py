"""Typer commands under ``iar init``.

Holds :func:`init_command`. Imports the shared :func:`_run_typer_command`
and option types from :mod:`backend.api.cli_typer_app`.
"""

from __future__ import annotations

from typing import Annotated

import typer

from backend.api.cli_typer_app import (
    _run_typer_command,
    _typer_selector_options,
    app,
)


@app.command("init")
def init_command(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print config without writing.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing .iar.toml.")
    ] = False,
    repository_id: Annotated[
        str | None, typer.Option("--id", help="Repository ID to write.")
    ] = None,
    display_name: Annotated[
        str | None, typer.Option("--display-name", help="Repository display name.")
    ] = None,
    remote: Annotated[str | None, typer.Option("--remote", help="Git remote name.")] = None,
    base_branch: Annotated[
        str | None, typer.Option("--base-branch", help="Git base branch.")
    ] = None,
    copy_skills: Annotated[
        bool,
        typer.Option(
            "--copy-skills/--no-copy-skills",
            help="Copy bundled skills (prd, code-reviewer) into .claude/skills/.",
        ),
    ] = True,
    skip_skills: Annotated[
        bool,
        typer.Option(
            "--skip-skills",
            help="Skip bundled skill copy (equivalent to --no-copy-skills).",
        ),
    ] = False,
    no_update_gitignore: Annotated[
        bool,
        typer.Option(
            "--no-update-gitignore",
            help=(
                "Do not add IAR runtime patterns (.iar/, .agent-runner/, "
                ".iar-worktrees/) to .gitignore. Default: managed by iar init."
            ),
        ),
    ] = False,
) -> int:
    """Create repository-local .iar.toml config."""
    selector_options = _typer_selector_options(ctx, repo=None, repo_id=None, config=None)
    return _run_typer_command(
        "init",
        **selector_options,
        dry_run=dry_run,
        force=force,
        repository_id=repository_id,
        display_name=display_name,
        remote=remote,
        base_branch=base_branch,
        copy_skills=copy_skills,
        skip_skills=skip_skills,
        no_update_gitignore=no_update_gitignore,
    )


__all__ = ["init_command"]
