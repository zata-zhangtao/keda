"""Agent Runner read-only status endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from backend.engines.agent_runner.factory import build_app_config

router = APIRouter(tags=["agent-runner"])


@router.get("/agent-runner/status")
def get_agent_runner_status() -> dict:
    """Return runner configuration summary and runtime status."""
    app_config = build_app_config()
    return {
        "daemon_mode": False,
        "config": {
            "max_issues": app_config.runner.max_issues,
            "default_agent": app_config.runner.default_agent,
            "max_recovery_attempts": app_config.runner.max_recovery_attempts,
            "ready_label": app_config.labels.ready,
            "running_label": app_config.labels.running,
            "review_label": app_config.labels.review,
            "failed_label": app_config.labels.failed,
            "base_branch": app_config.git.base_branch,
            "remote": app_config.git.remote,
            "auto_merge": app_config.safety.auto_merge,
            "forbidden_path_patterns": list(app_config.safety.forbidden_path_patterns),
        },
    }


@router.get("/agent-runner/health")
def get_agent_runner_health() -> dict:
    """Return runner health status."""
    from backend.engines.agent_runner.factory import create_process_runner

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
