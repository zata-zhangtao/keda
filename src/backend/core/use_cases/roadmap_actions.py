"""Roadmap start actions: single PRD and global scheduling.

Actions reuse existing Issue creation, label editing, and runner spawn
workflows so the roadmap layer never bypasses the iar state machine.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.interfaces.runner_console import (
    AuditEntry,
    IRoadmapStore,
    IRunnerProcessSupervisor,
    RoadmapQueueEntry,
    RunnerProcessKind,
)
from backend.core.shared.models.agent_runner import RepositoryRunContext
from backend.core.shared.models.roadmap import (
    RoadmapActionResult,
    RoadmapGlobalStartResult,
    RoadmapPrd,
    RoadmapPrdState,
    RoadmapSettingsEntry,
)
from backend.core.use_cases.console_processes import (
    ConsoleProcessError,
    start_runner_process,
)
from backend.core.use_cases.create_issue_from_prd import (
    IssueFromPrdRequest,
    create_issue_from_prd,
    parse_issue_number,
)
from backend.core.use_cases.roadmap_prd_scanner import scan_roadmap_prds
from backend.core.use_cases.roadmap_dependencies import evaluate_roadmap_dependencies
from backend.core.use_cases.roadmap_state_resolver import resolve_roadmap_states

_logger = logging.getLogger(__name__)


class RoadmapActionError(ValueError):
    """Roadmap action was rejected or failed."""


_DEFAULT_MAX_PARALLEL = 2


def _issue_type_from_filename(filename: str) -> str:
    """Map PRD filename tokens such as ``FEAT`` or ``BUG`` to Issue type labels."""
    match = re.search(r"P\d+-([A-Z]+)-", filename.upper())
    type_token = match.group(1) if match else "FEAT"
    mapping = {
        "FEAT": "feature",
        "BUG": "bug",
        "CHORE": "chore",
        "DOCS": "docs",
        "REFACTOR": "chore",
        "TEST": "chore",
    }
    return mapping.get(type_token, "feature")


def _now_iso() -> str:
    """Return current UTC time as ISO8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(
    store: IRoadmapStore,
    *,
    action: str,
    repo_id: str,
    prd_path: str,
    issue_number: int | None,
    result: str,
    detail: str,
) -> None:
    """Append a roadmap action audit entry."""
    try:
        store.append_audit(
            AuditEntry(
                occurred_at=_now_iso(),
                actor="roadmap",
                action=action,
                repo_id=repo_id,
                issue_number=issue_number,
                params_json=f'{{"prd_path": "{prd_path}"}}',
                result=result,
                detail=detail,
            )
        )
    except Exception as exc:  # noqa: BLE001 - audit must not break actions.
        _logger.warning("Failed to audit roadmap action %s: %s", action, exc)


