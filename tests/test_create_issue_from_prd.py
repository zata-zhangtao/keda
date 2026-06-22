"""Tests for PRD-driven Issue creation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from unittest.mock import patch

from backend.api.cli import _prompt_and_publish_prd_if_needed
from backend.core.shared.models.agent_runner import (
    CommandResult,
    GeneratedContentConfig,
    GeneratedContentTargetConfig,
    LabelConfig,
)
from backend.core.use_cases.create_issue_from_prd import (
    IssueFromPrdRequest,
    build_issue_body,
    create_issue_from_prd,
)
from backend.infrastructure.process_runner import SubprocessRunner
from tests.conftest import FakeContentGenerator, FakeGitHubClient, FakeProcessRunner


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
    """Build a PRD-to-Issue request for tests."""
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


def test_create_issue_from_prd_replaces_placeholder_issue_link(
    tmp_path: Path,
) -> None:
    """Placeholder Issue lines should not count as an existing link."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    prd = repo / "prd.md"
    prd.write_text(
        "# PRD: Test\n\n- GitHub Issue: (待创建/关联)\n\n## 1. Introduction\n",
        encoding="utf-8",
    )

    create_issue_from_prd(
        request=_request(repo, Path("prd.md")),
        github_client=fake_client,
    )

    prd_text = prd.read_text(encoding="utf-8")
    assert "(待创建/关联)" not in prd_text
    assert "- GitHub Issue: https://github.com/example/repo/issues/42" in prd_text
    assert prd_text.count("- GitHub Issue:") == 1


@pytest.mark.parametrize(
    "issue_line",
    [
        "- GitHub Issue: https://github.com/example/repo/issues/7",
        "- GitHub Issue: https://github.com/example/repo/issues/7 （含尾注说明）",
    ],
)
def test_create_issue_from_prd_rejects_existing_issue_link(
    tmp_path: Path, issue_line: str
) -> None:
    """A real Issue URL should block creation without --force."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    prd = repo / "prd.md"
    prd.write_text(f"# PRD: Test\n\n{issue_line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="already has a GitHub Issue link"):
        create_issue_from_prd(
            request=_request(repo, Path("prd.md")),
            github_client=fake_client,
        )

    create_calls = [c for c in fake_client.calls if c["method"] == "create_issue"]
    assert not create_calls


def test_create_issue_from_prd_materializes_prd_ref_issue_link(
    tmp_path: Path,
) -> None:
    """PRD filename dependencies should resolve to upstream Issue numbers."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    task_dir = repo / "tasks" / "pending"
    task_dir.mkdir(parents=True)
    upstream_prd = task_dir / "P2-FEAT-20260527-190923-prd-from-issue.md"
    upstream_prd.write_text(
        "# PRD: Upstream\n\n"
        "- GitHub Issue: https://github.com/example/repo/issues/77\n",
        encoding="utf-8",
    )
    downstream_prd = task_dir / "P2-FEAT-20260528-110730-prd-review.md"
    downstream_prd.write_text(
        "# PRD: Downstream\n\n"
        "## Delivery Dependencies\n\n"
        "- Group: downstream-group\n"
        "- Depends on groups:\n"
        "- Depends on tasks/issues:\n"
        "  - P2-FEAT-20260527-190923-prd-from-issue\n"
        "- Gate type: hard\n",
        encoding="utf-8",
    )

    create_issue_from_prd(
        request=_request(
            repo,
            Path("tasks/pending/P2-FEAT-20260528-110730-prd-review.md"),
        ),
        github_client=fake_client,
    )

    create_calls = [c for c in fake_client.calls if c["method"] == "create_issue"]
    assert "<!-- iar:depends-on #77 -->" in create_calls[0]["body"]
    assert "task-group/downstream-group" not in create_calls[0]["labels"]


