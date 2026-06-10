"""Typer command tree for the issue-agent-runner CLI.

NOTE: This Typer frontend delegates actual execution to ``backend.api.cli``
(``_run_parsed_command``). When adding or changing CLI options, defaults, or
argument structure, keep ``cli.py`` in sync so tests, help text, and the real
entry point behave the same way.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from enum import Enum
from typing import Annotated, Any

import typer
from typer import _click as typer_click
from typer.completion import get_completion_script

from backend.api.cli import _run_parsed_command, error_console


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


class CompletionShellChoice(str, Enum):
    """Shells supported by the explicit completion installer."""

    bash = "bash"
    zsh = "zsh"
    fish = "fish"


app = typer.Typer(
    name="iar",
    help="Issue Agent Runner CLI.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
labels_app = typer.Typer(help="Manage GitHub labels.", no_args_is_help=True)
issue_app = typer.Typer(help="Create and manage GitHub Issues.", no_args_is_help=True)
completion_app = typer.Typer(help="Manage shell completion.", no_args_is_help=True)
worktree_app = typer.Typer(
    help="Manage iAR-owned Git worktrees for the current repository.",
    no_args_is_help=True,
)
app.add_typer(labels_app, name="labels")
app.add_typer(issue_app, name="issue")
app.add_typer(completion_app, name="completion")
app.add_typer(worktree_app, name="worktree")

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
CompletionShellOption = Annotated[
    CompletionShellChoice,
    typer.Option("--shell", "-s", help="Shell to generate completion for."),
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
    return _run_parsed_command(argparse.Namespace(command=command, **kwargs))


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


def _completion_script(shell: CompletionShellChoice) -> str:
    """Return the shell completion script for the iAR executable."""
    return get_completion_script(
        prog_name="iar",
        complete_var="_IAR_COMPLETE",
        shell=_enum_value(shell),
    )


def _append_unique_line(file_path: Path, line: str) -> bool:
    """Append a shell profile line when it is not already present."""
    existing_text = ""
    if file_path.exists():
        existing_text = file_path.read_text(encoding="utf-8")
        if line in existing_text.splitlines():
            return False
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as profile_file:
        if existing_text and not existing_text.endswith("\n"):
            profile_file.write("\n")
        profile_file.write(f"{line}\n")
    return True


def _install_completion_script(
    shell: CompletionShellChoice,
) -> tuple[Path, Path | None]:
    """Install iAR shell completion and return the script/profile paths."""
    script_content = _completion_script(shell)
    home_path = Path.home()
    if shell is CompletionShellChoice.zsh:
        completion_dir = home_path / ".zsh" / "completions"
        completion_path = completion_dir / "_iar"
        completion_path.parent.mkdir(parents=True, exist_ok=True)
        completion_path.write_text(f"{script_content}\n", encoding="utf-8")
        zshrc_path = home_path / ".zshrc"
        _append_unique_line(zshrc_path, "autoload -Uz compinit && compinit")
        source_line = f'[ -f "{completion_path}" ] && source "{completion_path}"'
        _append_unique_line(zshrc_path, source_line)
        return completion_path, zshrc_path
    if shell is CompletionShellChoice.bash:
        completion_path = home_path / ".config" / "iar" / "iar_completion.bash"
        completion_path.parent.mkdir(parents=True, exist_ok=True)
        completion_path.write_text(f"{script_content}\n", encoding="utf-8")
        bashrc_path = home_path / ".bashrc"
        source_line = f'[ -f "{completion_path}" ] && source "{completion_path}"'
        _append_unique_line(bashrc_path, source_line)
        return completion_path, bashrc_path
    completion_path = home_path / ".config" / "fish" / "completions" / "iar.fish"
    completion_path.parent.mkdir(parents=True, exist_ok=True)
    completion_path.write_text(f"{script_content}\n", encoding="utf-8")
    return completion_path, None


@app.callback()
def _app_callback(
    ctx: typer.Context,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
) -> None:
    """Store top-level options so legacy and modern forms both work."""
    ctx.obj = {"repo": repo, "repo_id": repo_id, "config": config}


@completion_app.command("show")
def completion_show_command(
    shell: CompletionShellOption = CompletionShellChoice.zsh,
) -> int:
    """Print a shell completion script."""
    typer.echo(_completion_script(shell))
    return 0


@completion_app.command("install")
def completion_install_command(
    shell: CompletionShellOption = CompletionShellChoice.zsh,
) -> int:
    """Install shell completion for the current user."""
    completion_path, profile_path = _install_completion_script(shell)
    typer.echo(f"Installed {shell.value} completion: {completion_path}")
    if profile_path is not None:
        typer.echo(f"Reload your shell with: source {profile_path}")
    else:
        typer.echo("Open a new terminal session to activate completion.")
    return 0


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
    prd_path: str,
    issue_type: IssueTypeChoice,
    title: str | None,
    ready: bool,
    agent: IssueAgentChoice,
    publish_prd: bool,
    force: bool,
    repo: str | None,
    repo_id: str | None,
    config: str | None,
    group: str = "",
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
        prd_path=prd_path,
        type=_enum_value(issue_type),
        title=title,
        ready=ready,
        agent=_enum_value(agent),
        publish_prd=publish_prd,
        force=force,
        group=group,
        depends_on=depends_on,
        depends_on_group=depends_on_group,
    )


@issue_app.command("create")
def issue_create_command(
    ctx: typer.Context,
    prd_path: Annotated[str, typer.Argument(help="PRD Markdown path.")],
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
        typer.Option("--publish-prd", help="Publish the PRD before ready."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Bypass PRD checks.")] = False,
    group: Annotated[
        str,
        typer.Option(
            "--group", help="Task group name (materialised as task-group/<name> label)."
        ),
    ] = "",
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
    """Create a GitHub Issue from a PRD file."""
    return _run_issue_create_command(
        ctx,
        prd_path=prd_path,
        issue_type=issue_type,
        title=title,
        ready=ready,
        agent=agent,
        publish_prd=publish_prd,
        force=force,
        group=group,
        depends_on=tuple(depends_on or ()),
        depends_on_group=tuple(depends_on_group or ()),
        repo=repo,
        repo_id=repo_id,
        config=config,
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


def main(argv: list[str] | None = None) -> int:
    """Run the Typer-powered CLI."""
    args = sys.argv[1:] if argv is None else argv
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
