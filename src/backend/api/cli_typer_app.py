"""Typer app definition and shared option types.

Owns the :data:`app` instance, every sub-application (``labels_app``,
``issue_app``, ``registry_app``, ``daemon_app``, ``worktree_app``,
``workflow_app``, ``loop_app``, ``completion_app``), the shared
``Annotated[...]`` option types, and the small ``_run_typer_*`` /
``_typer_selector_options`` helpers used by every Typer command module
under :mod:`backend.api.cli_typer_*`.

The command implementations live in the per-domain modules
(:mod:`backend.api.cli_typer_init`, :mod:`backend.api.cli_typer_runner`,
…) and are imported for side effects at the bottom of this file so the
Typer decorators register against :data:`app` before :func:`main` is
called.
"""

from __future__ import annotations

import argparse
import sys
from enum import Enum
from importlib import metadata as importlib_metadata
from typing import Annotated, Any

import typer
from typer import _click as typer_click

from backend.api.cli import _run_parsed_command, error_console
from backend.api.cli_completion import register_completion_commands

__all__ = [
    "AllRepositoriesOption",
    "ConfigOption",
    "ConcurrencyOption",
    "DaemonIntervalOption",
    "IssueAgentChoice",
    "IssueTypeChoice",
    "LogsKindChoice",
    "MaxIssuesOption",
    "RepoIdOption",
    "RepoOption",
    "RunAgentChoice",
    "RunAgentOption",
    "_HELP_CONTEXT",
    "_app_callback",
    "_enum_value",
    "_run_typer_command",
    "_run_typer_repository_command",
    "_typer_selector_options",
    "app",
    "auth_app",
    "completion_app",
    "container_app",
    "daemon_app",
    "issue_app",
    "labels_app",
    "loop_app",
    "main",
    "registry_app",
    "worktree_app",
    "workflow_app",
]


def _resolve_keda_version() -> str:
    """Return the installed ``keda`` distribution version, falling back to ``0.0.0+unknown``.

    The install-smoke workflow shells out to ``iar --version`` after a
    ``uv tool install --reinstall --editable .``; the editable install resolves
    to the metadata recorded in ``pyproject.toml``. If the distribution cannot
    be located (for example when running from an unpacked sdist), we still want
    a well-formed version line instead of an exception so the smoke gate has a
    deterministic contract.
    """
    try:
        return importlib_metadata.version("keda")
    except importlib_metadata.PackageNotFoundError:
        return "0.0.0+unknown"


class RunAgentChoice(str, Enum):
    """Agent choices accepted by runner commands."""

    auto = "auto"
    codex = "codex"
    claude = "claude"
    kimi = "kimi"


class IssueAgentChoice(str, Enum):
    """Agent labels accepted by issue creation commands."""

    auto = "auto"
    codex = "codex"
    claude = "claude"
    kimi = "kimi"
    none = "none"


class IssueTypeChoice(str, Enum):
    """Issue type labels accepted by PRD issue creation."""

    feature = "feature"
    refactor = "refactor"
    bug = "bug"


class LogsKindChoice(str, Enum):
    """Kind selector for ``iar logs``."""

    daemon = "daemon"
    review_daemon = "review_daemon"


_HELP_CONTEXT = {"help_option_names": ["-h", "--help"]}

app = typer.Typer(
    name="iar",
    help="Issue Agent Runner CLI.",
    no_args_is_help=False,
    rich_markup_mode="rich",
    context_settings=_HELP_CONTEXT,
)
labels_app = typer.Typer(
    help="Manage GitHub labels.", no_args_is_help=True, context_settings=_HELP_CONTEXT
)
issue_app = typer.Typer(
    help="Create and manage GitHub Issues.",
    no_args_is_help=True,
    context_settings=_HELP_CONTEXT,
)
completion_app = typer.Typer(
    help="Manage shell completion.",
    no_args_is_help=True,
    context_settings=_HELP_CONTEXT,
)
worktree_app = typer.Typer(
    help="Manage iAR-owned Git worktrees for the current repository.",
    no_args_is_help=True,
    context_settings=_HELP_CONTEXT,
)
registry_app = typer.Typer(
    help="Manage the repository registry in config.toml.",
    no_args_is_help=True,
    context_settings=_HELP_CONTEXT,
)
daemon_app = typer.Typer(
    help="Run the agent runner continuously or inspect daemon status.",
    no_args_is_help=False,
    context_settings=_HELP_CONTEXT,
)
workflow_app = typer.Typer(
    help="Install and manage bundled workflow templates.",
    no_args_is_help=True,
    context_settings=_HELP_CONTEXT,
)
loop_app = typer.Typer(
    help="Register and manage recurring task generators.",
    no_args_is_help=True,
    context_settings=_HELP_CONTEXT,
)
container_app = typer.Typer(
    help="Manage the iar runner container (auth import, up, down, logs).",
    no_args_is_help=True,
    context_settings=_HELP_CONTEXT,
)
auth_app = typer.Typer(
    help="Manage the container-side authentication snapshot.",
    no_args_is_help=True,
    context_settings=_HELP_CONTEXT,
)
container_app.add_typer(auth_app, name="auth")
app.add_typer(labels_app, name="labels")
app.add_typer(issue_app, name="issue")
app.add_typer(completion_app, name="completion")
register_completion_commands(completion_app)
app.add_typer(worktree_app, name="worktree")
app.add_typer(registry_app, name="registry")
app.add_typer(daemon_app, name="daemon")
app.add_typer(workflow_app, name="workflow")
app.add_typer(loop_app, name="loop")
app.add_typer(container_app, name="container")