def test_create_issue_from_prd_materializes_prd_ref_group_fallback(
    tmp_path: Path,
) -> None:
    """PRD refs without Issue links should resolve to the upstream PRD group."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    task_dir = repo / "tasks" / "pending"
    task_dir.mkdir(parents=True)
    upstream_prd = task_dir / "P2-FEAT-20260527-190923-prd-from-issue.md"
    upstream_prd.write_text(
        "# PRD: Upstream\n\n"
        "## Delivery Dependencies\n\n"
        "- Group: prd-from-issue-generation\n"
        "- Depends on groups:\n"
        "- Depends on tasks/issues:\n"
        "- Gate type: none\n",
        encoding="utf-8",
    )
    downstream_prd = task_dir / "P2-FEAT-20260528-110730-prd-review.md"
    downstream_prd.write_text(
        "# PRD: Downstream\n\n"
        "## Delivery Dependencies\n\n"
        "- Depends on groups:\n"
        "- Depends on tasks/issues:\n"
        "  - tasks/pending/P2-FEAT-20260527-190923-prd-from-issue.md\n"
        "- Gate type: hard\n",
        encoding="utf-8",
    )

    create_issue_from_prd(
        request=_request(
            repo,
            Path("tasks/pending/P2-FEAT-20260528-110730-prd-review.md"),
        ),
        github_client=fake_client,
    )

    create_calls = [c for c in fake_client.calls if c["method"] == "create_issue"]
    assert (
        "<!-- iar:depends-on group:prd-from-issue-generation -->"
        in create_calls[0]["body"]
    )


def test_create_issue_from_prd_unresolved_prd_ref_is_actionable(
    tmp_path: Path,
) -> None:
    """Unresolved PRD refs should tell the operator how to fix the field."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    prd = repo / "tasks" / "pending" / "downstream.md"
    prd.parent.mkdir(parents=True)
    prd.write_text(
        "# PRD: Downstream\n\n"
        "## Delivery Dependencies\n\n"
        "- Depends on tasks/issues: missing-upstream-prd\n"
        "- Gate type: hard\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        create_issue_from_prd(
            request=_request(repo, Path("tasks/pending/downstream.md")),
            github_client=fake_client,
        )

    message = str(exc_info.value)
    assert "Could not resolve PRD dependency reference" in message
    assert "Use a GitHub Issue number" in message
    assert "repo-relative PRD path" in message
    assert [c for c in fake_client.calls if c["method"] == "create_issue"] == []


def test_create_issue_from_prd_ambiguous_prd_ref_is_actionable(
    tmp_path: Path,
) -> None:
    """Ambiguous filename refs should ask for a repo-relative path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    pending_dir = repo / "tasks" / "pending"
    archive_dir = repo / "tasks" / "archive"
    pending_dir.mkdir(parents=True)
    archive_dir.mkdir(parents=True)
    filename = "P2-FEAT-20260527-190923-prd-from-issue.md"
    (pending_dir / filename).write_text("# PRD: Pending\n", encoding="utf-8")
    (archive_dir / filename).write_text("# PRD: Archive\n", encoding="utf-8")

    prd = pending_dir / "downstream.md"
    prd.write_text(
        "# PRD: Downstream\n\n"
        "## Delivery Dependencies\n\n"
        "- Depends on tasks/issues: P2-FEAT-20260527-190923-prd-from-issue\n"
        "- Gate type: hard\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        create_issue_from_prd(
            request=_request(repo, Path("tasks/pending/downstream.md")),
            github_client=fake_client,
        )

    message = str(exc_info.value)
    assert "Ambiguous PRD dependency reference" in message
    assert "Use a repo-relative path" in message
    assert "tasks/pending/P2-FEAT-20260527-190923-prd-from-issue.md" in message


def test_create_issue_from_prd_rejects_self_referential_prd_ref(
    tmp_path: Path,
) -> None:
    """A PRD that depends on itself should fail fast as a self-dependency."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    prd = repo / "tasks" / "pending" / "downstream.md"
    prd.parent.mkdir(parents=True)
    prd.write_text(
        "# PRD: Downstream\n\n"
        "## Delivery Dependencies\n\n"
        "- Depends on tasks/issues: tasks/pending/downstream.md\n"
        "- Gate type: hard\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        create_issue_from_prd(
            request=_request(repo, Path("tasks/pending/downstream.md")),
            github_client=fake_client,
        )

    message = str(exc_info.value)
    assert "it resolves to the current PRD" in message
    assert "self-dependency" in message
    assert [c for c in fake_client.calls if c["method"] == "create_issue"] == []


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


