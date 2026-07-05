"""Typer commands for the agent-runner flow.

Holds the runner-side commands:

- :func:`run_command` (top-level ``iar run``)
- :func:`logs_command` (top-level ``iar logs``) — placed between
  ``run_command`` and ``review_command`` so the historical ``iar --help``
  command order is preserved.
- :func:`review_command` (top-level ``iar review``)
- :func:`review_daemon_command` (top-level ``iar review-daemon``)
- ``daemon_callback`` (default for ``iar daemon`` without subcommand)
- :func:`daemon_run_command` and :func:`daemon_status_command` (under
  ``iar daemon``)

Plus the ``_run_runner_command`` / ``_run_daemon_command`` helpers used
by every command in this module.

The top-level ``iar loop-daemon`` command lives in
:mod:`backend.api.cli_typer_loop` so the historical ``iar --help``
command order is preserved.
"""

from __future__ import annotations

from typing import Annotated

import typer

from backend.api.cli_typer_app import (
    AllRepositoriesOption,
    ConfigOption,
    ConcurrencyOption,
    DaemonIntervalOption,
    LogsKindChoice,
    MaxIssuesOption,
    RepoIdOption,
    RepoOption,
    RunAgentChoice,
    RunAgentOption,
    _enum_value,
    _run_typer_command,
    _run_typer_repository_command,
    _typer_selector_options,
    app,
    daemon_app,
)


def _run_runner_command(
    ctx: typer.Context,
    *,
    command: str,
    dry_run: bool,
    agent: RunAgentChoice,
    max_issues: int | None,
    repo: str | None,
    repo_id: str | None,
    config: str | None,
    all_repositories: bool,
) -> int:
    """Run `run` or `review` through the shared dispatch path."""
    return _run_typer_repository_command(
        ctx,
        command,
        repo=repo,
        repo_id=repo_id,
        config=config,
        dry_run=dry_run,
        agent=_enum_value(agent),
        max_issues=max_issues,
        all_repositories=all_repositories,
    )


@app.command("run")
def run_command(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview only.")] = False,
    agent: RunAgentOption = RunAgentChoice.auto,
    max_issues: MaxIssuesOption = None,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
    all_repositories: AllRepositoriesOption = False,
) -> int:
    """Run one agent-runner polling cycle."""
    return _run_runner_command(
        ctx,
        command="run",
        dry_run=dry_run,
        agent=agent,
        max_issues=max_issues,
        repo=repo,
        repo_id=repo_id,
        config=config,
        all_repositories=all_repositories,
    )


@app.command("logs")
def logs_command(
    ctx: typer.Context,
    kind: Annotated[
        LogsKindChoice,
        typer.Option("--kind", help="Process kind to tail: daemon or review_daemon."),
    ] = LogsKindChoice.daemon,
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", help="Number of recent lines to print."),
    ] = 200,
    follow: Annotated[
        bool,
        typer.Option("-f", "--follow", help="Follow log output continuously."),
    ] = False,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
) -> int:
    """Print the most recent log lines for a managed daemon process.

    By default the command targets the daemon for the repository inferred
    from the current working directory.  Pass --kind review_daemon to tail
    the review-daemon log instead.
    """
    selector_options = _typer_selector_options(ctx, repo=repo, repo_id=repo_id, config=config)
    return _run_typer_command(
        "logs",
        **selector_options,
        kind=_enum_value(kind),
        lines=lines,
        follow=follow,
    )


@app.command("review")
def review_command(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview only.")] = False,
    agent: RunAgentOption = RunAgentChoice.auto,
    max_issues: MaxIssuesOption = None,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
    all_repositories: AllRepositoriesOption = False,
) -> int:
    """Run one supervisor review polling cycle."""
    return _run_runner_command(
        ctx,
        command="review",
        dry_run=dry_run,
        agent=agent,
        max_issues=max_issues,
        repo=repo,
        repo_id=repo_id,
        config=config,
        all_repositories=all_repositories,
    )


def _run_daemon_command(
    ctx: typer.Context,
    *,
    command: str,
    interval: int | None,
    agent: RunAgentChoice,
    max_issues: int | None,
    repo: str | None,
    repo_id: str | None,
    config: str | None,
    all_repositories: bool,
    concurrency: int | None = None,
) -> int:
    """Run daemon or review-daemon through the shared dispatch path."""
    return _run_typer_repository_command(
        ctx,
        command,
        repo=repo,
        repo_id=repo_id,
        config=config,
        interval=interval,
        agent=_enum_value(agent),
        max_issues=max_issues,
        all_repositories=all_repositories,
        concurrency=concurrency,
    )


@daemon_app.callback(invoke_without_command=True)
def daemon_callback(
    ctx: typer.Context,
    interval: DaemonIntervalOption = None,
    agent: RunAgentOption = RunAgentChoice.auto,
    max_issues: MaxIssuesOption = None,
    concurrency: ConcurrencyOption = None,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
    all_repositories: AllRepositoriesOption = False,
) -> None:
    """Backward-compatible default: `iar daemon` runs the daemon."""
    if ctx.invoked_subcommand is not None:
        return
    exit_code = _run_daemon_command(
        ctx,
        command="daemon",
        interval=interval,
        agent=agent,
        max_issues=max_issues,
        repo=repo,
        repo_id=repo_id,
        config=config,
        all_repositories=all_repositories,
        concurrency=concurrency,
    )
    raise typer.Exit(code=exit_code)


@daemon_app.command("run")
def daemon_run_command(
    ctx: typer.Context,
    interval: DaemonIntervalOption = None,
    agent: RunAgentOption = RunAgentChoice.auto,
    max_issues: MaxIssuesOption = None,
    concurrency: ConcurrencyOption = None,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
    all_repositories: AllRepositoriesOption = False,
) -> int:
    """Run the agent runner continuously.

    Defaults to the current initialized repository; pass --all to target
    every enabled registry entry instead.
    """
    return _run_daemon_command(
        ctx,
        command="daemon",
        interval=interval,
        agent=agent,
        max_issues=max_issues,
        repo=repo,
        repo_id=repo_id,
        config=config,
        all_repositories=all_repositories,
        concurrency=concurrency,
    )


@daemon_app.command("status")
def daemon_status_command(
    ctx: typer.Context,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
    all_repositories: AllRepositoriesOption = False,
) -> int:
    """Show running daemon and review-daemon processes."""
    selector_options = _typer_selector_options(ctx, repo=repo, repo_id=repo_id, config=config)
    return _run_typer_command(
        "daemon",
        daemon_command="status",
        all_repositories=all_repositories,
        **selector_options,
    )


@app.command("review-daemon")
def review_daemon_command(
    ctx: typer.Context,
    interval: DaemonIntervalOption = None,
    agent: RunAgentOption = RunAgentChoice.auto,
    max_issues: MaxIssuesOption = None,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
    all_repositories: AllRepositoriesOption = False,
) -> int:
    """Run supervisor review continuously.

    Defaults to the current initialized repository; pass --all to target
    every enabled registry entry instead.
    """
    return _run_daemon_command(
        ctx,
        command="review-daemon",
        interval=interval,
        agent=agent,
        max_issues=max_issues,
        repo=repo,
        repo_id=repo_id,
        config=config,
        all_repositories=all_repositories,
    )


# Re-export the ``_typer_selector_options`` helper so ``daemon_status_command``
# can reach it through the same import path the original ``cli_typer`` module
# exposed.

__all__ = [
    "_run_daemon_command",
    "_run_runner_command",
    "daemon_callback",
    "daemon_run_command",
    "daemon_status_command",
    "review_command",
    "review_daemon_command",
    "run_command",
]
