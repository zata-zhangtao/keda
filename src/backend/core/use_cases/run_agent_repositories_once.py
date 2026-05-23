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

    Returns:
        Exit code (0 on success, 1 if any repository failed).
    """
    aggregated_exit_code = 0
    for context in contexts:
        _logger.info(
            "Running once for repository '%s' (%s).",
            context.repo_id,
            context.display_name,
        )
        github_client = github_client_factory(context.repo_path)
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
