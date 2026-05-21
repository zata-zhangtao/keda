"""Tests for PRD-driven Issue creation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from backend.core.shared.models.agent_runner import CommandResult
from backend.core.use_cases.create_issue_from_prd import (
    IssueFromPrdRequest,
    create_issue_from_prd,
)
from backend.infrastructure.process_runner import SubprocessRunner
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a command for test setup."""
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, encoding="utf-8"
    )


def _init_repo(path: Path) -> None:
    """Initialize a git repository."""
    _run(["git", "init", "-b", "main"], path)
    _run(["git", "config", "user.name", "Test"], path)
    _run(["git", "config", "user.email", "test@example.com"], path)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-m", "init"], path)


def _init_remote_repo(local_repo_path: Path, remote_repo_path: Path) -> None:
    """Initialize a local repository with a bare remote."""
    _init_repo(local_repo_path)
    remote_repo_path.mkdir()
    _run(["git", "init", "--bare"], remote_repo_path)
    _run(["git", "remote", "add", "origin", str(remote_repo_path)], local_repo_path)
    _run(["git", "push", "-u", "origin", "main"], local_repo_path)


def _request(
    repo_path: Path, prd_path: Path, **request_overrides: Any
) -> IssueFromPrdRequest:
    """Build an Issue-from-PRD request for tests."""
    request_values = {"issue_type": "feature", **request_overrides}
    return IssueFromPrdRequest(
        repo_path=repo_path,
        prd_path=prd_path,
        **request_values,
    )


def _command_result(command: list[str], stdout: str = "") -> CommandResult:
    """Build a successful command result for tests."""
    return CommandResult(
        command=tuple(command), return_code=0, stdout=stdout, stderr=""
    )


def test_create_issue_from_prd_writes_issue_link(tmp_path: Path) -> None:
    """Issue creation should write the generated URL back to the PRD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    prd = repo / "tasks" / "20260516-120000-prd-example.md"
    prd.parent.mkdir()
    prd.write_text(
        "# PRD: Example\n\n## Acceptance Checklist\n\n- [x] One\n- [ ] Two\n",
        encoding="utf-8",
    )

    issue_url = create_issue_from_prd(
        request=_request(repo, Path("tasks/20260516-120000-prd-example.md")),
        github_client=fake_client,
    )

    assert issue_url == "https://github.com/example/repo/issues/42"
    assert "- GitHub Issue: https://github.com/example/repo/issues/42" in prd.read_text(
        encoding="utf-8"
    )
    create_calls = [c for c in fake_client.calls if c["method"] == "create_issue"]
    assert len(create_calls) == 1
    call = create_calls[0]
    assert call["labels"] == [
        "type/feature",
        "status/backlog",
        "source/prd",
        "agent/ready",
    ]


def test_create_issue_from_prd_with_agent_label(tmp_path: Path) -> None:
    """Agent routing label should be applied when explicitly requested."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    prd = repo / "prd.md"
    prd.write_text("# PRD: Test\n", encoding="utf-8")

    create_issue_from_prd(
        request=_request(repo, Path("prd.md"), issue_type="bug", issue_agent="claude"),
        github_client=fake_client,
    )

    create_calls = [c for c in fake_client.calls if c["method"] == "create_issue"]
    assert "agent/claude" in create_calls[0]["labels"]


def test_create_issue_from_prd_force_overwrite(tmp_path: Path) -> None:
    """--force should overwrite an existing Issue link."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    prd = repo / "prd.md"
    prd.write_text("# PRD: Test\n\n- GitHub Issue: https://old.url\n", encoding="utf-8")

    create_issue_from_prd(
        request=_request(repo, Path("prd.md"), force=True),
        github_client=fake_client,
    )

    prd_text = prd.read_text(encoding="utf-8")
    assert "https://old.url" not in prd_text
    assert "https://github.com/example/repo/issues/42" in prd_text


def test_publish_prd_adds_ready_label_after_push(tmp_path: Path) -> None:
    """--publish-prd --ready should add ready only after the PRD push succeeds."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()
    process_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): _command_result(
                ["git", "branch", "--show-current"], stdout="main\n"
            ),
            ("git", "diff", "--cached", "--name-only", "--"): _command_result(
                ["git", "diff", "--cached", "--name-only", "--"], stdout=""
            ),
        }
    )

    prd = repo / "tasks" / "20260521-110127-prd-publish-prd-before-ready.md"
    prd.parent.mkdir()
    prd.write_text("# PRD: Publish Before Ready\n", encoding="utf-8")

    issue_url = create_issue_from_prd(
        request=_request(
            repo,
            Path("tasks/20260521-110127-prd-publish-prd-before-ready.md"),
            queue_ready=True,
            issue_agent="codex",
            publish_prd=True,
            git_remote="origin",
            git_base_branch="main",
        ),
        github_client=fake_client,
        process_runner=process_runner,
    )

    assert issue_url == "https://github.com/example/repo/issues/42"
    create_calls = [
        call for call in fake_client.calls if call["method"] == "create_issue"
    ]
    assert create_calls[0]["labels"] == [
        "type/feature",
        "status/backlog",
        "source/prd",
        "agent/codex",
    ]
    assert fake_client.calls[-1] == {
        "method": "edit_issue_labels",
        "issue_number": 42,
        "add": ["agent/ready"],
        "remove": [],
    }
    assert process_runner.calls == [
        ["git", "branch", "--show-current"],
        ["git", "diff", "--cached", "--name-only", "--"],
        [
            "git",
            "add",
            "--",
            "tasks/20260521-110127-prd-publish-prd-before-ready.md",
        ],
        [
            "git",
            "commit",
            "-m",
            "docs(prd): publish publish-prd-before-ready",
            "--",
            "tasks/20260521-110127-prd-publish-prd-before-ready.md",
        ],
        ["git", "push", "origin", "main"],
    ]