def test_prompt_publish_on_yes(tmp_path: Path) -> None:
    """Interactive prompt should publish PRD when user answers yes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()
    process_runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain"): _command_result(
                ["git", "status", "--porcelain"],
                stdout=" M tasks/pending/example.md\n",
            ),
            ("git", "branch", "--show-current"): _command_result(
                ["git", "branch", "--show-current"], stdout="main\n"
            ),
        }
    )

    with patch("builtins.input", return_value="y"):
        published = _prompt_and_publish_prd_if_needed(
            repo_path=repo,
            relative_prd_path=Path("tasks/pending/example.md"),
            issue_url="https://github.com/example/repo/issues/42",
            queue_ready=True,
            git_remote="origin",
            labels_config=LabelConfig(),
            github_client=fake_client,
            process_runner=process_runner,
        )

    assert published is True
    assert ["git", "status", "--porcelain"] in process_runner.calls
    assert ["git", "push", "origin", "main"] in process_runner.calls
    assert fake_client.calls[-1] == {
        "method": "edit_issue_labels",
        "issue_number": 42,
        "add": ["agent/ready"],
        "remove": [],
    }


def test_prompt_publish_on_no(tmp_path: Path) -> None:
    """Interactive prompt should skip publishing when user answers no."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()
    process_runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain"): _command_result(
                ["git", "status", "--porcelain"],
                stdout=" M tasks/pending/example.md\n",
            ),
        }
    )

    with patch("builtins.input", return_value="n"):
        published = _prompt_and_publish_prd_if_needed(
            repo_path=repo,
            relative_prd_path=Path("tasks/pending/example.md"),
            issue_url="https://github.com/example/repo/issues/42",
            queue_ready=True,
            git_remote="origin",
            labels_config=LabelConfig(),
            github_client=fake_client,
            process_runner=process_runner,
        )

    assert published is False
    assert process_runner.calls == [["git", "status", "--porcelain"]]
    assert [
        call for call in fake_client.calls if call["method"] == "edit_issue_labels"
    ] == []


def test_prompt_publish_clean_worktree(tmp_path: Path) -> None:
    """Interactive prompt should not prompt when working tree is clean."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()
    process_runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain"): _command_result(
                ["git", "status", "--porcelain"], stdout=""
            ),
        }
    )

    published = _prompt_and_publish_prd_if_needed(
        repo_path=repo,
        relative_prd_path=Path("tasks/pending/example.md"),
        issue_url="https://github.com/example/repo/issues/42",
        queue_ready=True,
        git_remote="origin",
        labels_config=LabelConfig(),
        github_client=fake_client,
        process_runner=process_runner,
    )

    assert published is False
    assert process_runner.calls == [["git", "status", "--porcelain"]]
    assert [
        call for call in fake_client.calls if call["method"] == "edit_issue_labels"
    ] == []


def test_create_issue_from_prd_with_generated_content_template(tmp_path: Path) -> None:
    """Template mode generated content should be used when valid."""
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

    gc_config = GeneratedContentConfig(
        enabled=True,
        issue_from_prd=GeneratedContentTargetConfig(
            enabled=True,
            mode="template",
            title_template="[{issue_type}] {prd_title}",
            body_template=(
                "## Summary\n\n{prd_introduction}\n\n"
                "- PRD path: `{relative_prd_path}`\n\n"
                "{acceptance_items}"
            ),
        ),
    )

    issue_url = create_issue_from_prd(
        request=_request(
            repo,
            Path("tasks/20260516-120000-prd-example.md"),
            generated_content_config=gc_config,
        ),
        github_client=fake_client,
    )

    assert issue_url == "https://github.com/example/repo/issues/42"
    create_calls = [c for c in fake_client.calls if c["method"] == "create_issue"]
    assert len(create_calls) == 1
    assert create_calls[0]["title"] == "[feature] Example"
    assert (
        "- PRD path: `tasks/20260516-120000-prd-example.md`" in create_calls[0]["body"]
    )


def test_create_issue_from_prd_generated_content_fallback_on_invalid(
    tmp_path: Path,
) -> None:
    """Invalid generated content should fallback to deterministic template."""
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

    gc_config = GeneratedContentConfig(
        enabled=True,
        issue_from_prd=GeneratedContentTargetConfig(
            enabled=True,
            mode="template",
            title_template="Title",
            body_template="Missing anchor.",
        ),
    )

    issue_url = create_issue_from_prd(
        request=_request(
            repo,
            Path("tasks/20260516-120000-prd-example.md"),
            generated_content_config=gc_config,
        ),
        github_client=fake_client,
    )

    assert issue_url == "https://github.com/example/repo/issues/42"
    create_calls = [c for c in fake_client.calls if c["method"] == "create_issue"]
    assert len(create_calls) == 1
    assert (
        "- PRD path: `tasks/20260516-120000-prd-example.md`" in create_calls[0]["body"]
    )


def test_create_issue_from_prd_agent_mode_generates_content(tmp_path: Path) -> None:
    """Agent mode should generate Issue content and use it when valid."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()
    generator = FakeContentGenerator(
        response='{"title": "AI Title", "body": "- PRD path: `tasks/example.md`\\n\\nAI body."}'
    )

    prd = repo / "tasks" / "example.md"
    prd.parent.mkdir()
    prd.write_text("# PRD: Example\n", encoding="utf-8")

    gc_config = GeneratedContentConfig(
        enabled=True,
        issue_from_prd=GeneratedContentTargetConfig(
            enabled=True,
            mode="agent",
            output="json",
            prompt="Generate Issue for {relative_prd_path}",
        ),
    )

    issue_url = create_issue_from_prd(
        request=_request(
            repo,
            Path("tasks/example.md"),
            generated_content_config=gc_config,
        ),
        github_client=fake_client,
        content_generator=generator,
    )

    assert issue_url == "https://github.com/example/repo/issues/42"
    create_calls = [c for c in fake_client.calls if c["method"] == "create_issue"]
    assert len(create_calls) == 1
    assert create_calls[0]["title"] == "AI Title"
    assert create_calls[0]["body"] == "- PRD path: `tasks/example.md`\n\nAI body."


