"""Tests for ``run_agent_repositories_once`` multi-repository fan-out.

Covers exit code aggregation, per-repository failure isolation and the
recovery hint emitted for network errors."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def test_run_agent_repositories_once_aggregates_exit_code() -> None:
    """Multi-repo run should return 1 if any repository fails."""
    from backend.core.shared.models.agent_runner import (
        RepositoryRunContext,
    )
    from backend.core.use_cases.run_agent_repositories_once import (
        run_agent_repositories_once,
    )

    fake_client = FakeGitHubClient()
    fake_client.list_ready_issues = lambda ready_label, limit: []
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "remote"): CommandResult(
                command=("git", "remote"),
                return_code=0,
                stdout="origin\n",
                stderr="",
            ),
        }
    )

    contexts = [
        RepositoryRunContext(
            repo_id="repo-a",
            display_name="Repo A",
            repo_path=Path("."),
            config=AppConfig(),
        ),
        RepositoryRunContext(
            repo_id="repo-b",
            display_name="Repo B",
            repo_path=Path("."),
            config=AppConfig(),
        ),
    ]

    exit_code = run_agent_repositories_once(
        contexts=contexts,
        dry_run=False,
        agent="auto",
        max_issues=1,
        process_runner=fake_runner,
        github_client_factory=lambda rp: fake_client,
    )

    assert exit_code == 0


def test_run_agent_repositories_once_isolates_failures() -> None:
    """One repository failure should not block subsequent repositories."""
    from backend.core.shared.models.agent_runner import (
        RepositoryRunContext,
    )
    from backend.core.use_cases.run_agent_repositories_once import (
        run_agent_repositories_once,
    )

    class _FailingClient(FakeGitHubClient):
        def __init__(self, should_fail: bool = False) -> None:
            super().__init__()
            self._should_fail = should_fail

        def list_ready_issues(self, ready_label: str, limit: int) -> list:
            if self._should_fail:
                raise RuntimeError("Simulated failure")
            return []

    contexts = [
        RepositoryRunContext(
            repo_id="repo-a",
            display_name="Repo A",
            repo_path=Path("."),
            config=AppConfig(),
        ),
        RepositoryRunContext(
            repo_id="repo-b",
            display_name="Repo B",
            repo_path=Path("."),
            config=AppConfig(),
        ),
    ]

    call_index = [0]

    def client_factory(rp: Path) -> FakeGitHubClient:
        call_index[0] += 1
        return _FailingClient(should_fail=(call_index[0] == 1))

    fake_runner = FakeProcessRunner()
    exit_code = run_agent_repositories_once(
        contexts=contexts,
        dry_run=False,
        agent="auto",
        max_issues=1,
        process_runner=fake_runner,
        github_client_factory=client_factory,
    )

    assert exit_code == 1


def test_run_agent_repositories_once_hints_recovery_for_network_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient GitHub API network error should include a recovery hint."""
    from backend.core.shared.models.agent_runner import (
        RepositoryRunContext,
    )
    from backend.core.use_cases.run_agent_repositories_once import (
        run_agent_repositories_once,
    )

    class _NetworkFailingClient(FakeGitHubClient):
        def list_ready_issues(self, ready_label: str, limit: int) -> list:
            raise RuntimeError(
                "Command '['gh', 'issue', 'list', ...]' returned non-zero exit "
                "status 1.\n\n--- stderr/stdout ---\n"
                'Post "https://api.github.com/graphql": EOF'
            )

    context = RepositoryRunContext(
        repo_id="repo-net",
        display_name="Repo Network",
        repo_path=Path("."),
        config=AppConfig(),
    )

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "remote"): CommandResult(
                command=("git", "remote"),
                return_code=0,
                stdout="origin\n",
                stderr="",
            ),
        }
    )
    with caplog.at_level(logging.ERROR):
        exit_code = run_agent_repositories_once(
            contexts=[context],
            dry_run=False,
            agent="auto",
            max_issues=1,
            process_runner=fake_runner,
            github_client_factory=lambda rp: _NetworkFailingClient(),
        )

    assert exit_code == 1
    assert any(
        "Run `iar run` again to retry" in record.message
        for record in caplog.records
        if record.levelno == logging.ERROR
    )
