"""Typer commands for issue recovery.

Holds :func:`recover_command` and :func:`blocked_continue_command`. The
log inspection command lives in :mod:`backend.api.cli_typer_logs` so the
historical ``iar --help`` command order is preserved.
"""

from __future__ import annotations

from typing import Annotated

import typer

from backend.api.cli_typer_app import (
    ConfigOption,
    RepoIdOption,
    RepoOption,
    RunAgentChoice,
    RunAgentOption,
    _enum_value,
    _run_typer_repository_command,
    app,
)


@app.command("recover")
def recover_command(
    ctx: typer.Context,
    issue: Annotated[int, typer.Option("--issue", help="Issue number to recover.")],
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Explicitly confirm the current branch name."),
    ] = None,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
) -> int:
    """Resume a failed publish operation for an Issue."""
    return _run_typer_repository_command(
        ctx,
        "recover",
        repo=repo,
        repo_id=repo_id,
        config=config,
        issue=issue,
        branch=branch,
    )


@app.command("blocked-continue")
def blocked_continue_command(
    ctx: typer.Context,
    issue: Annotated[int, typer.Option("--issue", help="Issue number to continue.")],
    agent: RunAgentOption = RunAgentChoice.auto,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
) -> int:
    """Resume a blocked Issue after resolving forbidden paths."""
    return _run_typer_repository_command(
        ctx,
        "blocked-continue",
        repo=repo,
        repo_id=repo_id,
        config=config,
        issue=issue,
        agent=_enum_value(agent),
    )


__all__ = ["blocked_continue_command", "recover_command"]
