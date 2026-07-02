"""Multi-repository runner — single polling pass across all targets."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IAgentTranscriptRunner,
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.interfaces.runner_console import IRunHistoryStore
from backend.core.shared.models.agent_runner import RepositoryRunContext
from backend.core.use_cases.agent_runner_orchestrate import run_once

_logger = logging.getLogger(__name__)

# Hints in stderr/exception text that indicate a transient GitHub API/network
# failure rather than a logic error. Used to surface actionable recovery steps.
_TRANSIENT_NETWORK_ERROR_HINTS: tuple[str, ...] = (
    "api.github.com/graphql",
    'Post "https://api.github.com/graphql"',
    "connection reset by peer",
    "broken pipe",
    "Temporary failure in name resolution",
    "timeout",
    "EOF",
)

_RECOVERY_MESSAGE = (
    "Recovery: this appears to be a transient GitHub API network error; "
    "no issue labels were changed. Run `iar run` again to retry."
)


def _is_transient_network_error(exc: Exception) -> bool:
    """Return True when the exception looks like a transient network failure."""
    exc_text = f"{exc}"
    return any(
        hint.lower() in exc_text.lower() for hint in _TRANSIENT_NETWORK_ERROR_HINTS
    )


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
    transcript_runner_factory: Callable[[Path], IAgentTranscriptRunner] | None = None,
    max_deliberation_issues: int = 1,
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
        transcript_runner_factory: Optional factory that returns an
            :class:`IAgentTranscriptRunner` for a repo path. When omitted, the
            Phase 0 deliberation queue is skipped (zero regression for callers
            that do not assemble a runner).
        max_deliberation_issues: Maximum ``agent/deliberate`` Issues to process
            per Phase 0 pass.

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

        # Phase 0: Asynchronous Issue-comment deliberation on Issues opted in
        # via ``agent/deliberate``. Skipped when no transcript runner factory
        # was injected so existing callers (tests, ad-hoc scripts) keep their
        # previous behaviour.
        if not dry_run and transcript_runner_factory is not None:
            try:
                from backend.core.use_cases.agent_runner_deliberation_issues import (
                    process_deliberation_issues,
                )

                process_deliberation_issues(
                    repo_path=context.repo_path,
                    config=context.config,
                    github_client=github_client,
                    transcript_runner_factory=transcript_runner_factory,
                    max_issues=max_deliberation_issues,
                    stale_rounds_before_hint=context.config.deliberation.stale_rounds_before_hint,
                )
            except Exception as exc:  # noqa: BLE001 - isolate Phase 0 failures.
                _logger.error(
                    "Deliberation phase failed for repository '%s': %s",
                    context.repo_id,
                    exc,
                )

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
            if _is_transient_network_error(exc):
                _logger.error(
                    "Repository '%s' run_once failed: %s\n%s",
                    context.repo_id,
                    exc,
                    _RECOVERY_MESSAGE,
                )
            else:
                _logger.error(
                    "Repository '%s' run_once failed: %s",
                    context.repo_id,
                    exc,
                )
    return aggregated_exit_code