def test_publish_prd_push_failure_does_not_add_ready_label(tmp_path: Path) -> None:
    """A failed PRD push should fail the command without adding ready."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()
    push_command = ("git", "push", "origin", "main")
    process_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): _command_result(
                ["git", "branch", "--show-current"], stdout="main\n"
            ),
            ("git", "diff", "--cached", "--name-only", "--"): _command_result(
                ["git", "diff", "--cached", "--name-only", "--"], stdout=""
            ),
            push_command: CommandResult(
                command=push_command,
                return_code=1,
                stdout="",
                stderr="rejected",
            ),
        }
    )

    prd = repo / "tasks" / "pending" / "example.md"
    prd.parent.mkdir(parents=True)
    prd.write_text("# PRD: Example\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Command failed"):
        create_issue_from_prd(
            request=_request(
                repo,
                Path("tasks/pending/example.md"),
                publish_prd=True,
                queue_ready=True,
                git_base_branch="main",
            ),
            github_client=fake_client,
            process_runner=process_runner,
        )

    create_calls = [
        call for call in fake_client.calls if call["method"] == "create_issue"
    ]
    edit_calls = [
        call for call in fake_client.calls if call["method"] == "edit_issue_labels"
    ]
    assert "agent/ready" not in create_calls[0]["labels"]
    assert edit_calls == []


def test_publish_prd_rejects_non_target_staged_changes(tmp_path: Path) -> None:
    """Non-target staged changes should prevent automatic PRD publishing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()
    process_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): _command_result(
                ["git", "branch", "--show-current"], stdout="main\n"
            ),
            ("git", "diff", "--cached", "--name-only", "--"): _command_result(
                ["git", "diff", "--cached", "--name-only", "--"],
                stdout="README.md\n",
            ),
        }
    )

    prd = repo / "tasks" / "pending" / "example.md"
    prd.parent.mkdir(parents=True)
    prd.write_text("# PRD: Example\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="staged changes outside target PRD"):
        create_issue_from_prd(
            request=_request(
                repo,
                Path("tasks/pending/example.md"),
                publish_prd=True,
                queue_ready=True,
                git_base_branch="main",
            ),
            github_client=fake_client,
            process_runner=process_runner,
        )

    assert fake_client.calls == []
    assert ["git", "add", "--", "tasks/pending/example.md"] not in process_runner.calls


def test_publish_prd_commit_contains_only_target_prd(tmp_path: Path) -> None:
    """PRD publishing should not commit other unstaged or untracked files."""
    repo = tmp_path / "repo"
    remote_repo = tmp_path / "remote.git"
    repo.mkdir()
    _init_remote_repo(repo, remote_repo)
    fake_client = FakeGitHubClient()
    process_runner = SubprocessRunner()

    prd = repo / "tasks" / "pending" / "example.md"
    prd.parent.mkdir(parents=True)
    prd.write_text("# PRD: Example\n", encoding="utf-8")
    (repo / "README.md").write_text("unrelated modified file\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked file\n", encoding="utf-8")

    create_issue_from_prd(
        request=_request(
            repo,
            Path("tasks/pending/example.md"),
            publish_prd=True,
            queue_ready=False,
            git_remote="origin",
            git_base_branch="main",
        ),
        github_client=fake_client,
        process_runner=process_runner,
    )

    changed_paths_process = _run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], repo
    )
    status_process = _run(["git", "status", "--porcelain"], repo)

    assert changed_paths_process.stdout.splitlines() == ["tasks/pending/example.md"]
    assert " M README.md" in status_process.stdout
    assert "?? untracked.txt" in status_process.stdout
    assert [call["method"] for call in fake_client.calls] == ["create_issue"]


def test_publish_prd_ready_requires_base_branch(tmp_path: Path) -> None:
    """Ready PRD publishing should require the configured base branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()
    process_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): _command_result(
                ["git", "branch", "--show-current"], stdout="task/3-example\n"
            )
        }
    )

    prd = repo / "tasks" / "pending" / "example.md"
    prd.parent.mkdir(parents=True)
    prd.write_text("# PRD: Example\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Switch to base branch 'main'"):
        create_issue_from_prd(
            request=_request(
                repo,
                Path("tasks/pending/example.md"),
                publish_prd=True,
                queue_ready=True,
                git_base_branch="main",
            ),
            github_client=fake_client,
            process_runner=process_runner,
        )

    assert fake_client.calls == []


def test_without_publish_prd_no_git_commands_are_run(tmp_path: Path) -> None:
    """Default Issue creation should not execute PRD publishing Git commands."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()
    process_runner = FakeProcessRunner()

    prd = repo / "tasks" / "pending" / "example.md"
    prd.parent.mkdir(parents=True)
    prd.write_text("# PRD: Example\n", encoding="utf-8")

    create_issue_from_prd(
        request=_request(repo, Path("tasks/pending/example.md")),
        github_client=fake_client,
        process_runner=process_runner,
    )

    assert process_runner.calls == []
