"""Typer command tree for the issue-agent-runner CLI.

NOTE: This Typer frontend delegates actual execution to ``backend.api.cli``
(``_run_parsed_command``). When adding or changing CLI options, defaults, or
argument structure, keep ``cli.py`` in sync so tests, help text, and the real
entry point behave the same way.
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
from backend.api.cli_completion import (
    register_completion_commands,
)


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


_HELP_CONTEXT = {"help_option_names": ["-h", "--help"]}

app = typer.Typer(
    name="iar",
    help="Issue Agent Runner CLI.",
    no_args_is_help=True,
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
workflow_app = typer.Typer(
    help="Install and manage bundled workflow templates.",
    no_args_is_help=True,
    context_settings=_HELP_CONTEXT,
)
app.add_typer(labels_app, name="labels")
app.add_typer(issue_app, name="issue")
app.add_typer(completion_app, name="completion")
register_completion_commands(completion_app)
app.add_typer(worktree_app, name="worktree")
app.add_typer(registry_app, name="registry")
app.add_typer(workflow_app, name="workflow")

RepoOption = Annotated[
    str | None, typer.Option("--repo", help="Target repository path.")
]
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
    selector_options = _typer_selector_options(
        ctx, repo=repo, repo_id=repo_id, config=config
    )
    return _run_typer_command(command, **selector_options, **kwargs)


@app.callback()
def _app_callback(
    ctx: typer.Context,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
) -> None:
    """Store top-level options so legacy and modern forms both work."""
    ctx.obj = {"repo": repo, "repo_id": repo_id, "config": config}


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
    remote: Annotated[
        str | None, typer.Option("--remote", help="Git remote name.")
    ] = None,
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
) -> int:
    """Create repository-local .iar.toml config."""
    selector_options = _typer_selector_options(
        ctx, repo=None, repo_id=None, config=None
    )
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
    )


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
        bool, typer.Option("--dry-run", help="Print candidates without writing.")
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
    repo_id: Annotated[
        str, typer.Option("--repo-id", help="Registry identifier to reinitialize.")
    ],
    remote: Annotated[
        str, typer.Option("--remote", help="Git remote name to write.")
    ] = "origin",
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
    repo_id: Annotated[
        str, typer.Option("--repo-id", help="Registry identifier to remove.")
    ],
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
        error_console.print(
            "[red]Either --repo-id or --all is required for iar registry start.[/]"
        )
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
        typer.Option(
            "--all", help="Stop daemons for all repositories with running processes."
        ),
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
        error_console.print(
            "[red]Either --repo-id or --all is required for iar registry stop.[/]"
        )
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


def _run_issue_create_command(
    ctx: typer.Context,
    *,
    prd_paths: list[str],
    issue_type: IssueTypeChoice,
    title: str | None,
    ready: bool,
    agent: IssueAgentChoice,
    publish_prd: bool,
    force: bool,
    repo: str | None,
    repo_id: str | None,
    config: str | None,
    depends_on: tuple[int, ...] = (),
    depends_on_group: tuple[str, ...] = (),
) -> int:
    """Run the shared PRD-to-Issue command."""
    return _run_typer_repository_command(
        ctx,
        "issue create",
        repo=repo,
        repo_id=repo_id,
        config=config,
        prd_paths=prd_paths,
        type=_enum_value(issue_type),
        title=title,
        ready=ready,
        agent=_enum_value(agent),
        publish_prd=publish_prd,
        force=force,
        depends_on=depends_on,
        depends_on_group=depends_on_group,
    )


@issue_app.command("create")
def issue_create_command(
    ctx: typer.Context,
    prd_paths: Annotated[
        list[str],
        typer.Argument(
            help="One or more PRD Markdown files or directories containing PRD files."
        ),
    ],
    issue_type: Annotated[
        IssueTypeChoice,
        typer.Option("--type", help="Issue type label."),
    ] = IssueTypeChoice.feature,
    title: Annotated[
        str | None, typer.Option("--title", help="Override generated issue title.")
    ] = None,
    ready: Annotated[
        bool,
        typer.Option("--ready/--no-ready", help="Queue the Issue for a runner."),
    ] = False,
    agent: Annotated[
        IssueAgentChoice,
        typer.Option("--agent", help="Optional agent routing label."),
    ] = IssueAgentChoice.auto,
    publish_prd: Annotated[
        bool,
        typer.Option(
            "--publish-prd/--no-publish-prd",
            help="Publish the PRD before ready (default: on).",
        ),
    ] = True,
    force: Annotated[bool, typer.Option("--force", help="Bypass PRD checks.")] = False,
    depends_on: Annotated[
        list[int] | None,
        typer.Option("--depends-on", help="Upstream Issue number (repeatable)."),
    ] = None,
    depends_on_group: Annotated[
        list[str] | None,
        typer.Option("--depends-on-group", help="Upstream group label (repeatable)."),
    ] = None,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
) -> int:
    """Create GitHub Issues from one or more PRD files."""
    return _run_issue_create_command(
        ctx,
        prd_paths=prd_paths,
        issue_type=issue_type,
        title=title,
        ready=ready,
        agent=agent,
        publish_prd=publish_prd,
        force=force,
        depends_on=tuple(depends_on or ()),
        depends_on_group=tuple(depends_on_group or ()),
        repo=repo,
        repo_id=repo_id,
        config=config,
    )


@issue_app.command("list", context_settings=_HELP_CONTEXT)
def issue_list_command(
    ctx: typer.Context,
    repo: Annotated[
        str | None, typer.Option("--repo", help="Target repository path.")
    ] = None,
    repo_id: Annotated[
        str | None, typer.Option("--repo-id", help="Target configured repository ID.")
    ] = None,
    all_registered: Annotated[
        bool,
        typer.Option(
            "--all-registered",
            help="Force multi-repository scan even when cwd is an iAR project repo.",
        ),
    ] = False,
    state: Annotated[
        str, typer.Option("--state", help="Issue state filter: open|closed|all.")
    ] = "all",
    label: Annotated[
        str | None,
        typer.Option("--label", help="Only show Issues carrying this label."),
    ] = None,
    with_pr: Annotated[
        bool, typer.Option("--with-pr", help="Only show Issues with at least one PR.")
    ] = False,
    without_pr: Annotated[
        bool, typer.Option("--without-pr", help="Only show Issues with no PRs.")
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum Issues per repository (default: 100)."),
    ] = 100,
    output: Annotated[
        str, typer.Option("--output", help="Render format: table|json.")
    ] = "table",
) -> int:
    """List Issues with linked Pull Request status."""
    context_values = ctx.obj or {}
    effective_repo = repo if repo is not None else context_values.get("repo")
    effective_repo_id = (
        repo_id if repo_id is not None else context_values.get("repo_id")
    )
    return _run_typer_command(
        "issue list",
        repo=effective_repo,
        repo_id=effective_repo_id,
        config=None,
        all_registered=all_registered,
        state=state,
        label=label,
        with_pr=with_pr,
        without_pr=without_pr,
        limit=limit,
        output=output,
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
    )


@app.command("daemon")
def daemon_command(
    ctx: typer.Context,
    interval: DaemonIntervalOption = None,
    agent: RunAgentOption = RunAgentChoice.auto,
    max_issues: MaxIssuesOption = None,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
    all_repositories: AllRepositoriesOption = False,
) -> int:
    """Run the agent runner continuously."""
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
    """Run supervisor review continuously."""
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


@app.command("ask")
def ask_command(
    ctx: typer.Context,
    prompt: Annotated[str, typer.Argument(help="Natural language request.")],
    agent: Annotated[
        RunAgentChoice,
        typer.Option("--agent", help="Planner agent to use."),
    ] = RunAgentChoice.auto,
    plan_only: Annotated[
        bool,
        typer.Option("--plan-only", help="Only generate plan without executing."),
    ] = False,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Allow execution after confirmation."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Auto-confirm non-interactive execution."),
    ] = False,
    output: Annotated[
        str | None,
        typer.Option("--output", help="Output directory for decision audit."),
    ] = None,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
) -> int:
    """Ask the agent runner to decide the next safe action."""
    selector_options = _typer_selector_options(
        ctx, repo=repo, repo_id=repo_id, config=config
    )
    return _run_typer_command(
        "ask",
        **selector_options,
        prompt=prompt,
        agent=_enum_value(agent),
        plan_only=plan_only,
        execute=execute,
        yes=yes,
        output=output,
    )


@app.command("deliberate")
def deliberate_command(
    ctx: typer.Context,
    prompt: Annotated[str, typer.Argument(help="Requirement or question.")],
    agents: Annotated[
        str,
        typer.Option("--agents", help="Comma-separated participant profile IDs."),
    ] = "architect,skeptic,implementer",
    rounds: Annotated[
        int | None, typer.Option("--rounds", help="Number of discussion rounds.")
    ] = None,
    synthesizer: Annotated[
        str | None, typer.Option("--synthesizer", help="Agent to run synthesis.")
    ] = None,
    output: Annotated[
        str | None, typer.Option("--output", help="Output directory.")
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Optional session ID for reproducibility."),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Return non-zero exit code if any agent fails."),
    ] = False,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
) -> int:
    """Run a multi-agent deliberation session."""
    return _run_typer_repository_command(
        ctx,
        "deliberate",
        repo=repo,
        repo_id=repo_id,
        config=config,
        prompt=prompt,
        agents=agents,
        rounds=rounds,
        synthesizer=synthesizer,
        output=output,
        session_id=session_id,
        strict=strict,
    )


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


@workflow_app.command("install")
def workflow_install_command(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Argument(help="Workflow template name (e.g. 'preview').")
    ],
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
    selector_options = _typer_selector_options(
        ctx, repo=repo, repo_id=repo_id, config=config
    )
    return _run_typer_command(
        "workflow install",
        **selector_options,
        name=name,
        force=force,
        dry_run=dry_run,
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
    branch: Annotated[
        str, typer.Option("--branch", help="Branch name whose worktree to remove.")
    ],
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
    yes: Annotated[
        bool, typer.Option("--yes", help="Actually delete eligible branches.")
    ] = False,
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


@app.command("takeover")
def takeover_command(
    owner: Annotated[
        str | None,
        typer.Option(
            "--owner", help="GitHub user or organization whose repositories to list."
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit", help="Maximum number of repositories to fetch from GitHub."
        ),
    ] = 100,
    clone_root: Annotated[
        str | None,
        typer.Option(
            "--clone-root", help="Directory where repositories will be cloned."
        ),
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
        typer.Option(
            "--dry-run", help="Preview the takeover plan without making changes."
        ),
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
