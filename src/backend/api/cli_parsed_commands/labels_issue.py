"""``iar labels`` / ``iar issue create`` / ``iar issue list`` handlers.

Extracted from :mod:`backend.api.cli`'s monolithic ``_run_parsed_command``
dispatcher.
"""

from __future__ import annotations

from pathlib import Path

from backend.api.cli_console import console, error_console
from backend.api.cli_helpers import _resolve_cli_repository_targets
from backend.api.cli_parsed_context import ParsedCommandContext
from backend.api import cli as _cli
from backend.engines.agent_runner.factory import logger
from backend.api.cli_utils import _format_cli_exception


def run_labels_command(ctx: ParsedCommandContext) -> int:
    """``iar labels sync``: sync standard labels to the target repository."""
    contexts = _resolve_cli_repository_targets(
        parsed=ctx.parsed,
        runner_settings=ctx.runner_settings,
        repo_id=ctx.repo_id,
        repo_override=ctx.repo_override,
    )
    for context in contexts:
        _cli.require_iar_repository_initialized(context.repo_path, ctx.process_runner)
    if contexts:
        _cli._ensure_gh_auth_or_prompt(contexts[0].repo_path, ctx.process_runner)
    for context in contexts:
        github_client = ctx.github_client_factory(context.repo_path)
        _cli.sync_labels(labels_config=context.config.labels, github_client=github_client)
    logger.info("Labels are ready.")
    return 0


def run_issue_create_command(ctx: ParsedCommandContext) -> int:
    """``iar issue create``: create GitHub Issues from one or more PRD files."""
    raw_prd_paths = getattr(ctx.parsed, "prd_paths", [])

    context = _cli.resolve_issue_from_prd_target(
        ctx.runner_settings,
        repo_id=ctx.repo_id,
        repo_path_override=ctx.repo_override,
        cwd=Path.cwd(),
    )
    try:
        prd_paths, skipped_prd_paths = _cli._expand_prd_paths(context.repo_path, raw_prd_paths)
    except ValueError as exc:
        logger.error("iar issue create failed: %s", exc)
        return 1

    for skipped_prd_path in skipped_prd_paths:
        console.print(f"[yellow]Skipped PRD with existing Issue:[/] {skipped_prd_path}")
        logger.info("Skipped PRD with existing Issue: %s", skipped_prd_path)

    if not prd_paths:
        console.print("[green]All PRDs in the requested directories already have GitHub Issues.[/]")
        return 0

    if len(prd_paths) > 1 and ctx.parsed.title is not None:
        logger.error("--title cannot be used when creating Issues from multiple PRDs.")
        return 1

    _cli.require_iar_repository_initialized(context.repo_path, ctx.process_runner)
    _cli._ensure_gh_auth_or_prompt(context.repo_path, ctx.process_runner)
    github_client = _cli.create_github_client(context.repo_path, ctx.process_runner)
    gc_config = context.config.generated_content
    content_generator = None
    if gc_config.enabled and gc_config.issue_from_prd.enabled:
        if gc_config.issue_from_prd.mode == "agent":
            content_generator = _cli.create_content_generator(ctx.process_runner)

    failed_prd_paths: list[str] = []
    for prd_path_text in prd_paths:
        # publish_prd 默认开启；仅当用户显式 --no-publish-prd 时，
        # 先把 queue_ready 压成 False，避免 Issue 还没发布就已经 ready，
        # runner 在 worktree 里读到过时 PRD。交互式 prompt 在 push 成功后再补 ready。
        queue_ready_for_request = ctx.parsed.ready if ctx.parsed.publish_prd else False
        try:
            _, relative_prd_path = _cli.resolve_prd_paths(context.repo_path, Path(prd_path_text))
            issue_url = _cli.create_issue_from_prd(
                request=_cli.IssueFromPrdRequest(
                    repo_path=context.repo_path,
                    prd_path=Path(prd_path_text),
                    issue_type=ctx.parsed.type,
                    title_override=ctx.parsed.title,
                    queue_ready=queue_ready_for_request,
                    issue_agent=ctx.parsed.agent,
                    labels_config=context.config.labels,
                    force=ctx.parsed.force,
                    publish_prd=ctx.parsed.publish_prd,
                    git_remote=context.config.git.remote,
                    git_base_branch=context.config.git.base_branch,
                    generated_content_config=gc_config,
                    depends_on=tuple(getattr(ctx.parsed, "depends_on", []) or []),
                    depends_on_group=tuple(getattr(ctx.parsed, "depends_on_group", []) or []),
                    parse_evidence_format_with_agent=context.config.validation.parse_evidence_format_with_agent,
                    validation_language=context.config.validation.language,
                    structured_evidence=context.config.validation.structured_evidence,
                ),
                github_client=github_client,
                process_runner=ctx.process_runner,
                content_generator=content_generator,
            )

            published = False
            if not ctx.parsed.publish_prd:
                published = _cli._prompt_and_publish_prd_if_needed(
                    repo_path=context.repo_path,
                    relative_prd_path=relative_prd_path,
                    issue_url=issue_url,
                    queue_ready=ctx.parsed.ready,
                    git_remote=context.config.git.remote,
                    labels_config=context.config.labels,
                    github_client=github_client,
                    process_runner=ctx.process_runner,
                )
            if not ctx.parsed.ready or (
                ctx.parsed.ready and not ctx.parsed.publish_prd and not published
            ):
                logger.info(
                    "Issue created without '%s' label. "
                    "Use --ready if you want a runner to pick it up.",
                    context.config.labels.ready,
                )
            logger.info("Created GitHub Issue: %s", issue_url)
            console.print(f"[green]Created GitHub Issue:[/] {issue_url}")
        except Exception as exc:  # noqa: BLE001 - batch should continue.
            failed_prd_paths.append(prd_path_text)
            error_detail = _format_cli_exception(exc)
            logger.error(
                "Failed to create Issue from %s:\n%s",
                prd_path_text,
                error_detail,
            )
            error_console.print(f"[red]Failed to create Issue from {prd_path_text}:[/]")
            error_console.print(error_detail, markup=False)

    if failed_prd_paths:
        logger.error(
            "Issue creation failed for %d PRD(s): %s",
            len(failed_prd_paths),
            ", ".join(failed_prd_paths),
        )
        return 1
    return 0


