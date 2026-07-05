"""Agent Runner read-only status endpoints."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from backend.core.shared.interfaces.agent_runner import (
    IGitHubClient,
    IProcessRunner,
)
from backend.core.use_cases.agent_runner_monitor import (
    IssueMonitoringSnapshot,
    MonitoringResult,
    build_issue_snapshot,
    build_overview,
)
from backend.engines.agent_runner.factory import (
    create_github_client,
    create_process_runner,
    get_agent_runner_status_data,
    load_fresh_agent_runner_settings,
    resolve_repository_targets_with_diagnostics,
)

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-runner"])


@router.get("/agent-runner/status")
def get_agent_runner_status() -> dict:
    """Return runner configuration summary and runtime status."""
    return get_agent_runner_status_data()


@router.get("/agent-runner/health")
def get_agent_runner_health() -> dict:
    """Return runner health status."""
    runner = create_process_runner()
    gh_available = False
    try:
        runner.run(["gh", "--version"], cwd=Path("."))
        gh_available = True
    except Exception:
        pass

    return {
        "status": "healthy" if gh_available else "degraded",
        "gh_cli_available": gh_available,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Monitoring dashboard endpoints
# ─────────────────────────────────────────────────────────────────────────────


def _serialize_monitoring(value: Any) -> Any:
    """Convert dataclass instances (and nested ones) into JSON-friendly dicts."""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _serialize_monitoring(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [_serialize_monitoring(item) for item in value]
    if isinstance(value, list):
        return [_serialize_monitoring(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize_monitoring(item) for key, item in value.items()}
    return value


def _get_monitoring_dependencies() -> tuple[Callable[[Path], IGitHubClient], IProcessRunner]:
    """Resolve the GitHub client factory and shared process runner."""
    return create_github_client, create_process_runner()


_OVERVIEW_CACHE: dict[str, Any] = {}
_OVERVIEW_CACHE_TTL_SECONDS = 30

# In-memory overview job store. Each job tracks the lifecycle of an async
# overview build. Local single-user deployment: jobs are lost on restart.
_OVERVIEW_JOBS: dict[str, dict[str, Any]] = {}
_OVERVIEW_JOBS_TTL_SECONDS = 300  # 5 minutes
_OVERVIEW_JOBS_LOCK = threading.Lock()


def _prune_overview_jobs() -> None:
    """Remove overview jobs that exceeded the retention window."""
    cutoff = time.time() - _OVERVIEW_JOBS_TTL_SECONDS
    with _OVERVIEW_JOBS_LOCK:
        stale = [
            job_id for job_id, job in _OVERVIEW_JOBS.items() if job.get("created_at", 0) < cutoff
        ]
        for job_id in stale:
            _OVERVIEW_JOBS.pop(job_id, None)


def _start_overview_job(repo_ids: list[str] | None) -> str:
    """Spawn an async overview build and return its job id."""
    job_id = uuid.uuid4().hex
    now = time.time()
    with _OVERVIEW_JOBS_LOCK:
        _OVERVIEW_JOBS[job_id] = {
            "status": "pending",
            "repo_ids": list(repo_ids) if repo_ids else None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "payload": None,
            "error": None,
        }
    thread = threading.Thread(
        target=_run_overview_job,
        args=(job_id, list(repo_ids) if repo_ids else None),
        daemon=True,
    )
    thread.start()
    return job_id


def _run_overview_job(job_id: str, repo_ids: list[str] | None) -> None:
    """Execute the overview build for a queued job, updating its state."""
    with _OVERVIEW_JOBS_LOCK:
        job = _OVERVIEW_JOBS.get(job_id)
        if job is None:
            return
        job["status"] = "running"
        job["started_at"] = time.time()
    try:
        payload = _build_overview_response(repo_ids=repo_ids)
    except HTTPException as exc:
        with _OVERVIEW_JOBS_LOCK:
            job["status"] = "failed"
            job["error"] = exc.detail
            job["finished_at"] = time.time()
        return
    except Exception as exc:  # noqa: BLE001
        _logger.exception("Overview job %s crashed: %s", job_id, exc)
        with _OVERVIEW_JOBS_LOCK:
            job["status"] = "failed"
            job["error"] = str(exc)
            job["finished_at"] = time.time()
        return
    with _OVERVIEW_JOBS_LOCK:
        job["status"] = "completed"
        job["payload"] = payload
        job["finished_at"] = time.time()


def _serialize_overview_job(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe view of an overview job's state."""
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "repo_ids": job.get("repo_ids"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "payload": job.get("payload"),
    }


