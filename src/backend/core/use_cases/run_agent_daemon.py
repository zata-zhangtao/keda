"""Local Issue queue runner — daemon mode."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import AppConfig
from backend.core.use_cases.run_agent_once import run_once

_logger = logging.getLogger(__name__)


def run_agent_daemon(
    *,
    repo_path: Path,
    config: AppConfig,
    interval: int,
    agent: str,
    max_issues: int,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> None:
    """Run the queue poller forever.

    Args:
        repo_path: Target repository path.
        config: Application configuration.
        interval: Seconds between polling passes.
        agent: Agent override (auto, codex, claude).
        max_issues: Maximum issues to process per pass.
        github_client: Client for interacting with GitHub.
        process_runner: Runner for executing subprocess commands.
    """
    while True:
        try:
            run_once(
                repo_path=repo_path,
                config=config,
                dry_run=False,
                agent=agent,
                max_issues=max_issues,
                github_client=github_client,
                process_runner=process_runner,
            )
        except Exception as exc:  # noqa: BLE001 - daemon should survive unexpected errors.
            _logger.error("Daemon pass failed: %s", exc)
        _logger.info("Sleeping for %d seconds before next poll.", interval)
        time.sleep(interval)
