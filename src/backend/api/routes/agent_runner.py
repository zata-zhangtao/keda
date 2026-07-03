"""Agent Runner read-only status endpoints."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

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


def _build_overview_response() -> dict:
    """Build the ``/overview`` monitoring response payload."""
    settings = load_fresh_agent_runner_settings()
    repository_contexts, resolution_failures = resolve_repository_targets_with_diagnostics(settings)
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


def _get_cached_overview_response() -> dict:
    """Return a cached overview when available to keep the dashboard snappy."""
    now = time.time()
    cached_payload = _OVERVIEW_CACHE.get("payload")
    cached_at = _OVERVIEW_CACHE.get("timestamp", 0)
    if cached_payload is not None and (now - cached_at) < _OVERVIEW_CACHE_TTL_SECONDS:
        return cached_payload
    payload = _build_overview_response()
    _OVERVIEW_CACHE["payload"] = payload
    _OVERVIEW_CACHE["timestamp"] = now
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
def get_agent_runner_overview() -> dict:
    """Return the Agent Runner monitoring overview.

    Read-only payload that the dashboard renders. Includes per-repository
    health, queue counts, Issue summaries, latest event markers and anomaly
    counts. Does not expose any write/modify API.
    """
    return _get_cached_overview_response()


@router.get("/agent-runner/issues/{issue_number}")
def get_agent_runner_issue_detail(issue_number: int) -> dict:
    """Return monitoring detail (labels, PR, worktree, timeline, anomalies) for an Issue."""
    if issue_number <= 0:
        raise HTTPException(status_code=400, detail="issue_number must be a positive integer.")
    return _build_issue_detail_response(issue_number)


# Pre-fill cache on module import so the first dashboard hit is snappy.
_warm_overview_cache()