def _create_issue_for_prd(
    prd: RoadmapPrd,
    context: RepositoryRunContext,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> int:
    """Create a GitHub Issue for a PRD and return its number.

    Uses the publish-safe path so the PRD is pushed to the base branch before
    ``agent/ready`` is added.
    """
    issue_type = _issue_type_from_filename(Path(prd.prd_path).name)
    request = IssueFromPrdRequest(
        repo_path=context.repo_path,
        prd_path=Path(prd.prd_path),
        issue_type=issue_type,
        queue_ready=True,
        publish_prd=True,
        git_remote=context.config.git.remote,
        git_base_branch=context.config.git.base_branch,
        generated_content_config=context.config.generated_content,
        labels_config=context.config.labels,
    )
    issue_url = create_issue_from_prd(
        request=request,
        github_client=github_client,
        process_runner=process_runner,
    )
    return parse_issue_number(issue_url)


def _ensure_ready_label(
    prd: RoadmapPrd,
    context: RepositoryRunContext,
    github_client: IGitHubClient,
) -> None:
    """Add ``agent/ready`` to an existing Issue, removing ``agent/failed``."""
    if prd.issue_number is None:
        raise RoadmapActionError(f"PRD {prd.prd_path} has no Issue to label.")
    labels_config = context.config.labels
    add_labels = [labels_config.ready]
    remove_labels = [labels_config.failed]
    github_client.edit_issue_labels(
        prd.issue_number,
        add=add_labels,
        remove=remove_labels,
    )


def _spawn_runner(
    repo_id: str,
    contexts: Sequence[RepositoryRunContext],
    supervisor: IRunnerProcessSupervisor,
    runner_command: Sequence[str],
    spawn_cwd: Path,
) -> None:
    """Spawn a one-shot runner for the repository."""
    start_runner_process(
        repo_id=repo_id,
        kind=RunnerProcessKind.RUN_ONCE,
        contexts=contexts,
        supervisor=supervisor,
        runner_command=runner_command,
        spawn_cwd=spawn_cwd,
    )


def start_prd(
    *,
    prd_path: str,
    repo_id: str,
    contexts: Sequence[RepositoryRunContext],
    github_client: IGitHubClient,
    supervisor: IRunnerProcessSupervisor,
    store: IRoadmapStore,
    runner_command: Sequence[str],
    spawn_cwd: Path,
    process_runner: IProcessRunner,
) -> RoadmapActionResult:
    """Start a single PRD: create Issue if needed, label it, spawn runner.

    Args:
        prd_path: Repository-relative PRD path.
        repo_id: Target repository ID.
        contexts: Resolved enabled repository contexts.
        github_client: GitHub client.
        supervisor: Process supervisor.
        store: Roadmap store for auditing.
        runner_command: Runner command prefix.
        spawn_cwd: Working directory for the runner subprocess.
        process_runner: Process runner for Git publishing commands.

    Returns:
        Action result with the new state.
    """
    context = _resolve_context(repo_id, contexts)
    prds = scan_roadmap_prds(context.repo_path, include_archived=False)
    prd = next((p for p in prds if p.prd_path == prd_path), None)
    if prd is None:
        raise RoadmapActionError(f"PRD not found or not pending: {prd_path}")

    if prd.issue_number is None:
        try:
            issue_number = _create_issue_for_prd(prd, context, github_client, process_runner)
        except Exception as exc:  # noqa: BLE001
            _audit(
                store,
                action="start_prd",
                repo_id=repo_id,
                prd_path=prd_path,
                issue_number=None,
                result="error",
                detail=str(exc),
            )
            raise RoadmapActionError(f"创建 Issue 失败: {exc}") from exc
    else:
        try:
            _ensure_ready_label(prd, context, github_client)
        except Exception as exc:  # noqa: BLE001
            _audit(
                store,
                action="start_prd",
                repo_id=repo_id,
                prd_path=prd_path,
                issue_number=prd.issue_number,
                result="error",
                detail=str(exc),
            )
            raise RoadmapActionError(f"添加 ready 标签失败: {exc}") from exc
        issue_number = prd.issue_number

    try:
        _spawn_runner(repo_id, contexts, supervisor, runner_command, spawn_cwd)
    except ConsoleProcessError as exc:
        _audit(
            store,
            action="start_prd",
            repo_id=repo_id,
            prd_path=prd_path,
            issue_number=issue_number,
            result="error",
            detail=str(exc),
        )
        raise RoadmapActionError(f"启动 runner 失败: {exc}") from exc

    _audit(
        store,
        action="start_prd",
        repo_id=repo_id,
        prd_path=prd_path,
        issue_number=issue_number,
        result="accepted",
        detail="Issue created/labelled and runner spawned.",
    )
    return RoadmapActionResult(
        prd_path=prd_path,
        issue_number=issue_number,
        state=RoadmapPrdState.READY,
        detail="PRD 已进入 ready 状态并启动 runner。",
    )


def _resolve_context(
    repo_id: str, contexts: Sequence[RepositoryRunContext]
) -> RepositoryRunContext:
    """Return the enabled repository context for ``repo_id``."""
    for context in contexts:
        if context.repo_id == repo_id:
            return context
    raise RoadmapActionError(f"Repository '{repo_id}' is not an enabled registry target.")


def get_or_create_roadmap_settings(store: IRoadmapStore, repo_id: str) -> RoadmapSettingsEntry:
    """Return existing settings or create defaults."""
    settings = store.get_roadmap_settings(repo_id)
    if settings is not None:
        return settings
    return RoadmapSettingsEntry(
        repo_id=repo_id,
        max_parallel=_DEFAULT_MAX_PARALLEL,
        default_view="list",
        updated_at=_now_iso(),
    )


def start_global_roadmap(
    *,
    repo_id: str,
    max_parallel: int,
    contexts: Sequence[RepositoryRunContext],
    github_client_factory: Callable[[Path], IGitHubClient],
    supervisor: IRunnerProcessSupervisor,
    store: IRoadmapStore,
    runner_command: Sequence[str],
    spawn_cwd: Path,
    process_runner: IProcessRunner,
) -> RoadmapGlobalStartResult:
    """Start up to ``max_parallel`` eligible pending PRDs.

    Args:
        repo_id: Target repository ID.
        max_parallel: Upper bound on concurrent running PRDs.
        contexts: Resolved enabled repository contexts.
        github_client_factory: Callable ``(repo_path) -> IGitHubClient``.
        supervisor: Process supervisor.
        store: Roadmap store.
        runner_command: Runner command prefix.
        spawn_cwd: Working directory for the runner subprocess.
        process_runner: Process runner for Git publishing commands.

    Returns:
        Summary of started, queued, and skipped PRDs.
    """
    if max_parallel < 1:
        raise RoadmapActionError("并发数必须 >= 1")

    context = _resolve_context(repo_id, contexts)
    github_client = github_client_factory(context.repo_path)
    prds = scan_roadmap_prds(context.repo_path, include_archived=False)
    block_reasons = evaluate_roadmap_dependencies(
        prds,
        github_client=github_client,
        labels_config=context.config.labels,
    )
    resolved_prds = resolve_roadmap_states(
        prds,
        github_client=github_client,
        config=context.config,
        block_reasons=block_reasons,
    )

    # Persist settings.
    settings = RoadmapSettingsEntry(
        repo_id=repo_id,
        max_parallel=max_parallel,
        default_view="list",
        updated_at=_now_iso(),
    )
    store.save_roadmap_settings(settings)

    # Determine how many slots are free.
    running_count = sum(1 for p in resolved_prds if p.state == RoadmapPrdState.RUNNING)
    free_slots = max(0, max_parallel - running_count)

    # Eligible PRDs: not started and not blocked/merged/running.
    eligible = [
        p for p in resolved_prds if p.state == RoadmapPrdState.NOT_STARTED and not p.block_reason
    ]
    # Sort by priority (P0 first) and then updated_at descending.
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    eligible.sort(
        key=lambda p: (
            priority_order.get(p.priority, 99),
            p.updated_at,
        )
    )

    started: list[RoadmapActionResult] = []
    queued: list[str] = []
    skipped: list[str] = []

    for prd in eligible:
        if len(started) < free_slots:
            try:
                result = start_prd(
                    prd_path=prd.prd_path,
                    repo_id=repo_id,
                    contexts=contexts,
                    github_client=github_client,
                    supervisor=supervisor,
                    store=store,
                    runner_command=runner_command,
                    spawn_cwd=spawn_cwd,
                    process_runner=process_runner,
                )
                started.append(result)
                store.enqueue_roadmap(
                    RoadmapQueueEntry(
                        repo_id=repo_id,
                        prd_path=prd.prd_path,
                        status="running",
                        trigger="global",
                        started_at=_now_iso(),
                        finished_at=None,
                        error_detail=None,
                    )
                )
            except RoadmapActionError as exc:
                skipped.append(f"{prd.prd_path}: {exc}")
                store.enqueue_roadmap(
                    RoadmapQueueEntry(
                        repo_id=repo_id,
                        prd_path=prd.prd_path,
                        status="failed",
                        trigger="global",
                        started_at=_now_iso(),
                        finished_at=_now_iso(),
                        error_detail=str(exc),
                    )
                )
        else:
            store.enqueue_roadmap(
                RoadmapQueueEntry(
                    repo_id=repo_id,
                    prd_path=prd.prd_path,
                    status="queued",
                    trigger="global",
                    started_at=None,
                    finished_at=None,
                    error_detail=None,
                )
            )
            queued.append(prd.prd_path)

    return RoadmapGlobalStartResult(
        started=started,
        queued=queued,
        skipped=skipped,
    )


def stop_global_roadmap(*, repo_id: str, store: IRoadmapStore) -> dict:
    """Clear the roadmap queue for a repository.

    Already-running PRDs are not stopped; only queued entries are removed.
    """
    store.clear_roadmap_queue(repo_id=repo_id)
    _audit(
        store,
        action="stop_global",
        repo_id=repo_id,
        prd_path="",
        issue_number=None,
        result="accepted",
        detail="Cleared roadmap queue.",
    )
    return {"stopped": True, "repo_id": repo_id}
