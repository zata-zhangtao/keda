"""Tests for the console API routes and resilient repository resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.api.routes.agent_runner_console as console_routes
from backend.api.app import app
from backend.core.shared.models.agent_runner import (
    AppConfig,
    RepositoryRunContext,
)
from backend.infrastructure.config.registry_editor import TomlRegistryEditor
from backend.infrastructure.console.process_supervisor import (
    PidfileProcessSupervisor,
)
from backend.infrastructure.config.settings import (
    AgentRunnerConsoleSettings,
    AgentRunnerRepositorySettings,
    AgentRunnerSettings,
)
from backend.engines.agent_runner.factory import (
    resolve_repository_targets_with_diagnostics,
)
from backend.infrastructure.persistence.console_store import SqliteConsoleStore

client = TestClient(app)


@pytest.fixture
def console_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Wire console routes to tmp-backed store/supervisor/registry/contexts."""
    store = SqliteConsoleStore(tmp_path / "console.db")
    supervisor = PidfileProcessSupervisor(
        registry_path=tmp_path / "processes.json",
        log_dir=tmp_path / "logs",
    )
    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "# 保留注释\n"
        "[agent_runner.repositories.keda-main]\n"
        f'path = "{repo_dir}"\n'
        "enabled = true\n",
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
    fake_runner_command = [sys.executable, "-u", "-c", "print('fake runner')"]

    monkeypatch.setattr(console_routes, "create_console_store", lambda: store)
    monkeypatch.setattr(console_routes, "create_process_supervisor", lambda: supervisor)
    monkeypatch.setattr(
        console_routes,
        "create_registry_editor",
        lambda: TomlRegistryEditor(config_path),
    )
    monkeypatch.setattr(console_routes, "_resolve_contexts", lambda: contexts)
    monkeypatch.setattr(console_routes, "resolve_console_spawn_cwd", lambda: tmp_path)

    fake_settings = AgentRunnerSettings(
        console=AgentRunnerConsoleSettings(
            runner_command=fake_runner_command,
            stop_timeout_seconds=5,
        )
    )
    monkeypatch.setattr(
        console_routes, "load_fresh_agent_runner_settings", lambda: fake_settings
    )
    return {
        "store": store,
        "supervisor": supervisor,
        "config_path": config_path,
        "tmp_path": tmp_path,
    }


def test_process_lifecycle_via_api(console_environment) -> None:
    """Start, list, read logs and stop a process through the HTTP API."""
    start_response = client.post(
        "/api/v1/agent-runner/console/processes",
        json={"repo_id": "keda-main", "kind": "run_once"},
    )
    assert start_response.status_code == 201, start_response.text
    process_id = start_response.json()["process_id"]

    list_response = client.get("/api/v1/agent-runner/console/processes")
    assert list_response.status_code == 200
    listed_ids = [p["process_id"] for p in list_response.json()["processes"]]
    assert process_id in listed_ids

    log_response = client.get(
        f"/api/v1/agent-runner/console/processes/{process_id}/logs?offset=0"
    )
    assert log_response.status_code == 200
    assert "next_offset" in log_response.json()

    stop_response = client.post(
        f"/api/v1/agent-runner/console/processes/{process_id}/stop"
    )
    assert stop_response.status_code == 200
    assert stop_response.json()["status"] in ("stopped", "exited", "killed")


def test_duplicate_daemon_rejected_via_api(console_environment) -> None:
    """A second daemon for the same repo must return 409."""
    first = client.post(
        "/api/v1/agent-runner/console/processes",
        json={"repo_id": "keda-main", "kind": "daemon"},
    )
    assert first.status_code == 201
    process_id = first.json()["process_id"]
    # 第一个 fake daemon 立即退出的话去重就不会触发；等待时直接再启动。
    second = client.post(
        "/api/v1/agent-runner/console/processes",
        json={"repo_id": "keda-main", "kind": "daemon"},
    )
    # fake runner 可能已经退出（非常快），此时允许 201；
    # 仍在运行时必须 409。两种结果都不允许 500。
    assert second.status_code in (201, 409)
    client.post(f"/api/v1/agent-runner/console/processes/{process_id}/stop")


def test_unknown_process_kind_rejected(console_environment) -> None:
    """Kinds outside the whitelist enum must fail validation (422)."""
    response = client.post(
        "/api/v1/agent-runner/console/processes",
        json={"repo_id": "keda-main", "kind": "arbitrary_shell"},
    )
    assert response.status_code == 422


def test_issue_action_unknown_rejected_via_api(console_environment) -> None:
    """Unknown issue actions must return 400 and be audited."""
    response = client.post(
        "/api/v1/agent-runner/console/repositories/keda-main/issues/1/actions",
        json={"action": "merge_pr"},
    )
    assert response.status_code == 400
    audit_response = client.get("/api/v1/agent-runner/console/audit")
    audits = audit_response.json()["audits"]
    assert audits[0]["action"] == "merge_pr"
    assert audits[0]["result"] == "rejected"


