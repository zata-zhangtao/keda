"""Agent Runner read-only status endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from backend.engines.agent_runner.factory import (
    create_process_runner,
    get_agent_runner_status_data,
)

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
        runner.run(["gh", "--version"], cwd=".")
        gh_available = True
    except Exception:
        pass

    return {
        "status": "healthy" if gh_available else "degraded",
        "gh_cli_available": gh_available,
    }
