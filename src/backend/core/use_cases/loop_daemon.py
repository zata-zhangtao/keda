"""Loop scheduling daemon.

A long-running process that polls the loop state store on a configurable
interval, fires any loops that are due, and updates the next fire time.
The implementation mirrors the survival pattern of
:mod:`backend.core.use_cases.run_agent_daemon`: an exception in any one
fire is logged but does not stop the daemon.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.interfaces.loop_scheduler import (
    ILoopClock,
    ILoopStateStore,
)
from backend.core.shared.models.agent_runner import LabelConfig
from backend.core.shared.models.loop import LoopTask
from backend.core.use_cases.loop_fire import fire_loop
from backend.core.use_cases.loop_scheduler import list_due_tasks

_logger = logging.getLogger(__name__)


def _run_daemon_tick(
    *,
    state_store: ILoopStateStore,
    github_client_factory: Callable[[Path], IGitHubClient],
    process_runner: IProcessRunner,
    clock: ILoopClock,
    content_generator_factory: Callable[[Path], IContentGenerator | None] | None = None,
    labels_config: LabelConfig | None = None,
    dry_run: bool = False,
    repo_resolver: Callable[[LoopTask], Path],
) -> list[tuple[str, str]]:
    """Run a single daemon pass and fire all due tasks.

    Args:
        state_store: Loop state store.
        github_client_factory: Factory producing an :class:`IGitHubClient`
            for a given repository path.
        process_runner: Process runner used for pre-commands and publishing.
        clock: Wall-clock abstraction.
        content_generator_factory: Optional factory for content generators.
        labels_config: Label config forwarded to fire_loop.
        dry_run: When True, only render PRDs and report what would happen.
        repo_resolver: Resolves the repository path for a loop task.

    Returns:
        A list of ``(loop_id, status)`` pairs reporting the outcome of
        each fire attempted in this pass.
    """
    state_store.load()
    tasks = state_store.list_tasks()
    due_tasks = list_due_tasks(tasks, clock=clock)
    if not due_tasks:
        return []
    outcomes: list[tuple[str, str]] = []
    for task in due_tasks:
        repo_path = repo_resolver(task)
        github_client = github_client_factory(repo_path)
        content_generator = (
            content_generator_factory(repo_path)
            if content_generator_factory is not None
            else None
        )
        try:
            result = fire_loop(
                task,
                repo_path=repo_path,
                github_client=github_client,
                process_runner=process_runner,
                state_store=state_store,
                clock=clock,
                content_generator=content_generator,
                labels_config=labels_config,
                dry_run=dry_run,
            )
            outcomes.append((task.id, result.status.value))
        except Exception as exc:  # noqa: BLE001 - daemon must survive.
            _logger.error(
                "Loop '%s' fire failed and will be retried next pass: %s",
                task.id,
                exc,
            )
            outcomes.append((task.id, f"error: {exc}"))
    return outcomes


def run_loop_daemon(
    *,
    state_store: ILoopStateStore,
    github_client_factory: Callable[[Path], IGitHubClient],
    process_runner: IProcessRunner,
    clock: ILoopClock,
    repo_resolver: Callable[[LoopTask], Path],
    interval: int,
    content_generator_factory: Callable[[Path], IContentGenerator | None] | None = None,
    labels_config: LabelConfig | None = None,
    dry_run: bool = False,
    max_passes: int | None = None,
) -> None:
    """Run the loop scheduling daemon until interrupted.

    Args:
        state_store: Loop state store.
        github_client_factory: Factory producing an :class:`IGitHubClient`
            for a given repository path.
        process_runner: Process runner.
        clock: Wall-clock abstraction.
        repo_resolver: Resolves the repository path for a loop task.
        interval: Seconds between polling passes.
        content_generator_factory: Optional factory for content generators.
        labels_config: Label config forwarded to fire_loop.
        dry_run: When True, never write to disk or call GitHub.
        max_passes: Stop after this many passes (used by tests and the
            ``--once`` integration path). ``None`` means run forever.
    """
    if interval <= 0:
        raise ValueError("Daemon interval must be positive.")
    passes_completed = 0
    while True:
        outcomes = _run_daemon_tick(
            state_store=state_store,
            github_client_factory=github_client_factory,
            process_runner=process_runner,
            clock=clock,
            content_generator_factory=content_generator_factory,
            labels_config=labels_config,
            dry_run=dry_run,
            repo_resolver=repo_resolver,
        )
        if outcomes:
            _logger.info(
                "Loop daemon pass: %s", ", ".join(f"{i}={s}" for i, s in outcomes)
            )
        passes_completed += 1
        if max_passes is not None and passes_completed >= max_passes:
            return
        if dry_run and max_passes is None:
            # In dry-run mode, only inspect once so the operator gets a
            # quick sanity-check before falling back to running the
            # real daemon.
            return
        _logger.info("Loop daemon sleeping for %d seconds.", interval)
        clock.sleep_seconds(interval)
