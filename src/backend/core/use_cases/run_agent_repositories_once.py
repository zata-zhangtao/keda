"""Multi-repository runner — single polling pass across all targets."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.interfaces.runner_console import IRunHistoryStore
from backend.core.shared.models.agent_runner import RepositoryRunContext
from backend.core.use_cases.agent_runner_orchestrate import run_once

_logger = logging.getLogger(__name__)


def run_agent_repositories_once(
    *,
    contexts: list[RepositoryRunContext],
    dry_run: bool,
    agent: str,
    max_issues: int,
    process_runner: IProcessRunner,
    github_client_factory: Callable[[Path], IGitHubClient],
    content_generator: IContentGenerator | None = None,
    run_history_store: IRunHistoryStore | None = None,
    run_trigger: str = "cli_run",
    max_prd_issues: int = 1,
) -> int:
    """Run one polling pass across all target repositories.

    Args:
        contexts: Resolved repository targets with merged configurations.
        dry_run: If True, only list ready issues without processing.
        agent: Agent override (auto, codex, claude).
        max_issues: Maximum issues to process per repository.
        process_runner: Runner for executing subprocess commands.
        github_client_factory: Factory that creates an IGitHubClient for a repo path.
        content_generator: Optional content generator for AI-generated PR content.
        run_history_store: Optional side-channel run history store.
        run_trigger: Trigger source recorded with each run record.
        max_prd_issues: Maximum rework-prd issues to process per repository.

    Returns:
        Exit code (0 on success, 1 if any repository failed).
    """
    from backend.core.use_cases.agent_runner_orchestrate import (
        process_prd_rework_issues,
    )

    aggregated_exit_code = 0
    for context in contexts:
        _logger.info(
            "Running once for repository '%s' (%s).",
            context.repo_id,
            context.display_name,
        )
        github_client = github_client_factory(context.repo_path)
        if not dry_run:
            try:
                process_prd_rework_issues(
                    repo_path=context.repo_path,
                    config=context.config,
                    github_client=github_client,
                    process_runner=process_runner,
                    content_generator=content_generator,
                    max_issues=max_prd_issues,
                )
            except Exception as exc:  # noqa: BLE001 - isolate PRD rework failures.
                _logger.error(
                    "PRD rework phase failed for repository '%s': %s",
                    context.repo_id,
                    exc,
                )
        try:
            repo_exit_code = run_once(
                repo_path=context.repo_path,
                config=context.config,
                dry_run=dry_run,
                agent=agent,
                max_issues=max_issues,
                github_client=github_client,
                process_runner=process_runner,
                content_generator=content_generator,
                run_history_store=run_history_store,
                run_trigger=run_trigger,
                repo_id=context.repo_id,
            )
            if repo_exit_code != 0:
                aggregated_exit_code = 1
        except Exception as exc:  # noqa: BLE001 - isolate per-repo failures.
            aggregated_exit_code = 1
            _logger.error(
                "Repository '%s' run_once failed: %s",
                context.repo_id,
                exc,
            )
    return aggregated_exit_code
