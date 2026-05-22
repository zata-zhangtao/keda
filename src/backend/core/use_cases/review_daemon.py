"""Post-PR review daemon — continuous polling across all targets."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import RepositoryRunContext
from backend.core.use_cases.review_once import review_once

_logger = logging.getLogger(__name__)


def run_review_daemon(
    *,
    contexts: list[RepositoryRunContext],
    interval: int,
    agent: str,
    max_issues: int,
    process_runner: IProcessRunner,
    github_client_factory: Callable[[Path], IGitHubClient],
) -> None:
    """Run the review poller forever across all target repositories.

    Args:
        contexts: Resolved repository targets with merged configurations.
        interval: Seconds between polling passes.
        agent: Agent override (auto, codex, claude).
        max_issues: Maximum issues to process per pass per repository.
        process_runner: Runner for executing subprocess commands.
        github_client_factory: Factory that creates an IGitHubClient for a repo path.
    """
    while True:
        for context in contexts:
            _logger.info(
                "Review daemon pass for repository '%s' (%s).",
                context.repo_id,
                context.display_name,
            )
            github_client = github_client_factory(context.repo_path)
            try:
                review_once(
                    repo_path=context.repo_path,
                    config=context.config,
                    dry_run=False,
                    agent=agent,
                    max_issues=max_issues,
                    github_client=github_client,
                    process_runner=process_runner,
                )
            except Exception as exc:  # noqa: BLE001 - daemon should survive unexpected errors.
                _logger.error(
                    "Review daemon pass failed for repository '%s': %s",
                    context.repo_id,
                    exc,
                )
        _logger.info("Sleeping for %d seconds before next review poll.", interval)
        time.sleep(interval)