def test_create_issue_from_prd_disabled_uses_fallback(tmp_path: Path) -> None:
    """When generated content is disabled, deterministic template should be used."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    prd = repo / "tasks" / "example.md"
    prd.parent.mkdir()
    prd.write_text("# PRD: Example\n", encoding="utf-8")

    gc_config = GeneratedContentConfig(enabled=False)

    issue_url = create_issue_from_prd(
        request=_request(
            repo,
            Path("tasks/example.md"),
            generated_content_config=gc_config,
        ),
        github_client=fake_client,
    )

    assert issue_url == "https://github.com/example/repo/issues/42"
    create_calls = [c for c in fake_client.calls if c["method"] == "create_issue"]
    assert len(create_calls) == 1
    assert "- PRD path: `tasks/example.md`" in create_calls[0]["body"]


def test_build_issue_body_falls_back_to_first_h2_section() -> None:
    """When PRD introduction keyword does not match, use the first ## section."""
    body = build_issue_body(
        relative_prd_path=Path("tasks/example.md"),
        title="[Feature] Example",
        acceptance_items=["- [ ] item"],
        prd_text=(
            "# PRD: Example\n\n"
            "## 1. 背景与目标\n\n"
            "Background content here.\n\n"
            "## 2. 需求形态\n\n"
            "Requirements here.\n"
        ),
    )
    assert "Background content here." in body
    assert "## Summary" in body
    assert "- PRD path: `tasks/example.md`" in body


def test_create_issue_from_prd_materializes_structured_evidence_marker(
    tmp_path: Path,
) -> None:
    """PRDs with Realistic Validation produce the structured evidence marker."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    prd = repo / "tasks" / "pending" / "structured.md"
    prd.parent.mkdir(parents=True)
    prd.write_text(
        "# PRD: Structured\n\n"
        "### Realistic Validation\n\n"
        "- [ ] Item A\n"
        "- [ ] Item B\n",
        encoding="utf-8",
    )

    create_issue_from_prd(
        request=_request(repo, Path("tasks/pending/structured.md")),
        github_client=fake_client,
    )

    create_call = next(c for c in fake_client.calls if c["method"] == "create_issue")
    assert (
        '<!-- iar:structured-evidence version=1 language="zh-CN" -->'
        in create_call["body"]
    )


def test_create_issue_from_prd_honors_validation_language(
    tmp_path: Path,
) -> None:
    """The structured evidence marker reflects the configured validation language."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    prd = repo / "tasks" / "pending" / "structured.md"
    prd.parent.mkdir(parents=True)
    prd.write_text(
        "# PRD: Structured\n\n" "### Realistic Validation\n\n" "- [ ] Item A\n",
        encoding="utf-8",
    )

    create_issue_from_prd(
        request=_request(
            repo,
            Path("tasks/pending/structured.md"),
            validation_language="en-US",
        ),
        github_client=fake_client,
    )

    create_call = next(c for c in fake_client.calls if c["method"] == "create_issue")
    assert (
        '<!-- iar:structured-evidence version=1 language="en-US" -->'
        in create_call["body"]
    )


def test_create_issue_from_prd_skips_marker_when_structured_evidence_disabled(
    tmp_path: Path,
) -> None:
    """When structured evidence is disabled, no marker is materialized."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    prd = repo / "tasks" / "pending" / "structured.md"
    prd.parent.mkdir(parents=True)
    prd.write_text(
        "# PRD: Structured\n\n" "### Realistic Validation\n\n" "- [ ] Item A\n",
        encoding="utf-8",
    )

    create_issue_from_prd(
        request=_request(
            repo,
            Path("tasks/pending/structured.md"),
            structured_evidence=False,
        ),
        github_client=fake_client,
    )

    create_call = next(c for c in fake_client.calls if c["method"] == "create_issue")
    assert "iar:structured-evidence" not in create_call["body"]
