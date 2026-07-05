"""``iar run`` / ``iar daemon`` / ``iar review`` / ``iar review-daemon``
/ ``iar recover`` / ``iar blocked-continue`` handlers.

Extracted from :mod:`backend.api.cli`'s monolithic ``_run_parsed_command``
dispatcher.
"""

from __future__ import annotations

from pathlib import Path

from backend.api.cli_console import console
from backend.api.cli_helpers import (
    _create_run_history_store_or_none,
    _ensure_gh_auth_or_prompt,
    _resolve_cli_repository_targets,
    _resolve_run_trigger,
)
from backend.api.cli_parsed_context import ParsedCommandContext
from backend.api.cli_registry import _run_daemon_status_command
from backend.api import cli as _cli
from backend.engines.agent_runner.factory import logger
from backend.engines.agent_runner.runner_live_view import create_runner_live_view


def run_run_command(ctx: ParsedCommandContext) -> int:
    """``iar run``: run one agent-runner polling cycle."""
    contexts = _resolve_cli_repository_targets(
        parsed=ctx.parsed,
        runner_settings=ctx.runner_settings,
        repo_id=ctx.repo_id,
        repo_override=ctx.repo_override,
    )
    for context in contexts:
        _cli.require_iar_repository_initialized(context.repo_path, ctx.process_runner)
    if contexts:
        _ensure_gh_auth_or_prompt(contexts[0].repo_path, ctx.process_runner)
    content_generator = _cli.create_content_generator(ctx.process_runner)

    def transcript_runner_factory(repo_path: Path) -> object:
        return _cli.create_transcript_runner(ctx.process_runner)

    return _cli.run_agent_repositories_once(
        contexts=contexts,
        dry_run=ctx.parsed.dry_run,
        agent=ctx.parsed.agent,
        max_issues=ctx.parsed.max_issues or ctx.runner_settings.runner.max_issues,
        process_runner=ctx.process_runner,
        github_client_factory=ctx.github_client_factory,
        content_generator=content_generator,
        run_history_store=_create_run_history_store_or_none(),
        run_trigger=_resolve_run_trigger("run"),
        max_prd_issues=1,
        transcript_runner_factory=transcript_runner_factory,
        max_deliberation_issues=ctx.runner_settings.daemon.max_deliberation_issues,
    )


def run_daemon_command(ctx: ParsedCommandContext) -> int:
    """``iar daemon``: run daemon continuously or report status."""
    daemon_command = getattr(ctx.parsed, "daemon_command", "run")
    if daemon_command == "status":
        return _run_daemon_status_command(
            parsed=ctx.parsed,
            process_runner=ctx.process_runner,
            runner_settings=ctx.runner_settings,
            repo_id=ctx.repo_id,
            repo_override=ctx.repo_override,
        )
    contexts = _resolve_cli_repository_targets(
        parsed=ctx.parsed,
        runner_settings=ctx.runner_settings,
        repo_id=ctx.repo_id,
        repo_override=ctx.repo_override,
    )
    for context in contexts:
        _cli.require_iar_repository_initialized(context.repo_path, ctx.process_runner)
    if contexts:
        _ensure_gh_auth_or_prompt(contexts[0].repo_path, ctx.process_runner)
    interval = (
        ctx.parsed.interval
        if ctx.parsed.interval is not None
        else ctx.runner_settings.daemon.run_interval_seconds
    )
    # Parallel execution: --concurrency overrides the toml default
    # ([agent_runner.runner].max_concurrent_issues; 1 = sequential).
    # A per-Issue live view (Rich on a TTY, plain otherwise) is created
    # only when running >1 in parallel; the sequential path is unchanged.
    daemon_concurrency = (
        getattr(ctx.parsed, "concurrency", None) or ctx.runner_settings.runner.max_concurrent_issues
    )
    daemon_output_view = create_runner_live_view() if daemon_concurrency > 1 else None

    def content_generator_factory(repo_path: Path):
        return _cli.create_content_generator(ctx.process_runner)

    def transcript_runner_factory(repo_path: Path) -> object:
        return _cli.create_transcript_runner(ctx.process_runner)

    # Single-instance guard: a second daemon for an already-served
    # repository would double the queue polling and agent spawns, so
    # refuse to start rather than pile up duplicate daemons.
    daemon_repo_ids = [context.repo_id for context in contexts]
    daemon_locks_dir = _cli.daemon_lock_dir(ctx.runner_settings.console.process_registry_path)
    try:
        acquired_daemon_locks = _cli.acquire_daemon_locks(daemon_locks_dir, daemon_repo_ids)
    except _cli.DaemonAlreadyRunningError as already_running:
        from backend.api.cli_console import error_console

        error_console.print(f"[red]{already_running}[/]")
        return 1
    try:
        _cli.run_agent_daemon(
            contexts=contexts,
            interval=interval,
            agent=ctx.parsed.agent,
            max_issues=ctx.parsed.max_issues or ctx.runner_settings.runner.max_issues,
            process_runner=ctx.process_runner,
            github_client_factory=ctx.github_client_factory,
            content_generator_factory=content_generator_factory,
            run_history_store=_create_run_history_store_or_none(),
            run_trigger=_resolve_run_trigger("daemon"),
            max_prd_issues=1,
            transcript_runner_factory=transcript_runner_factory,
            max_deliberation_issues=ctx.runner_settings.daemon.max_deliberation_issues,
            concurrency=daemon_concurrency,
            output_view=daemon_output_view,
            reclaim_stale_running=ctx.runner_settings.daemon.reclaim_stale_running,
            reclaim_ttl_seconds=ctx.runner_settings.daemon.reclaim_ttl_seconds,
        )
    finally:
        _cli.release_daemon_locks(acquired_daemon_locks)
    return 0