def run_issue_list_command(ctx: ParsedCommandContext) -> int:
    """``iar issue list``: list Issues with linked PR status."""
    if ctx.parsed.with_pr and ctx.parsed.without_pr:
        logger.error("--with-pr and --without-pr are mutually exclusive.")
        return 1
    from backend.core.use_cases.issue_pr_status import (
        IssueListRequest,
        list_issues_with_prs,
        make_default_has_local_iar_repo,
        render_issue_with_pulls_json,
        render_pr_column,
    )
    from rich.console import Console
    from rich.table import Table

    def _resolve_targets(
        *,
        repo_id: str | None,
        repo_path_override: str | None,
        all_repositories: bool,
    ) -> list:
        return _cli.resolve_repository_targets(
            ctx.runner_settings,
            repo_id=repo_id,
            repo_path_override=repo_path_override,
            all_repositories=all_repositories,
        )

    request = IssueListRequest(
        repo_id=ctx.repo_id,
        repo_path_override=ctx.repo_override,
        all_repositories=getattr(ctx.parsed, "all_registered", False),
        state=ctx.parsed.state,
        label=ctx.parsed.label,
        with_pr=ctx.parsed.with_pr,
        without_pr=ctx.parsed.without_pr,
        limit=ctx.parsed.limit,
        target_resolver=_resolve_targets,
        has_local_iar_repo=make_default_has_local_iar_repo(),
        github_client_factory=ctx.github_client_factory,
        process_runner=ctx.process_runner,
    )
    try:
        result = list_issues_with_prs(request)
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
        logger.error("iar issue list failed: %s", exc)
        error_console.print(f"[red]iar issue list failed:[/] {exc}")
        return 1

    if ctx.parsed.output == "json":
        render_issue_with_pulls_json(result, console)
        return 1 if result.errors else 0

    render_console = Console()
    multi_repo = len({row.repo for row in result.rows if row.repo}) > 1
    output_format = ctx.parsed.output
    if output_format == "table":
        render_pr_column(result, render_console, multi_repo=multi_repo)
        if result.errors:
            return 1
        return 0
    table = Table(show_header=True, header_style="bold")
    table.add_column("Repo")
    table.add_column("Issue")
    table.add_column("Title")
    table.add_column("State")
    table.add_column("Labels")
    table.add_column("PR")
    for row in result.rows:
        table.add_row(
            row.repo or "-",
            f"#{row.issue_number}",
            row.title,
            row.state,
            ",".join(row.labels),
            ",".join(row.pr_urls) if row.pr_urls else "-",
        )
    render_console.print(table)
    for repo_label, error_message in result.errors:
        error_console.print(f"[red]Error fetching {repo_label}:[/] {error_message}")
    return 1 if result.errors else 0


__all__ = ["run_issue_create_command", "run_issue_list_command", "run_labels_command"]