RepoOption = Annotated[str | None, typer.Option("--repo", help="Target repository path.")]
RepoIdOption = Annotated[
    str | None, typer.Option("--repo-id", help="Target configured repository ID.")
]
ConfigOption = Annotated[
    str | None,
    typer.Option(
        "--config",
        help="Deprecated: config is loaded from config.toml and env vars.",
    ),
]
AllRepositoriesOption = Annotated[
    bool,
    typer.Option(
        "--all",
        help="Process all enabled configured repositories.",
    ),
]
RunAgentOption = Annotated[
    RunAgentChoice,
    typer.Option("--agent", help="Agent runner to use."),
]
MaxIssuesOption = Annotated[
    int | None,
    typer.Option("--max-issues", help="Maximum number of issues to process."),
]
DaemonIntervalOption = Annotated[
    int | None,
    typer.Option("--interval", help="Polling interval."),
]
ConcurrencyOption = Annotated[
    int | None,
    typer.Option(
        "--concurrency",
        help=(
            "Issues to process in parallel per pass (default: "
            "[agent_runner.runner].max_concurrent_issues; 1 = sequential)."
        ),
    ),
]


def _enum_value(value: str | Enum) -> str:
    """Return a plain string for Typer enum values."""
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _typer_selector_options(
    ctx: typer.Context,
    *,
    repo: str | None,
    repo_id: str | None,
    config: str | None,
) -> dict[str, str | None]:
    """Merge command-level repository selectors with top-level selectors."""
    context_values = ctx.obj or {}
    return {
        "repo": repo if repo is not None else context_values.get("repo"),
        "repo_id": repo_id if repo_id is not None else context_values.get("repo_id"),
        "config": config if config is not None else context_values.get("config"),
    }


def _run_typer_command(command: str, **kwargs: Any) -> int:
    """Convert Typer command arguments to the dispatch namespace."""
    namespace_kwargs = {"config": None, **kwargs}
    return _run_parsed_command(argparse.Namespace(command=command, **namespace_kwargs))


def _run_typer_repository_command(
    ctx: typer.Context,
    command: str,
    *,
    repo: str | None,
    repo_id: str | None,
    config: str | None,
    **kwargs: Any,
) -> int:
    """Run a command that accepts repository selector options."""
    selector_options = _typer_selector_options(ctx, repo=repo, repo_id=repo_id, config=config)
    return _run_typer_command(command, **selector_options, **kwargs)


@app.callback(invoke_without_command=True)
def _app_callback(
    ctx: typer.Context,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
    agent: Annotated[
        RunAgentChoice | None,
        typer.Option(
            "--agent",
            help="Override the REPL default agent. "
            "Accepts codex/claude/kimi; 'auto' falls back to "
            "[agent_runner.repl].default_agent.",
        ),
    ] = None,
) -> None:
    """Store top-level options and dispatch the no-arg REPL entrypoint."""
    ctx.obj = {"repo": repo, "repo_id": repo_id, "config": config}
    if ctx.invoked_subcommand is not None:
        return
    # No subcommand and the user did not pass --help. The REPL entrypoint
    # only makes sense inside an interactive terminal; otherwise fall back
    # to Typer's standard help-and-exit behaviour so CI / pipe-driven
    # scripts keep working.
    agent_override = _enum_value(agent) if agent is not None else None
    if sys.stdin.isatty():
        _run_typer_command(
            "repl",
            repo=repo,
            repo_id=repo_id,
            config=config,
            agent=agent_override,
        )
        return
    typer.echo(ctx.get_help())
    raise typer.Exit(code=1)


# Side-effect imports: register every Typer command against ``app`` and the
# sub-apps defined above. Keep them after the ``_app_callback`` definition so
# the decorators can reach the registry, options, and helpers.
#
# Import order matters: Typer preserves the order in which commands register
# on a Typer app. The original ``cli_typer`` module defined commands in this
# order: init → registry → labels → issue → run/review/review-daemon/loop-daemon
# → logs → recover/blocked-continue → ask/repl/deliberate → worktree/workflow
# → takeover → loop. The modules below are imported in that exact order so
# ``iar --help`` byte-output stays stable.
from backend.api import (  # noqa: E402,F401
    cli_typer_init,
    cli_typer_registry,
    cli_typer_labels,
    cli_typer_issue,
    cli_typer_runner,
    cli_typer_recover,
    cli_typer_agent,
    cli_typer_worktree,
    cli_typer_workflow,
    cli_typer_container,
    cli_typer_takeover,
    cli_typer_loop,
)


def main(argv: list[str] | None = None) -> int:
    """Run the Typer-powered CLI."""
    args = sys.argv[1:] if argv is None else argv
    if "--version" in args or "-V" in args:
        typer.echo(f"iar {_resolve_keda_version()}")
        return 0
    try:
        result = app(args=args, prog_name="iar", standalone_mode=False)
    except typer_click.exceptions.NoArgsIsHelpError:
        return 0
    except typer_click.exceptions.ClickException as exc:
        exc.show()
        return exc.exit_code
    except typer_click.exceptions.Abort:
        error_console.print("[red]Aborted.[/]")
        return 1
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 1
    return int(result or 0)