def run_review_command(ctx: ParsedCommandContext) -> int:
    """``iar review``: one supervisor review polling cycle."""
    contexts = _resolve_cli_repository_targets(
        parsed=ctx.parsed,
        runner_settings=ctx.runner_settings,
        repo_id=ctx.repo_id,
        repo_override=ctx.repo_override,
    )
    for context in contexts:
        _cli.require_iar_repository_initialized(context.repo_path, ctx.process_runner)
    if contexts:
        _ensure_gh_auth_or_prompt(contexts[0].repo_path, ctx.process_runner)
    aggregated_exit_code = 0
    for context in contexts:
        github_client = ctx.github_client_factory(context.repo_path)
        try:
            repo_exit_code = _cli.review_once(
                repo_path=context.repo_path,
                config=context.config,
                dry_run=ctx.parsed.dry_run,
                agent=ctx.parsed.agent,
                max_issues=ctx.parsed.max_issues or ctx.runner_settings.runner.max_issues,
                github_client=github_client,
                process_runner=ctx.process_runner,
            )
            if repo_exit_code != 0:
                aggregated_exit_code = 1
        except Exception as exc:  # noqa: BLE001
            aggregated_exit_code = 1
            logger.error(
                "Repository '%s' review_once failed: %s",
                context.repo_id,
                exc,
            )
    return aggregated_exit_code


def run_review_daemon_command(ctx: ParsedCommandContext) -> int:
    """``iar review-daemon``: run supervisor review continuously."""
    contexts = _resolve_cli_repository_targets(
        parsed=ctx.parsed,
        runner_settings=ctx.runner_settings,
        repo_id=ctx.repo_id,
        repo_override=ctx.repo_override,
    )
    for context in contexts:
        _cli.require_iar_repository_initialized(context.repo_path, ctx.process_runner)
    if contexts:
        _ensure_gh_auth_or_prompt(contexts[0].repo_path, ctx.process_runner)
    interval = (
        ctx.parsed.interval
        if ctx.parsed.interval is not None
        else ctx.runner_settings.daemon.review_interval_seconds
    )
    _cli.run_review_daemon(
        contexts=contexts,
        interval=interval,
        agent=ctx.parsed.agent,
        max_issues=ctx.parsed.max_issues or ctx.runner_settings.runner.max_issues,
        process_runner=ctx.process_runner,
        github_client_factory=ctx.github_client_factory,
    )
    return 0


def run_recover_command(ctx: ParsedCommandContext) -> int:
    """``iar recover``: resume a failed publish operation for an Issue."""
    from backend.core.use_cases.recover_publish import (
        PublishRecoveryError,
        PublishRecoveryRequest,
        recover_publish_issue,
    )

    contexts = _cli.resolve_repository_targets(
        ctx.runner_settings,
        repo_id=ctx.repo_id,
        repo_path_override=ctx.repo_override,
    )
    for context in contexts:
        _cli.require_iar_repository_initialized(context.repo_path, ctx.process_runner)
    if len(contexts) != 1:
        logger.error(
            "recover requires exactly one target repository. Use --repo or --repo-id to specify."
        )
        return 1
    context = contexts[0]
    github_client = _cli.create_github_client(context.repo_path, ctx.process_runner)
    request = PublishRecoveryRequest(
        issue_number=ctx.parsed.issue,
        expected_branch=ctx.parsed.branch,
    )
    try:
        result = recover_publish_issue(
            request=request,
            repo_path=context.repo_path,
            config=context.config,
            github_client=github_client,
            process_runner=ctx.process_runner,
        )
        logger.info(
            "Publish recovered for Issue #%d: %s",
            result.issue_number,
            result.pr_url,
        )
        console.print(
            f"[green]Publish recovered for Issue #{result.issue_number}:[/] {result.pr_url}"
        )
        return 0
    except PublishRecoveryError as exc:
        logger.error(
            "Publish recovery failed (category=%s): %s",
            exc.failure_category,
            exc,
        )
        return 1


def run_blocked_continue_command(ctx: ParsedCommandContext) -> int:
    """``iar blocked-continue``: resume a blocked Issue after fixing paths."""
    from backend.core.use_cases.blocked_continue import (
        BlockedContinueError,
        blocked_continue_issue,
    )
    from backend.api.cli_console import error_console

    contexts = _cli.resolve_repository_targets(
        ctx.runner_settings,
        repo_id=ctx.repo_id,
        repo_path_override=ctx.repo_override,
    )
    for context in contexts:
        _cli.require_iar_repository_initialized(context.repo_path, ctx.process_runner)
    if len(contexts) != 1:
        logger.error(
            "blocked-continue requires exactly one target repository. "
            "Use --repo or --repo-id to specify."
        )
        return 1
    context = contexts[0]
    github_client = _cli.create_github_client(context.repo_path, ctx.process_runner)
    try:
        claimed = blocked_continue_issue(
            issue_number=ctx.parsed.issue,
            repo_path=context.repo_path,
            config=context.config,
            agent=ctx.parsed.agent,
            github_client=github_client,
            process_runner=ctx.process_runner,
        )
        if claimed:
            console.print(f"[green]Issue #{ctx.parsed.issue} resumed successfully.[/]")
            return 0
        console.print(f"[yellow]Issue #{ctx.parsed.issue} was claimed by another runner.[/]")
        return 0
    except BlockedContinueError as exc:
        logger.error("blocked-continue failed: %s", exc)
        error_console.print(f"[red]blocked-continue failed:[/] {exc}")
        return 1


__all__ = [
    "run_blocked_continue_command",
    "run_daemon_command",
    "run_recover_command",
    "run_review_command",
    "run_review_daemon_command",
    "run_run_command",
]
