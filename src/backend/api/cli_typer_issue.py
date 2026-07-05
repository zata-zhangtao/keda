"""Typer commands under ``iar issue``.

Holds :func:`issue_create_command` (PRD-to-Issue creation) and
:func:`issue_list_command` (cross-repo issue listing with PR linkage).
"""

from __future__ import annotations

from typing import Annotated

import typer

from backend.api.cli_typer_app import (
    ConfigOption,
    IssueAgentChoice,
    IssueTypeChoice,
    RepoIdOption,
    RepoOption,
    _HELP_CONTEXT,
    _enum_value,
    _run_typer_command,
    _run_typer_repository_command,
    issue_app,
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
        typer.Argument(help="One or more PRD Markdown files or directories containing PRD files."),
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
    repo: Annotated[str | None, typer.Option("--repo", help="Target repository path.")] = None,
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
    output: Annotated[str, typer.Option("--output", help="Render format: table|json.")] = "table",
) -> int:
    """List Issues with linked Pull Request status."""
    context_values = ctx.obj or {}
    effective_repo = repo if repo is not None else context_values.get("repo")
    effective_repo_id = repo_id if repo_id is not None else context_values.get("repo_id")
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


__all__ = ["_run_issue_create_command", "issue_create_command", "issue_list_command"]
