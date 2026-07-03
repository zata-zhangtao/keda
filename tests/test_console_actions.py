"""Tests for console whitelisted actions and audit logging."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    RepositoryRunContext,
)
from backend.core.use_cases.console_actions import (
    ConsoleActionError,
    execute_issue_action,
    execute_repository_action,
)
from backend.infrastructure.console.process_supervisor import (
    PidfileProcessSupervisor,
)
from backend.infrastructure.persistence.console_store import SqliteConsoleStore


class FakeGitHubClient:
    """Minimal IGitHubClient stub for label-flip actions."""

    def __init__(self, issue: IssueSummary) -> None:
        self._issue = issue
        self.label_edits: list[tuple[int, tuple[str, ...], tuple[str, ...]]] = []

    def get_issue(self, issue_number: int) -> IssueSummary:
        assert issue_number == self._issue.number
        return self._issue

    def edit_issue_labels(self, issue_number, *, add=(), remove=()):
        self.label_edits.append((issue_number, tuple(add), tuple(remove)))


def _make_context(repo_id: str = "keda-main") -> RepositoryRunContext:
    return RepositoryRunContext(
        repo_id=repo_id,
        display_name=repo_id,
        repo_path=Path("/tmp/repo"),
        config=AppConfig(),
    )


def _make_store(tmp_path: Path) -> SqliteConsoleStore:
    return SqliteConsoleStore(tmp_path / "console.db")


def _make_supervisor(tmp_path: Path) -> PidfileProcessSupervisor:
    return PidfileProcessSupervisor(
        registry_path=tmp_path / "processes.json",
        log_dir=tmp_path / "logs",
    )


def _fake_runner_command() -> list[str]:
    return [sys.executable, "-c", "print('fake runner')"]


def test_retry_failed_flips_labels_and_audits(tmp_path: Path) -> None:
    """retry_failed must flip failed -> ready and audit as accepted."""
    config = AppConfig()
    issue = IssueSummary(
        number=19,
        title="Broken",
        url="https://example.test/19",
        body="",
        labels=(config.labels.failed,),
    )
    github_client = FakeGitHubClient(issue)
    store = _make_store(tmp_path)

    action_result = execute_issue_action(
        action="retry_failed",
        repo_id="keda-main",
        issue_number=19,
        contexts=[_make_context()],
        github_client_factory=lambda _path: github_client,
        supervisor=_make_supervisor(tmp_path),
        store=store,
        runner_command=_fake_runner_command(),
        spawn_cwd=tmp_path,
    )

    assert action_result.result == "accepted"
    assert github_client.label_edits == [(19, (config.labels.ready,), (config.labels.failed,))]
    audits = store.list_recent_audits(limit=10)
    assert audits[0].action == "retry_failed"
    assert audits[0].result == "accepted"


def test_retry_failed_rejected_when_not_failed(tmp_path: Path) -> None:
    """retry_failed on a non-failed Issue must be rejected and audited."""
    issue = IssueSummary(
        number=20,
        title="Fine",
        url="https://example.test/20",
        body="",
        labels=("agent/review",),
    )
    store = _make_store(tmp_path)
    with pytest.raises(ConsoleActionError):
        execute_issue_action(
            action="retry_failed",
            repo_id="keda-main",
            issue_number=20,
            contexts=[_make_context()],
            github_client_factory=lambda _path: FakeGitHubClient(issue),
            supervisor=_make_supervisor(tmp_path),
            store=store,
            runner_command=_fake_runner_command(),
            spawn_cwd=tmp_path,
        )
    audits = store.list_recent_audits(limit=10)
    assert audits[0].result == "rejected"


def test_unknown_action_rejected_and_audited(tmp_path: Path) -> None:
    """Unknown actions must be rejected (whitelist) and audited."""
    store = _make_store(tmp_path)
    with pytest.raises(ConsoleActionError, match="Unknown issue action"):
        execute_issue_action(
            action="rm_rf_slash",
            repo_id="keda-main",
            issue_number=1,
            contexts=[_make_context()],
            github_client_factory=lambda _path: FakeGitHubClient(
                IssueSummary(number=1, title="", url="", body="", labels=())
            ),
            supervisor=_make_supervisor(tmp_path),
            store=store,
            runner_command=_fake_runner_command(),
            spawn_cwd=tmp_path,
        )
    audits = store.list_recent_audits(limit=10)
    assert audits[0].action == "rm_rf_slash"
    assert audits[0].result == "rejected"


def test_repository_run_once_spawns_process(tmp_path: Path) -> None:
    """run_once action must spawn a one-shot process and audit it."""
    store = _make_store(tmp_path)
    supervisor = _make_supervisor(tmp_path)
    action_result = execute_repository_action(
        action="run_once",
        repo_id="keda-main",
        contexts=[_make_context()],
        supervisor=supervisor,
        store=store,
        runner_command=_fake_runner_command(),
        spawn_cwd=tmp_path,
    )
    assert action_result.result == "accepted"
    assert action_result.process is not None
    audits = store.list_recent_audits(limit=10)
    assert audits[0].action == "run_once"
    assert audits[0].result == "accepted"


def test_repository_unknown_action_rejected(tmp_path: Path) -> None:
    """Unknown repository actions must be rejected before any spawn."""
    store = _make_store(tmp_path)
    with pytest.raises(ConsoleActionError, match="Unknown repository action"):
        execute_repository_action(
            action="deploy_to_prod",
            repo_id="keda-main",
            contexts=[_make_context()],
            supervisor=_make_supervisor(tmp_path),
            store=store,
            runner_command=_fake_runner_command(),
            spawn_cwd=tmp_path,
        )
    audits = store.list_recent_audits(limit=10)
    assert audits[0].result == "rejected"


def test_blocked_continue_spawns_process_with_issue(tmp_path: Path) -> None:
    """blocked_continue must spawn a one-shot process bound to the issue."""
    store = _make_store(tmp_path)
    supervisor = _make_supervisor(tmp_path)
    action_result = execute_issue_action(
        action="blocked_continue",
        repo_id="keda-main",
        issue_number=42,
        contexts=[_make_context()],
        github_client_factory=lambda _path: FakeGitHubClient(
            IssueSummary(number=42, title="", url="", body="", labels=())
        ),
        supervisor=supervisor,
        store=store,
        runner_command=_fake_runner_command(),
        spawn_cwd=tmp_path,
    )
    assert action_result.result == "accepted"
    assert action_result.process is not None
    assert str(action_result.process.kind) == "blocked_continue"
