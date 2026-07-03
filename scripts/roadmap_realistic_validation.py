#!/usr/bin/env python3
"""Sandbox realistic validation for roadmap actions (RV-1 / RV-2).

Runs outside the pytest suite so it can write evidence to `.iar/evidence/`
without depending on real GitHub API or real runner subprocesses.
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

# Make the project importable when run from repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from backend.api.app import app  # noqa: E402
from backend.core.shared.interfaces.agent_runner import IProcessRunner  # noqa: E402
from backend.core.use_cases.roadmap_actions import start_global_roadmap  # noqa: E402
from backend.core.shared.interfaces.runner_console import (  # noqa: E402
    IRunnerProcessSupervisor,
    ProcessLogChunk,
    RunnerProcessKind,
    RunnerProcessRecord,
)
from backend.core.shared.models.agent_runner import AppConfig, RepositoryRunContext  # noqa: E402
from backend.core.shared.models.roadmap import RoadmapPrdState  # noqa: E402
from backend.infrastructure.config.settings import (  # noqa: E402
    AgentRunnerConsoleSettings,
    AgentRunnerSettings,
)
from backend.infrastructure.persistence.console_store import SqliteConsoleStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

EVIDENCE_DIR = ROOT / ".iar" / "evidence"


@dataclass
class FakeGitHubClient:
    """In-memory GitHub client that records every call."""

    next_issue_number: int = 100
    calls: list[dict] = field(default_factory=list)
    labels: dict[int, set[str]] = field(default_factory=dict)

    def create_issue(self, *, title: str, body: str, labels: Sequence[str]) -> str:
        self.calls.append({"method": "create_issue", "title": title, "labels": list(labels)})
        number = self.next_issue_number
        self.next_issue_number += 1
        self.labels[number] = set(labels)
        return f"https://github.com/example/repo/issues/{number}"

    def edit_issue_labels(
        self, issue_number: int, *, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        self.calls.append(
            {
                "method": "edit_issue_labels",
                "issue_number": issue_number,
                "add": list(add),
                "remove": list(remove),
            }
        )
        current = self.labels.setdefault(issue_number, set())
        current.update(add)
        current.difference_update(remove)

    def get_issue(self, issue_number: int):
        from backend.infrastructure.github_client import IssueSummary

        return IssueSummary(
            number=issue_number,
            title=f"Issue #{issue_number}",
            url=f"https://github.com/example/repo/issues/{issue_number}",
            body="",
            labels=tuple(self.labels.get(issue_number, ())),
            state="OPEN",
        )

    def list_issue_comments(self, issue_number: int) -> list[str]:
        return []

    def get_pull_request_context(self, branch: str):
        return None

    def find_open_pr_by_head(self, branch: str) -> str | None:
        return None

    def find_merged_pr_by_head(self, branch: str) -> str | None:
        return None

    def list_issues_by_label(self, label: str, limit: int, state: str = "all") -> list:
        return []


@dataclass
class FakeProcessRunner(IProcessRunner):
    """Process runner that pretends all git commands succeeded."""

    commands: list[list[str]] = field(default_factory=list)

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: int | None = None,
        capture_output: bool = True,
        input_text: str | None = None,
    ):
        self.commands.append(list(command))

        class Result:
            return_code = 0
            stdout = ""
            stderr = ""

        if command[0] == "git" and command[1] == "branch" and command[2] == "--show-current":
            Result.stdout = "main\n"
        elif command[0] == "git" and command[1] == "status":
            Result.stdout = ""
        return Result()


@dataclass
class FakeSupervisor(IRunnerProcessSupervisor):
    """Process supervisor that records spawn calls."""

    spawns: list[dict] = field(default_factory=list)

    def spawn(
        self,
        *,
        repo_id: str,
        kind: RunnerProcessKind,
        argv: list[str],
        cwd: Path,
        log_path: Path | None = None,
    ) -> RunnerProcessRecord:
        self.spawns.append(
            {
                "repo_id": repo_id,
                "kind": kind,
                "argv": argv,
                "cwd": str(cwd),
                "log_path": str(log_path) if log_path else None,
            }
        )
        return RunnerProcessRecord(
            process_id="fake-id",
            repo_id=repo_id,
            kind=kind,
            pid=1234,
            status="running",
            exit_code=None,
            log_path=str(log_path),
            command=argv,
            started_at=datetime.now(timezone.utc).isoformat(),
            stopped_at=None,
        )

    def list_processes(self) -> list[RunnerProcessRecord]:
        return []

    def get_process(self, process_id: str) -> RunnerProcessRecord | None:
        return None

    def stop(self, process_id: str, *, timeout_seconds: int) -> RunnerProcessRecord:
        raise KeyError(process_id)

    def read_log(self, process_id: str, *, offset: int, max_bytes: int) -> ProcessLogChunk:
        return ProcessLogChunk(content="", next_offset=offset, eof=True)


def build_environment(tmp_path: Path):
    """Create an isolated roadmap test environment."""
    store = SqliteConsoleStore(tmp_path / "console.db")
    repo_dir = tmp_path / "repo"
    pending_dir = repo_dir / "tasks" / "pending"
    pending_dir.mkdir(parents=True)

    # Two independent PRDs for global scheduling.
    for index in range(2):
        (pending_dir / f"P1-FEAT-2026010{index}-alpha.md").write_text(
            f"# PRD: Alpha {index}\n\n## Acceptance Checklist\n- [ ] item\n",
            encoding="utf-8",
        )

    # A depends-on B pair.
    (pending_dir / "P1-FEAT-20260101-a.md").write_text(
        "# PRD: A\n\n## Acceptance Checklist\n- [ ] item\n",
        encoding="utf-8",
    )
    (pending_dir / "P1-FEAT-20260102-b.md").write_text(
        "# PRD: B\n\n## Acceptance Checklist\n- [ ] item\n\n"
        "## Delivery Dependencies\n- Depends on tasks/issues: `tasks/pending/P1-FEAT-20260101-a.md`\n",
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
    process_runner = FakeProcessRunner()
    supervisor = FakeSupervisor()

    fake_settings = AgentRunnerSettings(
        console=AgentRunnerConsoleSettings(runner_command=["echo", "fake-runner"])
    )

    import backend.api.routes.agent_runner_roadmap as roadmap_routes
    import backend.engines.agent_runner.factory as agent_factory

    roadmap_routes.create_roadmap_store = lambda: store
    roadmap_routes._resolve_contexts = lambda: contexts
    roadmap_routes.create_github_client = lambda repo_path: github_client
    agent_factory.create_github_client = lambda repo_path: github_client
    roadmap_routes.create_process_runner = lambda: process_runner
    agent_factory.create_process_runner = lambda: process_runner
    roadmap_routes.create_process_supervisor = lambda: supervisor
    agent_factory.create_process_supervisor = lambda: supervisor
    roadmap_routes.resolve_console_spawn_cwd = lambda: tmp_path
    agent_factory.resolve_console_spawn_cwd = lambda: tmp_path
    roadmap_routes.load_fresh_agent_runner_settings = lambda: fake_settings
    agent_factory.load_fresh_agent_runner_settings = lambda: fake_settings

    return {
        "store": store,
        "github_client": github_client,
        "process_runner": process_runner,
        "supervisor": supervisor,
        "repo_dir": repo_dir,
        "contexts": contexts,
        "tmp_path": tmp_path,
        "fake_settings": fake_settings,
    }


def _encode(prd_path: str) -> str:
    return base64.urlsafe_b64encode(prd_path.encode("utf-8")).decode("ascii")


def run_rv1(client: TestClient, env: dict, evidence: dict) -> None:
    """RV-1: single start creates issue, publishes PRD, labels ready, spawns runner."""
    # Use the dependency root PRD so RV-2 sees one already-running slot.
    prd_path = "tasks/pending/P1-FEAT-20260101-a.md"
    response = client.post(
        f"/api/v1/agent-runner/roadmap/prds/{_encode(prd_path)}/start",
        json={"repo_id": "keda-main"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["state"] == RoadmapPrdState.READY.value

    # Verify publish-safe path: create issue, git publish, then add ready label, then spawn.
    gh = env["github_client"]
    create_calls = [c for c in gh.calls if c["method"] == "create_issue"]
    assert len(create_calls) == 1
    issue_number = result["issue_number"]
    assert issue_number is not None

    edit_calls = [c for c in gh.calls if c["method"] == "edit_issue_labels"]
    ready_call = next((c for c in edit_calls if "agent/ready" in c.get("add", [])), None)
    assert ready_call is not None
    assert ready_call["issue_number"] == issue_number

    git_commands = [" ".join(c) for c in env["process_runner"].commands if c[0] == "git"]
    assert any("git add" in cmd for cmd in git_commands)
    assert any("git commit" in cmd for cmd in git_commands)
    assert any("git push" in cmd for cmd in git_commands)

    spawns = env["supervisor"].spawns
    assert len(spawns) == 1
    assert spawns[0]["kind"] == RunnerProcessKind.RUN_ONCE.value
    assert any("roadmap" in arg or "run" in arg for arg in spawns[0]["argv"])

    evidence["rv1"] = {
        "issue_number": issue_number,
        "git_commands": git_commands,
        "spawns": spawns,
        "audit": [a.__dict__ for a in env["store"].list_recent_audits(limit=100)],
    }


def run_rv2(client: TestClient, env: dict, evidence: dict) -> None:
    """RV-2: global start respects max_parallel and dependency order."""
    # Reset non-stateful recording structures so RV-2 evidence is clean.
    env["github_client"].calls.clear()
    env["process_runner"].commands.clear()
    env["supervisor"].spawns.clear()

    raw_result = start_global_roadmap(
        repo_id="keda-main",
        max_parallel=1,
        contexts=env["contexts"],
        github_client_factory=lambda repo_path: env["github_client"],
        supervisor=env["supervisor"],
        store=env["store"],
        runner_command=env["fake_settings"].console.runner_command,
        spawn_cwd=env["tmp_path"],
        process_runner=env["process_runner"],
    )
    result = {
        "started": [
            {
                "prd_path": s.prd_path,
                "issue_number": s.issue_number,
                "state": s.state.value,
                "detail": s.detail,
            }
            for s in raw_result.started
        ],
        "queued": raw_result.queued,
        "skipped": raw_result.skipped,
    }

    # With 2 independent PRDs and max_parallel=1, exactly 1 starts and 1 queues.
    assert len(result["started"]) == 1
    assert len(result["queued"]) == 1, f"expected 1 queued, got {len(result['queued'])}"

    started_paths = {s["prd_path"] for s in result["started"]}
    queued_paths = set(result["queued"])
    assert "tasks/pending/P1-FEAT-20260102-b.md" not in started_paths
    assert "tasks/pending/P1-FEAT-20260102-b.md" not in queued_paths

    # Queue evidence.
    queue_rows = env["store"].list_roadmap_queue(repo_id="keda-main")
    evidence["rv2"] = {
        "started": result["started"],
        "queued": result["queued"],
        "skipped": result["skipped"],
        "queue_rows": [q.__dict__ for q in queue_rows],
    }


def main() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(prefix="roadmap-rv-"))

    try:
        env = build_environment(tmp_path)
    except Exception:
        shutil.rmtree(tmp_path, ignore_errors=True)
        raise
    client = TestClient(app)

    evidence: dict = {}
    try:
        run_rv1(client, env, evidence)
        run_rv2(client, env, evidence)
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)

    audit_path = EVIDENCE_DIR / "roadmap-rv1-audit.txt"
    audit_path.write_text(
        json.dumps(evidence["rv1"]["audit"], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    runner_path = EVIDENCE_DIR / "roadmap-rv1-runner.log"
    runner_path.write_text(
        "\n".join(" ".join(c) for c in env["process_runner"].commands),
        encoding="utf-8",
    )

    queue_path = EVIDENCE_DIR / "roadmap-rv2-queue.json"
    queue_path.write_text(
        json.dumps(evidence["rv2"]["queue_rows"], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    poll_path = EVIDENCE_DIR / "roadmap-rv2-poll.jsonl"
    poll_lines = [
        json.dumps({"started": evidence["rv2"]["started"], "queued": evidence["rv2"]["queued"]}),
    ]
    poll_path.write_text("\n".join(poll_lines) + "\n", encoding="utf-8")

    print(f"Realistic validation evidence saved to {EVIDENCE_DIR}")
    print(f"  - {audit_path.name}")
    print(f"  - {runner_path.name}")
    print(f"  - {queue_path.name}")
    print(f"  - {poll_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
