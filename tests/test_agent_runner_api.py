"""Tests for the Agent Runner read-only API endpoints."""

from __future__ import annotations

from backend.api.routes.agent_runner import get_agent_runner_status


def test_status_returns_repositories_list() -> None:
    """Status endpoint should include a repositories list."""
    result = get_agent_runner_status()
    assert "repositories" in result
    assert isinstance(result["repositories"], list)


def test_status_returns_config() -> None:
    """Status endpoint should return global config summary."""
    result = get_agent_runner_status()
    assert "config" in result
    assert "max_issues" in result["config"]
    assert "base_branch" in result["config"]


def test_health_returns_status() -> None:
    """Health endpoint should return a status string."""
    from backend.api.routes.agent_runner import get_agent_runner_health

    result = get_agent_runner_health()
    assert "status" in result
    assert result["status"] in ("healthy", "degraded")