def _warm_overview_cache(delay: int = 5) -> None:
    """Pre-fill the overview cache in the background after server start."""

    def _run() -> None:
        time.sleep(delay)
        try:
            _get_cached_overview_response()
            _logger.info("Overview cache warmed successfully.")
        except Exception as exc:
            _logger.warning("Overview cache warm-up failed: %s", exc)

    threading.Thread(target=_run, daemon=True).start()


def _build_overview_response(repo_ids: list[str] | None = None) -> dict:
    """Build the ``/overview`` monitoring response payload.

    Args:
        repo_ids: Optional whitelist of repository IDs to include. ``None``
            means all enabled repositories.
    """
    settings = load_fresh_agent_runner_settings()
    repository_contexts, resolution_failures = resolve_repository_targets_with_diagnostics(settings)
    if repo_ids:
        requested = set(repo_ids)
        repository_contexts = [ctx for ctx in repository_contexts if ctx.repo_id in requested]
        resolution_failures = [
            failure for failure in resolution_failures if failure.repo_id in requested
        ]
    github_client_factory, process_runner = _get_monitoring_dependencies()
    try:
        result: MonitoringResult = build_overview(
            repositories=repository_contexts,
            github_client_factory=github_client_factory,
            process_runner=process_runner,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.exception("Failed to build monitoring overview: %s", exc)
        raise HTTPException(
            status_code=500, detail=f"Failed to build monitoring overview: {exc}"
        ) from exc
    overview_payload = _serialize_monitoring(result)
    overview_payload["unreachable_repositories"] = [
        _serialize_monitoring(failure) for failure in resolution_failures
    ]
    return overview_payload


def _get_cached_overview_response(repo_ids: list[str] | None = None) -> dict:
    """Return a cached overview when available to keep the dashboard snappy.

    Caching is keyed by the repo_ids filter (or a sentinel for "all") so
    that single-repo refreshes and full overviews don't stomp each other.
    """
    cache_key = ",".join(repo_ids) if repo_ids else "__all__"
    now = time.time()
    cache_branch = _OVERVIEW_CACHE.get(cache_key)
    if isinstance(cache_branch, dict):
        cached_payload = cache_branch.get("payload")
        cached_at = cache_branch.get("timestamp", 0)
        if cached_payload is not None and (now - cached_at) < _OVERVIEW_CACHE_TTL_SECONDS:
            return cached_payload
    payload = _build_overview_response(repo_ids=repo_ids)
    _OVERVIEW_CACHE[cache_key] = {"payload": payload, "timestamp": now}
    return payload


def _build_issue_detail_response(issue_number: int) -> dict:
    """Build the ``/issues/{issue_number}`` monitoring response payload."""
    settings = load_fresh_agent_runner_settings()
    repository_contexts, _resolution_failures = resolve_repository_targets_with_diagnostics(
        settings
    )
    github_client_factory, process_runner = _get_monitoring_dependencies()

    snapshot: IssueMonitoringSnapshot | None = None
    for repository_context in repository_contexts:
        github_client = github_client_factory(repository_context.repo_path)
        try:
            matching_issues = github_client.list_review_candidate_issues(
                (
                    repository_context.config.labels.ready,
                    repository_context.config.labels.running,
                    repository_context.config.labels.supervising,
                    repository_context.config.labels.review,
                    repository_context.config.labels.failed,
                    repository_context.config.labels.blocked,
                ),
                200,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.info(
                "Issue #%d lookup skipped for %s: %s",
                issue_number,
                repository_context.repo_id,
                exc,
            )
            continue
        for issue in matching_issues:
            if issue.number != issue_number:
                continue
            try:
                snapshot = build_issue_snapshot(
                    issue=issue,
                    config=repository_context.config,
                    github_client=github_client,
                    process_runner=process_runner,
                    repo_path=repository_context.repo_path,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Failed to build issue detail for #%d: %s", issue_number, exc)
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to build issue detail: {exc}",
                ) from exc
            break
        if snapshot is not None:
            break

    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"Issue #{issue_number} not found in monitored repositories.",
        )
    return _serialize_monitoring(snapshot)


@router.get("/agent-runner/overview")
def get_agent_runner_overview(
    repo_ids: str | None = Query(
        default=None,
        description=(
            "Optional comma-separated list of repository IDs to scope the "
            "overview to. Omit (or pass empty) to include every enabled "
            "repository."
        ),
    ),
    async_run: bool = Query(
        default=False,
        description=(
            "When true, queue an async overview build and return 202 with "
            "a job_id instead of blocking the request until the payload is "
            "ready. Poll GET /agent-runner/overview/jobs/{job_id} for the "
            "result."
        ),
    ),
) -> dict:
    """Return the Agent Runner monitoring overview.

    Read-only payload that the dashboard renders. Includes per-repository
    health, queue counts, Issue summaries, latest event markers and anomaly
    counts. Does not expose any write/modify API.

    When ``async_run=true`` the response is HTTP 202 with a ``job_id``; the
    payload is fetched later via ``GET /agent-runner/overview/jobs/{id}``.
    """
    parsed_repo_ids: list[str] | None = None
    if repo_ids:
        parsed_repo_ids = [item.strip() for item in repo_ids.split(",") if item.strip()]
        if not parsed_repo_ids:
            parsed_repo_ids = None

    if async_run:
        _prune_overview_jobs()
        job_id = _start_overview_job(parsed_repo_ids)
        # Surface 202 to the caller via a structured payload; FastAPI does
        # not let us set the status code from a return dict, so the client
        # infers async mode from the presence of job_id + pending/running.
        return {
            "async": True,
            "job_id": job_id,
            "repo_ids": parsed_repo_ids,
        }

    return _get_cached_overview_response(repo_ids=parsed_repo_ids)


@router.get("/agent-runner/overview/jobs/{job_id}")
def get_agent_runner_overview_job(job_id: str) -> dict:
    """Return the state of an async overview build job."""
    _prune_overview_jobs()
    with _OVERVIEW_JOBS_LOCK:
        job = _OVERVIEW_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Overview job '{job_id}' not found.")
    return _serialize_overview_job(job_id, job)


@router.get("/agent-runner/overview/per-repo")
def get_agent_runner_overview_per_repo() -> dict:
    """Start one overview job per enabled repository.

    Returns a mapping of ``repo_id -> job_id`` so the dashboard can poll each
    repository independently and render cards as jobs complete (gradual
    reveal). Each job carries a single-repo whitelist so the backend only
    scans the one repository it owns.
    """
    _prune_overview_jobs()
    settings = load_fresh_agent_runner_settings()
    repository_contexts, _ = resolve_repository_targets_with_diagnostics(settings)
    jobs_by_repo: dict[str, str] = {}
    for context in repository_contexts:
        jobs_by_repo[context.repo_id] = _start_overview_job([context.repo_id])
    return {
        "jobs_by_repo": jobs_by_repo,
        "started_at": time.time(),
    }


@router.get("/agent-runner/issues/{issue_number}")
def get_agent_runner_issue_detail(issue_number: int) -> dict:
    """Return monitoring detail (labels, PR, worktree, timeline, anomalies) for an Issue."""
    if issue_number <= 0:
        raise HTTPException(status_code=400, detail="issue_number must be a positive integer.")
    return _build_issue_detail_response(issue_number)


# Pre-fill cache on module import so the first dashboard hit is snappy.
_warm_overview_cache()
