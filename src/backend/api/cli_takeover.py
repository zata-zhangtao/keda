"""Implementation of the ``iar takeover`` CLI command."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from backend.api.cli_console import console, error_console
from backend.core.shared.interfaces.runner_console import RunnerProcessKind
from backend.core.use_cases.console_processes import start_runner_process
from backend.engines.agent_runner.factory import (
    create_github_client,
    create_process_supervisor,
    create_registry_editor,
    load_fresh_agent_runner_settings,
    logger,
    resolve_registry_config_toml_path,
    resolve_repository_targets_with_diagnostics,
)
from backend.engines.agent_runner.takeover import (
    build_takeover_options,
    execute_takeover,
    filter_unregistered_candidates,
    list_github_repositories,
    parse_selected_repositories,
)
from backend.engines.agent_runner.takeover_interactive import (
    select_repositories_interactive,
)

if TYPE_CHECKING:
    from backend.core.shared.interfaces.agent_runner import IProcessRunner


def _start_daemons_for_repo(repo_id: str, _repo_path: Path) -> None:
    """Start managed daemon and review-daemon for a freshly registered repo.

    The daemon is spawned from the directory containing the registry
    ``config.toml`` so the subprocess resolves the same global registry that was
    just updated by ``takeover``.
    """
    settings = load_fresh_agent_runner_settings()
    contexts, _failures = resolve_repository_targets_with_diagnostics(settings)
    supervisor = create_process_supervisor()
    spawn_cwd = resolve_registry_config_toml_path().parent
    runner_command = settings.console.runner_command
    for kind in (RunnerProcessKind.DAEMON, RunnerProcessKind.REVIEW_DAEMON):
        try:
            record = start_runner_process(
                repo_id=repo_id,
                kind=kind,
                contexts=contexts,
                supervisor=supervisor,
                runner_command=runner_command,
                spawn_cwd=spawn_cwd,
            )
            console.print(
                f"[green]Started {kind.value}[/] for {repo_id} "
                f"(process {record.process_id})"
            )
        except Exception as exc:  # noqa: BLE001 - daemon start is best effort.
            logger.warning("Failed to start %s for %s: %s", kind.value, repo_id, exc)
            error_console.print(
                f"[yellow]Failed to start {kind.value} for {repo_id}:[/] {exc}"
            )


def _run_takeover_command(
    parsed: argparse.Namespace, process_runner: IProcessRunner
) -> int:
    """Run the global repository takeover flow."""
    auth_client = create_github_client(Path.cwd(), process_runner)
    auth_status = auth_client.check_auth_status()
    if not auth_status.authenticated:
        error_console.print("[red]GitHub CLI 认证失败。[/]")
        if auth_status.failure_reason:
            error_console.print(f"[red]{auth_status.failure_reason}[/]")
        error_console.print("[yellow]请运行: gh auth login -h github.com[/]")
        return 1

    options = build_takeover_options(
        clone_root=parsed.clone_root,
        owner=parsed.owner,
        limit=parsed.limit,
        selected_repos=tuple(getattr(parsed, "repos", []) or ()),
        start_daemons=getattr(parsed, "start_daemons", True),
        dry_run=parsed.dry_run,
    )
    editor = create_registry_editor()

    try:
        if options.selected_repos:
            candidates = parse_selected_repositories(options.selected_repos)
        else:
            candidates = list_github_repositories(
                owner=options.owner,
                limit=options.limit,
                process_runner=process_runner,
            )
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
        logger.error("iar takeover failed to list repositories: %s", exc)
        error_console.print(f"[red]Failed to list repositories:[/] {exc}")
        return 1

    candidates = filter_unregistered_candidates(candidates, editor, options.clone_root)

    if not options.selected_repos:
        candidates = select_repositories_interactive(candidates, console=console)
        if not candidates:
            console.print("[yellow]No repositories selected. Aborting.[/]")
            return 0
        console.print(
            "Taking over: "
            + ", ".join(f"[cyan]{candidate.full_name}[/]" for candidate in candidates)
        )

    def _print_takeover_progress(full_name: str, stage: str) -> None:
        stage_labels = {
            "clone": "Cloned",
            "init": "Initialized",
            "register": "Registered",
            "start_daemons": "Started daemons",
            "complete": "Complete",
        }
        label = stage_labels.get(stage, stage)
        console.print(f"  [dim]{label}[/] {full_name}")

    try:
        result = execute_takeover(
            options=options,
            candidates=candidates,
            editor=editor,
            process_runner=process_runner,
            start_daemon_callback=_start_daemons_for_repo
            if options.start_daemons
            else None,
            progress_callback=_print_takeover_progress,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
        logger.error("iar takeover failed: %s", exc)
        error_console.print(f"[red]Takeover failed:[/] {exc}")
        return 1

    console.print(
        f"\n[green]Takeover complete:[/] {result.succeeded}/{result.attempted} succeeded"
    )
    if result.started_daemons:
        console.print(f"  Started {result.started_daemons} daemon(s)")
    if result.started_review_daemons:
        console.print(f"  Started {result.started_review_daemons} review-daemon(s)")
    for repo_result in result.repositories:
        status = "[green]OK[/]" if repo_result.error is None else "[red]FAILED[/]"
        console.print(f"  {status} {repo_result.full_name} -> {repo_result.repo_path}")
        if repo_result.error:
            console.print(f"    [red]{repo_result.error}[/]")
    return 0 if result.succeeded == result.attempted else 1