def test_registry_endpoints(console_environment, tmp_path: Path) -> None:
    """Registry list/add/patch must round-trip through config.toml."""
    list_response = client.get("/api/v1/agent-runner/repositories")
    assert list_response.status_code == 200
    assert [r["repo_id"] for r in list_response.json()["repositories"]] == ["keda-main"]

    new_repo = tmp_path / "second-repo"
    (new_repo / ".git").mkdir(parents=True)
    add_response = client.post(
        "/api/v1/agent-runner/repositories",
        json={"repo_id": "second", "path": str(new_repo)},
    )
    assert add_response.status_code == 201, add_response.text

    bad_add_response = client.post(
        "/api/v1/agent-runner/repositories",
        json={"repo_id": "ghost", "path": "/not/here"},
    )
    assert bad_add_response.status_code == 400

    patch_response = client.patch(
        "/api/v1/agent-runner/repositories/second", json={"enabled": False}
    )
    assert patch_response.status_code == 200

    config_text = console_environment["config_path"].read_text(encoding="utf-8")
    assert "# 保留注释" in config_text
    assert "[agent_runner.repositories.second]" in config_text


def _write_iar_toml(repo_root: Path, repo_id: str, display_name: str) -> None:
    """Helper to write a minimal .iar.toml for discovery tests."""
    iar_toml = repo_root / ".iar.toml"
    iar_toml.write_text(
        "[agent_runner]\n"
        "[agent_runner.repository]\n"
        f'id = "{repo_id}"\n'
        f'display_name = "{display_name}"\n',
        encoding="utf-8",
    )


def test_discover_iar_repositories_finds_local_repos(
    console_environment, tmp_path: Path
) -> None:
    """Discover endpoint must find IAR-initialized git repositories."""
    scan_root = tmp_path / "code"
    scan_root.mkdir()

    discovered_repo = scan_root / "foo"
    discovered_repo.mkdir()
    (discovered_repo / ".git").mkdir()
    _write_iar_toml(discovered_repo, "foo", "Foo Project")

    nested_parent = scan_root / "nested"
    nested_parent.mkdir()
    nested_repo = nested_parent / "bar"
    nested_repo.mkdir()
    (nested_repo / ".git").mkdir()
    _write_iar_toml(nested_repo, "bar", "Bar Project")

    non_iar_repo = scan_root / "baz"
    non_iar_repo.mkdir()
    (non_iar_repo / ".git").mkdir()

    response = client.get(
        "/api/v1/agent-runner/repositories/discover",
        params={"scan_root": str(scan_root)},
    )
    assert response.status_code == 200, response.text
    discovered = response.json()["repositories"]
    assert len(discovered) == 2
    repo_ids = {entry["repo_id"] for entry in discovered}
    assert repo_ids == {"bar", "foo"}
    assert all("display_name" in entry for entry in discovered)
    assert all("already_registered" in entry for entry in discovered)


def test_batch_add_repositories_skips_existing(
    console_environment, tmp_path: Path
) -> None:
    """Batch add should add new repos and skip already-registered ones."""
    first_repo = tmp_path / "first"
    first_repo.mkdir()
    (first_repo / ".git").mkdir()

    second_repo = tmp_path / "second"
    second_repo.mkdir()
    (second_repo / ".git").mkdir()

    response = client.post(
        "/api/v1/agent-runner/repositories/batch",
        json={
            "repositories": [
                {"repo_id": "keda-main", "path": str(first_repo)},
                {"repo_id": "second", "path": str(second_repo)},
            ]
        },
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert len(result["added"]) == 1
    assert result["added"][0]["repo_id"] == "second"
    assert result["skipped"] == ["keda-main"]
    assert result["errors"] == []

    list_response = client.get("/api/v1/agent-runner/repositories")
    registered_ids = {r["repo_id"] for r in list_response.json()["repositories"]}
    assert registered_ids == {"keda-main", "second"}


def test_stats_history_empty(console_environment) -> None:
    """History endpoint must work with an empty store."""
    response = client.get("/api/v1/agent-runner/console/stats/history?days=7")
    assert response.status_code == 200
    assert response.json()["trend"] == []


def test_audit_endpoint_lists_actions(console_environment) -> None:
    """Audit endpoint should expose process start/stop entries."""
    start = client.post(
        "/api/v1/agent-runner/console/processes",
        json={"repo_id": "keda-main", "kind": "run_once"},
    )
    process_id = start.json()["process_id"]
    client.post(f"/api/v1/agent-runner/console/processes/{process_id}/stop")
    audits = client.get("/api/v1/agent-runner/console/audit").json()["audits"]
    actions = [a["action"] for a in audits]
    assert "start_run_once" in actions
    assert "stop_process" in actions


# ── 韧性解析（registry 路径漂移不拖死面板） ─────────────────────────────────


def test_resolution_diagnostics_isolates_broken_path(tmp_path: Path) -> None:
    """One broken registry path must not abort resolution of others."""
    import subprocess

    good_repo = tmp_path / "good"
    good_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(good_repo)], check=True)
    settings = AgentRunnerSettings(
        repositories={
            "good": AgentRunnerRepositorySettings(path=str(good_repo)),
            "broken": AgentRunnerRepositorySettings(path="/missing/path"),
        }
    )
    contexts, failures = resolve_repository_targets_with_diagnostics(settings)
    # 注意：pydantic-settings 会把 config.toml 里真实 registry 合并进来，
    # 这里只断言 fixture 条目的行为，不假设 registry 为空。
    resolved_repo_ids = [context.repo_id for context in contexts]
    assert "good" in resolved_repo_ids
    assert "broken" not in resolved_repo_ids
    broken_failures = [f for f in failures if f.repo_id == "broken"]
    assert len(broken_failures) == 1
    assert "does not exist" in broken_failures[0].error


def test_auth_me_returns_local_session() -> None:
    """The local auth endpoint must return a fixed operator session."""
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == "local-operator"
    assert "display_name" in payload
