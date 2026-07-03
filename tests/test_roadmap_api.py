"""Tests for the roadmap API routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.api.routes.agent_runner_roadmap as roadmap_routes
from backend.api.app import app
from backend.core.shared.models.agent_runner import AppConfig, RepositoryRunContext
from backend.infrastructure.persistence.console_store import SqliteConsoleStore
from tests.conftest import FakeGitHubClient

client = TestClient(app)


@pytest.fixture
def roadmap_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Wire roadmap routes to tmp-backed store, repo, and fake GitHub client."""
    store = SqliteConsoleStore(tmp_path / "console.db")
    repo_dir = tmp_path / "repo"
    pending_dir = repo_dir / "tasks" / "pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / "P1-FEAT-20260101-test.md").write_text(
        "# PRD: Test Feature\n\n## Acceptance Checklist\n- [ ] item\n",
        encoding="utf-8",
    )

    contexts = [
        RepositoryRunContext(
            repo_id="keda-main",
            display_name="Keda Main",
            repo_path=repo_dir,
            config=AppConfig(),
        )
    ]
    github_client = FakeGitHubClient()

    monkeypatch.setattr(roadmap_routes, "create_roadmap_store", lambda: store)
    monkeypatch.setattr(roadmap_routes, "_resolve_contexts", lambda: contexts)
    monkeypatch.setattr(roadmap_routes, "create_github_client", lambda repo_path: github_client)
    monkeypatch.setattr(
        roadmap_routes,
        "create_process_runner",
        lambda: type("FakeRunner", (), {"run": lambda *a, **k: None})(),
    )
    monkeypatch.setattr(
        roadmap_routes,
        "create_process_supervisor",
        lambda: type(
            "FakeSupervisor",
            (),
            {
                "list_processes": lambda: [],
                "spawn": lambda **kwargs: type(
                    "Record",
                    (),
                    {
                        "process_id": "fake-id",
                        "repo_id": kwargs.get("repo_id"),
                        "kind": kwargs.get("kind"),
                        "pid": 1234,
                        "status": "running",
                        "exit_code": None,
                        "log_path": "",
                        "command": kwargs.get("argv"),
                        "started_at": "",
                        "stopped_at": None,
                    },
                )(),
            },
        )(),
    )
    monkeypatch.setattr(roadmap_routes, "resolve_console_spawn_cwd", lambda: tmp_path)

    from backend.infrastructure.config.settings import (
        AgentRunnerConsoleSettings,
        AgentRunnerSettings,
    )

    fake_settings = AgentRunnerSettings(
        console=AgentRunnerConsoleSettings(
            runner_command=["echo", "fake"],
        )
    )
    monkeypatch.setattr(roadmap_routes, "load_fresh_agent_runner_settings", lambda: fake_settings)

    return {
        "store": store,
        "repo_dir": repo_dir,
        "github_client": github_client,
        "tmp_path": tmp_path,
    }


def test_list_roadmap_prds(roadmap_environment) -> None:
    """GET /roadmap/prds should return scanned PRDs."""
    response = client.get(
        "/api/v1/agent-runner/roadmap/prds?repo_id=keda-main&include_archived=false"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["repo_id"] == "keda-main"
    assert len(data["prds"]) == 1
    assert data["prds"][0]["title"] == "Test Feature"


def test_update_settings(roadmap_environment) -> None:
    """PATCH /roadmap/settings should persist settings."""
    response = client.patch(
        "/api/v1/agent-runner/roadmap/settings?repo_id=keda-main",
        json={"max_parallel": 3, "default_view": "list"},
    )
    assert response.status_code == 200
    assert response.json()["max_parallel"] == 3

    response = client.get("/api/v1/agent-runner/roadmap/settings?repo_id=keda-main")
    assert response.status_code == 200
    assert response.json()["max_parallel"] == 3


def test_start_prd_rejects_missing_repo(roadmap_environment) -> None:
    """Starting a PRD for an unknown repo must return 400."""
    import base64

    encoded = base64.urlsafe_b64encode(b"tasks/pending/P1-FEAT-20260101-test.md").decode("ascii")
    response = client.post(
        f"/api/v1/agent-runner/roadmap/prds/{encoded}/start",
        json={"repo_id": "unknown"},
    )
    assert response.status_code == 400


def test_start_global_requires_valid_parallel(roadmap_environment) -> None:
    """Global start must validate max_parallel bounds."""
    response = client.post(
        "/api/v1/agent-runner/roadmap/start-global",
        json={"repo_id": "keda-main", "max_parallel": 0},
    )
    assert response.status_code == 422
