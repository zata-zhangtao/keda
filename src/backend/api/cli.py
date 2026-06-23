"""Command-line interface for issue-agent-runner.

NOTE: This argparse-based parser is still the execution backend for
``backend.api.cli_typer``. When adding or changing CLI options, defaults, or
argument structure, keep ``cli_typer.py`` in sync so the actual ``iar`` entry
point and its help text stay consistent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from backend.api.cli_console import console, error_console
from backend.api.cli_helpers import (
    _create_run_history_store_or_none,
    _ensure_gh_auth_or_prompt,
    _handle_not_initialized_error,
    _print_worktree_cleanup_result,
    _resolve_cli_repository_targets,
    _resolve_default_daemon_target,
    _resolve_run_trigger,
)
from backend.api.cli_init import (
    _print_workflow_config_plan,
    _run_init_command,
)
from backend.api.cli_prd_utils import (
    _expand_prd_paths,
    _prompt_and_publish_prd_if_needed,
)
from backend.api.cli_registry import (
    _run_registry_list_command,
    _run_registry_reinit_command,
    _run_registry_remove_command,
    _run_registry_start_command,
    _run_registry_stop_command,
)
from backend.api.cli_takeover import _run_takeover_command
from backend.api.cli_utils import _format_cli_exception
from backend.core.use_cases.create_issue_from_prd import (
    IssueFromPrdRequest,
    create_issue_from_prd,
    resolve_prd_paths,
)
from backend.core.use_cases.run_agent_daemon import run_agent_daemon
from backend.core.use_cases.run_agent_deliberation import (
    DeliberationRequest,
    create_default_session_id,
    run_agent_deliberation,
)
from backend.core.use_cases.interactive_decision import run_interactive_decision
from backend.core.use_cases.run_agent_repositories_once import (
    run_agent_repositories_once,
)
from backend.core.use_cases.review_daemon import run_review_daemon
from backend.core.use_cases.review_once import review_once
from backend.core.use_cases.sync_labels import sync_labels
from backend.core.use_cases.worktree_cleanup import (
    WorktreeCleanupRequest,
    cleanup_iar_worktrees,
)
from backend.core.use_cases.worktree_env import copy_missing_env_files
from backend.core.shared.models.agent_deliberation import DeliberationSession
from backend.engines.agent_runner.factory import (
    create_content_generator,
    create_event_sink,
    create_github_client,
    create_planner_runner,
    create_process_runner,
    create_registry_editor,
    create_transcript_runner,
    get_agent_runner_settings,
    logger,
    resolve_issue_from_prd_target,
    resolve_repository_targets,
    write_deliberation_outputs,
)
from backend.engines.agent_runner.failure_resolver import AgentFailureResolver
from backend.engines.agent_runner.live_terminal import create_output_view
from backend.engines.agent_runner.repository_local import (
    IARRepositoryNotInitializedError,
    detect_git_repository_root,
    discover_iar_repositories,
    require_iar_repository_initialized,
)
from backend.engines.agent_runner.worktree_cli import (
    build_worktree_manager,
)
from backend.engines.agent_runner.workflow_install import (
    ExistingFileRefusedError,
    UnknownWorkflowError,
    WorkflowInstallOptions,
    install_workflow,
)

if TYPE_CHECKING:
    from backend.core.shared.interfaces.agent_runner import IGitHubClient


def _run_parsed_command(parsed: argparse.Namespace) -> int:
    """Run a command after CLI arguments have been parsed."""
    if parsed.config:
        logger.warning(
            "The --config flag is deprecated. Use config.toml or env vars instead."
        )

    repo_id: str | None = getattr(parsed, "repo_id", None)
    repo_override: str | None = getattr(parsed, "repo", None)

    if repo_id is not None and repo_override is not None:
        logger.error("--repo and --repo-id are mutually exclusive.")
        return 1

    # daemon / review-daemon 在未指定仓库时：
    # 1. cwd 命中唯一 enabled 注册仓 → 仅处理该仓（与 --repo-id 等价）
    # 2. cwd 命中 disabled 注册仓 → 报错
    # 3. cwd 命中多个 enabled 注册仓 → 报错，要求显式选择
    # 4. cwd 未命中任何注册仓或未初始化 → 报错，不再回退到 --all
    if parsed.command in ("daemon", "review-daemon"):
        if (
            repo_id is None
            and repo_override is None
            and not getattr(parsed, "all_repositories", False)
        ):
            default_target = _resolve_default_daemon_target()
            if default_target.error:
                logger.error(default_target.error)
                return 1
            repo_id = default_target.repo_id

    process_runner = create_process_runner()

    if parsed.command == "init":
        if repo_id is not None or repo_override is not None:
            logger.error(
                "iar init uses the current Git repository; omit --repo/--repo-id."
            )
            return 1
        return _run_init_command(parsed, process_runner)

    if parsed.command == "workflow install":
        if (
            repo_id is not None
            or repo_override is not None
            or parsed.config is not None
        ):
            logger.error(
                "iar workflow install uses the current Git repository; "
                "omit --repo/--repo-id/--config."
            )
            return 1
        try:
            install_result = install_workflow(
                WorkflowInstallOptions(
                    cwd=Path.cwd(),
                    name=parsed.name,
                    force=parsed.force,
                    dry_run=parsed.dry_run,
                ),
                process_runner,
            )
        except UnknownWorkflowError as exc:
            logger.error("%s", exc)
            return 1
        except ExistingFileRefusedError as exc:
            logger.error("%s", exc)
            return 1
        except IARRepositoryNotInitializedError as exc:
            return _handle_not_initialized_error(exc)
        except ValueError as exc:
            logger.error("iar workflow install failed: %s", exc)
            return 1
        if parsed.dry_run:
            console.print("[cyan]Would install workflow:[/] %s" % install_result.name)
            for plan in install_result.template_file_plans:
                marker = (
                    "[yellow]would overwrite[/]"
                    if plan.exists_on_disk
                    else "[green]would write[/]"
                )
                console.print(
                    "  %s %s (%d bytes)"
                    % (marker, plan.target_path, plan.bytes_to_write)
                )
            _print_workflow_config_plan(install_result.config_toml_plan, dry_run=True)
            return 0

        for plan in install_result.template_file_plans:
            if plan.exists_on_disk and install_result.refused_template_paths:
                continue
            console.print(
                "%s %s"
                % (
                    "[green]Wrote[/]"
                    if not plan.exists_on_disk
                    else "[yellow]Overwrote[/]",
                    plan.target_path,
                )
            )
        _print_workflow_config_plan(install_result.config_toml_plan, dry_run=False)
        return 0

    if parsed.command == "takeover":
        return _run_takeover_command(parsed, process_runner)

    if parsed.command == "registry scan":
        try:
            entries = discover_iar_repositories(
                scan_root=Path(parsed.scan_root),
                editor=create_registry_editor(),
            )
        except ValueError as exc:
            logger.error("iar registry scan failed: %s", exc)
            return 1
        if not entries:
            console.print("[yellow]No IAR repositories found.[/]")
            return 0
        for entry in entries:
            status = "registered" if entry.already_registered else "new"
            print(f"[{entry.repo_id}] {entry.path} ({status})")
        return 0

    if parsed.command == "registry sync":
        try:
            entries = discover_iar_repositories(
                scan_root=Path(parsed.scan_root),
                editor=create_registry_editor(),
            )
        except ValueError as exc:
            logger.error("iar registry sync failed: %s", exc)
            return 1
        new_entries = [entry for entry in entries if not entry.already_registered]
        if not new_entries:
            console.print("[green]No new IAR repositories to register.[/]")
            return 0
        if parsed.dry_run:
            console.print("[cyan]Would register:[/]")
            for entry in new_entries:
                console.print(f"  {entry.repo_id}: {entry.path}")
            return 0
        editor = create_registry_editor()
        added = 0
        for entry in new_entries:
            try:
                editor.add_repository(
                    repo_id=entry.repo_id,
                    path=entry.path,
                    display_name=entry.display_name,
                )
            except ValueError as exc:
                logger.warning("Skipping %s: %s", entry.repo_id, exc)
                continue
            added += 1
            console.print(f"[green]Registered:[/] {entry.repo_id}")
        console.print(f"[green]Registered {added} repository(s).[/]")
        return 0

    if parsed.command == "registry reinit":
        return _run_registry_reinit_command(parsed, process_runner)

    if parsed.command == "registry remove":
        return _run_registry_remove_command(parsed, process_runner)

    if parsed.command == "registry list":
        return _run_registry_list_command(process_runner)

    if parsed.command == "registry start":
        return _run_registry_start_command(parsed, process_runner)

    if parsed.command == "registry stop":
        return _run_registry_stop_command(parsed, process_runner)

    if parsed.command == "worktree":
        try:
            repo_root_path = detect_git_repository_root(Path.cwd(), process_runner)
            require_iar_repository_initialized(repo_root_path, process_runner)
        except ValueError as exc:
            logger.error("iar worktree failed: %s", exc)
            return 1
        except IARRepositoryNotInitializedError as exc:
            return _handle_not_initialized_error(exc)
        manager = build_worktree_manager(repo_root_path, process_runner)
        if parsed.worktree_command == "create":
            created_worktree_path = manager.create(
                branch=parsed.branch, base_branch=parsed.base_branch
            )
            copied_env_paths = copy_missing_env_files(
                repo_root_path, created_worktree_path
            )
            if copied_env_paths:
                logger.info(
                    "Copied %d missing env file(s) into worktree %s",
                    len(copied_env_paths),
                    created_worktree_path,
                )
            return 0
        if parsed.worktree_command == "path":
            print(str(manager.worktree_path(parsed.branch)))
            return 0
        if parsed.worktree_command == "remove":
            manager.remove(branch=parsed.branch)
            return 0
        if parsed.worktree_command == "cleanup":
            runner_settings = get_agent_runner_settings()
            contexts = resolve_repository_targets(
                runner_settings,
                fallback_path=str(repo_root_path),
            )
            if len(contexts) != 1:
                logger.error("iar worktree cleanup requires exactly one repository.")
                return 1
            context = contexts[0]
            _ensure_gh_auth_or_prompt(context.repo_path, process_runner)
            github_client = create_github_client(context.repo_path, process_runner)
            cleanup_request = WorktreeCleanupRequest(
                repo_path=context.repo_path,
                remote=context.config.git.remote,
                base_branch=context.config.git.base_branch,
                dry_run=parsed.dry_run or not parsed.yes,
                force=parsed.force,
                managed_worktree_root_path=manager.worktree_root,
            )
            cleanup_result = cleanup_iar_worktrees(
                cleanup_request,
                github_client=github_client,
                process_runner=process_runner,
            )
            _print_worktree_cleanup_result(cleanup_result)
            return 1 if cleanup_result.failed_count else 0
        logger.error("iar worktree: unknown subcommand %r", parsed.worktree_command)
        return 1

    runner_settings = get_agent_runner_settings()

    def github_client_factory(repo_path: Path) -> "IGitHubClient":
        return create_github_client(repo_path, process_runner)

    try:
        if parsed.command == "labels":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            for context in contexts:
                require_iar_repository_initialized(context.repo_path, process_runner)
            if contexts:
                _ensure_gh_auth_or_prompt(contexts[0].repo_path, process_runner)
            for context in contexts:
                github_client = github_client_factory(context.repo_path)
                sync_labels(
                    labels_config=context.config.labels, github_client=github_client
                )
            logger.info("Labels are ready.")
            return 0

        if parsed.command == "issue create":
            raw_prd_paths = getattr(parsed, "prd_paths", [])

            context = resolve_issue_from_prd_target(
                runner_settings,
                repo_id=repo_id,
                repo_path_override=repo_override,
                cwd=Path.cwd(),
            )
            try:
                prd_paths, skipped_prd_paths = _expand_prd_paths(
                    context.repo_path, raw_prd_paths
                )
            except ValueError as exc:
                logger.error("iar issue create failed: %s", exc)
                return 1

            for skipped_prd_path in skipped_prd_paths:
                console.print(
                    f"[yellow]Skipped PRD with existing Issue:[/] {skipped_prd_path}"
                )
                logger.info("Skipped PRD with existing Issue: %s", skipped_prd_path)

            if not prd_paths:
                console.print(
                    "[green]All PRDs in the requested directories already have "
                    "GitHub Issues.[/]"
                )
                return 0

            if len(prd_paths) > 1 and parsed.title is not None:
                logger.error(
                    "--title cannot be used when creating Issues from multiple PRDs."
                )
                return 1

            require_iar_repository_initialized(context.repo_path, process_runner)
            _ensure_gh_auth_or_prompt(context.repo_path, process_runner)
            github_client = create_github_client(context.repo_path, process_runner)
            gc_config = context.config.generated_content
            content_generator = None
            if gc_config.enabled and gc_config.issue_from_prd.enabled:
                if gc_config.issue_from_prd.mode == "agent":
                    content_generator = create_content_generator(process_runner)

            failed_prd_paths: list[str] = []
            for prd_path_text in prd_paths:
                # publish_prd 默认开启；仅当用户显式 --no-publish-prd 时，
                # 先把 queue_ready 压成 False，避免 Issue 还没发布就已经 ready，
                # runner 在 worktree 里读到过时 PRD。交互式 prompt 在 push 成功后再补 ready。
                queue_ready_for_request = parsed.ready if parsed.publish_prd else False
                try:
                    _, relative_prd_path = resolve_prd_paths(
                        context.repo_path, Path(prd_path_text)
                    )
                    issue_url = create_issue_from_prd(
                        request=IssueFromPrdRequest(
                            repo_path=context.repo_path,
                            prd_path=Path(prd_path_text),
                            issue_type=parsed.type,
                            title_override=parsed.title,
                            queue_ready=queue_ready_for_request,
                            issue_agent=parsed.agent,
                            labels_config=context.config.labels,
                            force=parsed.force,
                            publish_prd=parsed.publish_prd,
                            git_remote=context.config.git.remote,
                            git_base_branch=context.config.git.base_branch,
                            generated_content_config=gc_config,
                            depends_on=tuple(getattr(parsed, "depends_on", []) or []),
                            depends_on_group=tuple(
                                getattr(parsed, "depends_on_group", []) or []
                            ),
                            parse_evidence_format_with_agent=context.config.validation.parse_evidence_format_with_agent,
                            validation_language=context.config.validation.language,
                            structured_evidence=context.config.validation.structured_evidence,
                        ),
                        github_client=github_client,
                        process_runner=process_runner,
                        content_generator=content_generator,
                    )

                    published = False
                    if not parsed.publish_prd:
                        published = _prompt_and_publish_prd_if_needed(
                            repo_path=context.repo_path,
                            relative_prd_path=relative_prd_path,
                            issue_url=issue_url,
                            queue_ready=parsed.ready,
                            git_remote=context.config.git.remote,
                            labels_config=context.config.labels,
                            github_client=github_client,
                            process_runner=process_runner,
                        )
                    if not parsed.ready or (
                        parsed.ready and not parsed.publish_prd and not published
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
                    error_console.print(
                        f"[red]Failed to create Issue from {prd_path_text}:[/]"
                    )
                    error_console.print(error_detail, markup=False)

            if failed_prd_paths:
                logger.error(
                    "Issue creation failed for %d PRD(s): %s",
                    len(failed_prd_paths),
                    ", ".join(failed_prd_paths),
                )
                return 1
            return 0

        if parsed.command == "issue list":
            if parsed.with_pr and parsed.without_pr:
                logger.error("--with-pr and --without-pr are mutually exclusive.")
                return 1
            from backend.core.use_cases.issue_pr_status import (
                IAR_REPOSITORY_MARKER_FILENAME,
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
                return resolve_repository_targets(
                    runner_settings,
                    repo_id=repo_id,
                    repo_path_override=repo_path_override,
                    all_repositories=all_repositories,
                )

            request = IssueListRequest(
                repo_id=repo_id,
                repo_path_override=repo_override,
                all_repositories=getattr(parsed, "all_registered", False),
                state_filter=parsed.state,
                label_filter=parsed.label,
                with_pr=True
                if parsed.with_pr
                else (False if parsed.without_pr else None),
                limit=parsed.limit,
            )
            try:
                result = list_issues_with_prs(
                    request,
                    cwd=Path.cwd(),
                    github_client_factory=github_client_factory,
                    resolve_targets=_resolve_targets,
                    has_local_iar_repo=make_default_has_local_iar_repo(
                        marker_filename=IAR_REPOSITORY_MARKER_FILENAME
                    ),
                )
            except ValueError as exc:
                logger.error("%s", exc)
                error_console.print(f"[red]{exc}[/]")
                return 1
            render_console = Console()
            multi_repo = len({row.repo for row in result.rows if row.repo}) > 1
            output_format = parsed.output
            if not result.rows and not result.errors:
                if output_format == "json":
                    sys.stdout.write("[]\n")
                else:
                    table = Table(show_header=True, header_style="bold")
                    if multi_repo:
                        table.add_column("repo")
                    table.add_column("#")
                    table.add_column("title")
                    table.add_column("labels")
                    table.add_column("state")
                    table.add_column("PRs")
                    render_console.print(table)
                    render_console.print("No issues match the filters.")
                return 0
            if output_format == "json":
                for row in result.rows:
                    sys.stdout.write(
                        json.dumps(render_issue_with_pulls_json(row)) + "\n"
                    )
            else:
                table = Table(show_header=True, header_style="bold")
                if multi_repo:
                    table.add_column("repo")
                table.add_column("#")
                table.add_column("title")
                table.add_column("labels")
                table.add_column("state")
                table.add_column("PRs")
                for row in result.rows:
                    cells = [
                        f"#{row.number}",
                        row.title,
                        ", ".join(row.labels) if row.labels else "—",
                        row.state,
                        render_pr_column(row.pulls),
                    ]
                    if multi_repo:
                        cells.insert(0, row.repo or "")
                    table.add_row(*cells)
                render_console.print(table)
            for repo_label, error_message in result.errors:
                error_console.print(
                    f"[red]Error fetching {repo_label}:[/] {error_message}"
                )
            return 1 if result.errors else 0

        if parsed.command == "run":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            for context in contexts:
                require_iar_repository_initialized(context.repo_path, process_runner)
            if contexts:
                _ensure_gh_auth_or_prompt(contexts[0].repo_path, process_runner)
            content_generator = create_content_generator(process_runner)

            def transcript_runner_factory(repo_path: Path) -> object:
                return create_transcript_runner(process_runner)

            return run_agent_repositories_once(
                contexts=contexts,
                dry_run=parsed.dry_run,
                agent=parsed.agent,
                max_issues=parsed.max_issues or runner_settings.runner.max_issues,
                process_runner=process_runner,
                github_client_factory=github_client_factory,
                content_generator=content_generator,
                run_history_store=_create_run_history_store_or_none(),
                run_trigger=_resolve_run_trigger("run"),
                max_prd_issues=1,
                transcript_runner_factory=transcript_runner_factory,
                max_deliberation_issues=runner_settings.daemon.max_deliberation_issues,
            )

        if parsed.command == "daemon":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            for context in contexts:
                require_iar_repository_initialized(context.repo_path, process_runner)
            if contexts:
                _ensure_gh_auth_or_prompt(contexts[0].repo_path, process_runner)
            interval = (
                parsed.interval
                if parsed.interval is not None
                else runner_settings.daemon.run_interval_seconds
            )

            def content_generator_factory(repo_path: Path):
                return create_content_generator(process_runner)

            def transcript_runner_factory(repo_path: Path) -> object:
                return create_transcript_runner(process_runner)

            run_agent_daemon(
                contexts=contexts,
                interval=interval,
                agent=parsed.agent,
                max_issues=parsed.max_issues or runner_settings.runner.max_issues,
                process_runner=process_runner,
                github_client_factory=github_client_factory,
                content_generator_factory=content_generator_factory,
                run_history_store=_create_run_history_store_or_none(),
                run_trigger=_resolve_run_trigger("daemon"),
                max_prd_issues=1,
                transcript_runner_factory=transcript_runner_factory,
                max_deliberation_issues=runner_settings.daemon.max_deliberation_issues,
            )
            return 0

        if parsed.command == "review":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            for context in contexts:
                require_iar_repository_initialized(context.repo_path, process_runner)
            if contexts:
                _ensure_gh_auth_or_prompt(contexts[0].repo_path, process_runner)
            aggregated_exit_code = 0
            for context in contexts:
                github_client = github_client_factory(context.repo_path)
                try:
                    repo_exit_code = review_once(
                        repo_path=context.repo_path,
                        config=context.config,
                        dry_run=parsed.dry_run,
                        agent=parsed.agent,
                        max_issues=parsed.max_issues
                        or runner_settings.runner.max_issues,
                        github_client=github_client,
                        process_runner=process_runner,
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

        if parsed.command == "review-daemon":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            for context in contexts:
                require_iar_repository_initialized(context.repo_path, process_runner)
            if contexts:
                _ensure_gh_auth_or_prompt(contexts[0].repo_path, process_runner)
            interval = (
                parsed.interval
                if parsed.interval is not None
                else runner_settings.daemon.review_interval_seconds
            )
            run_review_daemon(
                contexts=contexts,
                interval=interval,
                agent=parsed.agent,
                max_issues=parsed.max_issues or runner_settings.runner.max_issues,
                process_runner=process_runner,
                github_client_factory=github_client_factory,
            )
            return 0

        if parsed.command == "recover":
            from backend.core.use_cases.recover_publish import (
                PublishRecoveryError,
                PublishRecoveryRequest,
                recover_publish_issue,
            )

            contexts = resolve_repository_targets(
                runner_settings,
                repo_id=repo_id,
                repo_path_override=repo_override,
            )
            for context in contexts:
                require_iar_repository_initialized(context.repo_path, process_runner)
            if len(contexts) != 1:
                logger.error(
                    "recover requires exactly one target repository. "
                    "Use --repo or --repo-id to specify."
                )
                return 1
            context = contexts[0]
            github_client = create_github_client(context.repo_path, process_runner)
            request = PublishRecoveryRequest(
                issue_number=parsed.issue,
                expected_branch=parsed.branch,
            )
            try:
                result = recover_publish_issue(
                    request=request,
                    repo_path=context.repo_path,
                    config=context.config,
                    github_client=github_client,
                    process_runner=process_runner,
                )
                logger.info(
                    "Publish recovered for Issue #%d: %s",
                    result.issue_number,
                    result.pr_url,
                )
                console.print(
                    f"[green]Publish recovered for Issue "
                    f"#{result.issue_number}:[/] {result.pr_url}"
                )
                return 0
            except PublishRecoveryError as exc:
                logger.error(
                    "Publish recovery failed (category=%s): %s",
                    exc.failure_category,
                    exc,
                )
                return 1

        if parsed.command == "blocked-continue":
            from backend.core.use_cases.blocked_continue import (
                BlockedContinueError,
                blocked_continue_issue,
            )

            contexts = resolve_repository_targets(
                runner_settings,
                repo_id=repo_id,
                repo_path_override=repo_override,
            )
            for context in contexts:
                require_iar_repository_initialized(context.repo_path, process_runner)
            if len(contexts) != 1:
                logger.error(
                    "blocked-continue requires exactly one target repository. "
                    "Use --repo or --repo-id to specify."
                )
                return 1
            context = contexts[0]
            github_client = create_github_client(context.repo_path, process_runner)
            try:
                claimed = blocked_continue_issue(
                    issue_number=parsed.issue,
                    repo_path=context.repo_path,
                    config=context.config,
                    agent=parsed.agent,
                    github_client=github_client,
                    process_runner=process_runner,
                )
                if claimed:
                    console.print(
                        f"[green]Issue #{parsed.issue} resumed successfully.[/]"
                    )
                    return 0
                console.print(
                    f"[yellow]Issue #{parsed.issue} was claimed by another runner.[/]"
                )
                return 0
            except BlockedContinueError as exc:
                logger.error("blocked-continue failed: %s", exc)
                error_console.print(f"[red]blocked-continue failed:[/] {exc}")
                return 1

        if parsed.command == "ask":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            for context in contexts:
                require_iar_repository_initialized(context.repo_path, process_runner)
            if len(contexts) != 1:
                logger.error(
                    "ask requires exactly one target repository. "
                    "Use --repo or --repo-id to specify."
                )
                return 1
            context = contexts[0]
            _ensure_gh_auth_or_prompt(context.repo_path, process_runner)
            github_client = create_github_client(context.repo_path, process_runner)
            planner_runner = create_planner_runner(process_runner)
            content_generator = create_content_generator(process_runner)
            agent = parsed.agent
            if agent == "auto":
                agent = context.config.interactive_decision.default_agent
            output_dir = None
            if parsed.output:
                output_dir = Path(parsed.output)
            deliberation_config = context.config.deliberation
            transcript_runner = create_transcript_runner(process_runner)
            output_view = create_output_view()
            event_sink = create_event_sink(
                Path(context.config.interactive_decision.default_output_dir),
                output_view,
            )
            return run_interactive_decision(
                user_prompt=parsed.prompt,
                context=context,
                config=context.config.interactive_decision,
                agent=agent,
                plan_only=parsed.plan_only,
                execute=parsed.execute,
                auto_confirm=parsed.yes,
                output_dir=output_dir,
                planner_runner=planner_runner,
                github_client=github_client,
                process_runner=process_runner,
                content_generator=content_generator,
                github_client_factory=github_client_factory,
                deliberation_deps={
                    "config": deliberation_config,
                    "transcript_runner": transcript_runner,
                    "event_sink": event_sink,
                    "output_view": output_view,
                },
            )

        if parsed.command == "deliberate":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            if len(contexts) != 1:
                logger.error(
                    "deliberate requires exactly one target repository. "
                    "Use --repo or --repo-id to specify."
                )
                return 1
            context = contexts[0]
            require_iar_repository_initialized(context.repo_path, process_runner)
            deliberation_settings = context.config.deliberation
            output_dir = parsed.output or deliberation_settings.default_output_dir
            rounds = (
                parsed.rounds
                if parsed.rounds is not None
                else deliberation_settings.default_rounds
            )
            synthesizer = (
                parsed.synthesizer or deliberation_settings.default_synthesizer
            )
            agents = tuple(a.strip() for a in parsed.agents.split(",") if a.strip())
            session_id = parsed.session_id or create_default_session_id()
            output_path = Path(output_dir) / session_id
            request = DeliberationRequest(
                prompt=parsed.prompt,
                agents=agents,
                rounds=rounds,
                synthesizer=synthesizer,
                output_dir=str(output_path),
                session_id=session_id,
            )
            deliberation_config = context.config.deliberation
            transcript_runner = create_transcript_runner(process_runner)
            output_path.mkdir(parents=True, exist_ok=True)
            output_view = create_output_view()
            event_sink = create_event_sink(output_path, output_view)
            resolver = AgentFailureResolver()
            result = run_agent_deliberation(
                request=request,
                config=deliberation_config,
                transcript_runner=transcript_runner,
                event_sink=event_sink,
                target_repo_path=context.repo_path,
                output_view=output_view,
                resolver=resolver.resolve,
            )
            selected_profile_ids = tuple(
                dict.fromkeys(
                    profile_id
                    for outputs in result.agent_outputs.values()
                    for profile_id in outputs
                )
            )
            profiles_by_id = {
                profile.profile_id: profile for profile in deliberation_config.profiles
            }
            session_profiles = tuple(
                profiles_by_id[profile_id]
                for profile_id in selected_profile_ids
                if profile_id in profiles_by_id
            )
            session = DeliberationSession(
                session_id=result.session_id,
                prompt=result.prompt,
                profiles=session_profiles,
                rounds=request.rounds,
                synthesizer=request.synthesizer,
                output_dir=output_path,
                started_at=result.started_at,
                finished_at=result.finished_at,
            )
            write_deliberation_outputs(result, session, output_path)
            console.print(f"\n[green]Deliberation complete:[/] {output_path}")
            if result.failed_agents:
                for failure in result.failed_agents:
                    logger.warning(
                        "Deliberation agent failed: profile=%s attempted=%s "
                        "fallback=%s reason=%s",
                        failure.profile_id,
                        failure.attempted_agent,
                        failure.fallback_agent,
                        failure.reason,
                    )
                    console.print(
                        f"[yellow]Agent '{failure.profile_id}' failed "
                        f"(attempted={failure.attempted_agent}, "
                        f"fallback={failure.fallback_agent or 'none'}, "
                        f"reason={failure.reason}).[/]"
                    )
            strict_mode = (
                parsed.strict or not deliberation_config.continue_on_agent_error
            )
            all_participants_failed = len(selected_profile_ids) > 0 and all(
                profile_id in {f.profile_id for f in result.failed_agents}
                for profile_id in selected_profile_ids
            )
            synthesizer_failed = any(
                failure.profile_id == "synthesizer" for failure in result.failed_agents
            )
            if strict_mode and result.failed_agents:
                return 1
            if all_participants_failed:
                return 1
            if synthesizer_failed and not any(
                failure.profile_id != "synthesizer" for failure in result.failed_agents
            ):
                return 1
            return 0
    except IARRepositoryNotInitializedError as exc:
        return _handle_not_initialized_error(exc)
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
        error_detail = _format_cli_exception(exc)
        logger.error("iar failed:\n%s", error_detail)
        error_console.print("[red]iar failed:[/]")
        error_console.print(error_detail, markup=False)
        return 1
    return 1


def main(argv: list[str] | None = None) -> int:
    """Run the Typer-powered CLI."""
    from backend.api.cli_typer import main as typer_main

    return typer_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
