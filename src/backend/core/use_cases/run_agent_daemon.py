"""Local Issue queue runner — daemon mode."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.interfaces.runner_console import IRunHistoryStore
from backend.core.shared.models.agent_runner import RepositoryRunContext
from backend.core.use_cases.agent_runner_orchestrate import run_once

_logger = logging.getLogger(__name__)


def run_agent_daemon(
    *,
    contexts: list[RepositoryRunContext],
    interval: int,
    agent: str,
    max_issues: int,
    process_runner: IProcessRunner,
    github_client_factory: Callable[[Path], IGitHubClient],
    run_history_store: IRunHistoryStore | None = None,
    run_trigger: str = "cli_daemon",
) -> None:
    """Run the queue poller forever across all target repositories.

    Args:
        contexts: Resolved repository targets with merged configurations.
        interval: Seconds between polling passes.
        agent: Agent override (auto, codex, claude).
        max_issues: Maximum issues to process per pass per repository.
        process_runner: Runner for executing subprocess commands.
        github_client_factory: Factory that creates an IGitHubClient for a repo path.
        run_history_store: Optional side-channel run history store.
        run_trigger: Trigger source recorded with each run record.
    """
    while True:
        for context in contexts:
            _logger.info(
                "Daemon pass for repository '%s' (%s).",
                context.repo_id,
                context.display_name,
            )
            github_client = github_client_factory(context.repo_path)
            try:
                run_once(
                    repo_path=context.repo_path,
                    config=context.config,
                    dry_run=False,
                    agent=agent,
                    max_issues=max_issues,
                    github_client=github_client,
                    process_runner=process_runner,
                    run_history_store=run_history_store,
                    run_trigger=run_trigger,
                    repo_id=context.repo_id,
                )
            except Exception as exc:  # noqa: BLE001 - daemon should survive unexpected errors.
                _logger.error(
                    "Daemon pass failed for repository '%s': %s",
                    context.repo_id,
                    exc,
                )
        _logger.info("Sleeping for %d seconds before next poll.", interval)
        time.sleep(interval)
