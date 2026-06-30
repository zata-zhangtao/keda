"""Local Issue queue runner — daemon mode."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IAgentTranscriptRunner,
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.interfaces.runner_console import IRunHistoryStore
from backend.core.shared.interfaces.runner_live_view import IRunnerLiveView
from backend.core.shared.models.agent_runner import RepositoryRunContext
from backend.core.use_cases.agent_runner_orchestrate import (
    process_prd_rework_issues,
    run_once,
)
from backend.core.use_cases.agent_runner_reclaim import (
    reclaim_stale_running_issues,
)

_logger = logging.getLogger(__name__)


def run_agent_daemon(
    *,
    contexts: list[RepositoryRunContext],
    interval: int,
    agent: str,
    max_issues: int,
    process_runner: IProcessRunner,
    github_client_factory: Callable[[Path], IGitHubClient],
    content_generator_factory: Callable[[Path], IContentGenerator] | None = None,
    run_history_store: IRunHistoryStore | None = None,
    run_trigger: str = "cli_daemon",
    max_prd_issues: int = 1,
    transcript_runner_factory: Callable[[Path], IAgentTranscriptRunner] | None = None,
    max_deliberation_issues: int = 1,
    concurrency: int = 1,
    output_view: IRunnerLiveView | None = None,
    reclaim_stale_running: bool = False,
) -> None:
    """Run the queue poller forever across all target repositories.

    Args:
        contexts: Resolved repository targets with merged configurations.
        interval: Seconds between polling passes.
        agent: Agent override (auto, codex, claude).
        max_issues: Maximum issues to process per pass per repository.
        process_runner: Runner for executing subprocess commands.
        github_client_factory: Factory that creates an IGitHubClient for a repo path.
        content_generator_factory: Optional factory that creates an IContentGenerator
            for a repo path. When omitted, PRD rework uses template/fallback mode.
        run_history_store: Optional side-channel run history store.
        run_trigger: Trigger source recorded with each run record.
        max_prd_issues: Maximum rework-prd issues to process per pass per repository.
        transcript_runner_factory: Optional factory that returns an
            :class:`IAgentTranscriptRunner` for a repo path. When omitted, the
            Phase 0 deliberation queue is skipped entirely (zero regression for
            callers that do not assemble a runner).
        max_deliberation_issues: Maximum ``agent/deliberate`` Issues to process
            per Phase 0 pass. Defaults to 1 to bound multi-agent cost.
        concurrency: Issues processed in parallel within each repository's
            Phase 2 pass. ``1`` keeps the sequential path (zero regression).
        output_view: Optional live view for parallel runs; each Issue's agent
            output goes to its own panel. ``None`` shows no dashboard (per-Issue
            log files are still written).
    """
    while True:
        for context in contexts:
            _logger.info(
                "Daemon pass for repository '%s' (%s).",
                context.repo_id,
                context.display_name,
            )
            github_client = github_client_factory(context.repo_path)
            content_generator = (
                content_generator_factory(context.repo_path)
                if content_generator_factory is not None
                else None
            )

            # Phase -1: reclaim Issues stuck at agent/running because their
            # runner process died (hard kill / crash). Conservative — only
            # same-host, provably-dead PIDs — so it never disturbs a live run.
            # Reclaimed Issues become agent/ready and are picked up in Phase 2.
            if reclaim_stale_running:
                try:
                    reclaimed = reclaim_stale_running_issues(
                        config=context.config, github_client=github_client
                    )
                    if reclaimed:
                        _logger.info(
                            "Reclaimed %d stale agent/running Issue(s) for '%s': %s",
                            len(reclaimed),
                            context.repo_id,
                            reclaimed,
                        )
                except Exception as exc:  # noqa: BLE001 - daemon must survive reclaim faults.
                    _logger.error(
                        "Stale-running reclaim failed for repository '%s': %s",
                        context.repo_id,
                        exc,
                    )

            # Phase 0: Asynchronous Issue-comment deliberation on Issues that
            # explicitly opt in via the ``agent/deliberate`` label. Skipped
            # when no transcript runner factory was injected so existing
            # callers (tests, ad-hoc scripts) keep their previous behaviour.
            if transcript_runner_factory is not None:
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
                except Exception as exc:  # noqa: BLE001 - daemon must survive Phase 0 faults.
                    _logger.error("Deliberation phase failed: %s", exc)

            try:
                # Phase 1: PRD rework before normal ready-issue execution.
                process_prd_rework_issues(
                    repo_path=context.repo_path,
                    config=context.config,
                    github_client=github_client,
                    process_runner=process_runner,
                    content_generator=content_generator,
                    max_issues=max_prd_issues,
                )
            except Exception as exc:  # noqa: BLE001 - daemon should survive unexpected errors.
                _logger.error("PRD rework phase failed: %s", exc)

            try:
                # Phase 2: Ready issue execution.
                run_once(
                    repo_path=context.repo_path,
                    config=context.config,
                    dry_run=False,
                    agent=agent,
                    max_issues=max_issues,
                    github_client=github_client,
                    process_runner=process_runner,
                    content_generator=content_generator,
                    run_history_store=run_history_store,
                    run_trigger=run_trigger,
                    repo_id=context.repo_id,
                    concurrency=concurrency,
                    output_view=output_view,
                )
            except Exception as exc:  # noqa: BLE001 - daemon should survive unexpected errors.
                _logger.error(
                    "Daemon pass failed for repository '%s': %s",
                    context.repo_id,
                    exc,
                )
        _logger.info("Sleeping for %d seconds before next poll.", interval)
        time.sleep(interval)
