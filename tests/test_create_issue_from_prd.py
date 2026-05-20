"""Tests for PRD-driven Issue creation."""

from __future__ import annotations

import subprocess
from pathlib import Path

from backend.core.use_cases.create_issue_from_prd import create_issue_from_prd
from tests.conftest import FakeGitHubClient


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a command for test setup."""
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8")


def _init_repo(path: Path) -> None:
    """Initialize a git repository."""
    _run(["git", "init", "-b", "main"], path)
    _run(["git", "config", "user.name", "Test"], path)
    _run(["git", "config", "user.email", "test@example.com"], path)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-m", "init"], path)


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
        repo_path=repo,
        prd_path=Path("tasks/20260516-120000-prd-example.md"),
        issue_type="feature",
        github_client=fake_client,
    )

    assert issue_url == "https://github.com/example/repo/issues/42"
    assert "- GitHub Issue: https://github.com/example/repo/issues/42" in prd.read_text(encoding="utf-8")
    create_calls = [c for c in fake_client.calls if c["method"] == "create_issue"]
    assert len(create_calls) == 1
    call = create_calls[0]
    assert call["labels"] == ["type/feature", "status/backlog", "source/prd", "agent/ready"]


def test_create_issue_from_prd_with_agent_label(tmp_path: Path) -> None:
    """Agent routing label should be applied when explicitly requested."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    fake_client = FakeGitHubClient()

    prd = repo / "prd.md"
    prd.write_text("# PRD: Test\n", encoding="utf-8")

    create_issue_from_prd(
        repo_path=repo,
        prd_path=Path("prd.md"),
        issue_type="bug",
        issue_agent="claude",
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
        repo_path=repo,
        prd_path=Path("prd.md"),
        issue_type="feature",
        force=True,
        github_client=fake_client,
    )

    prd_text = prd.read_text(encoding="utf-8")
    assert "https://old.url" not in prd_text
    assert "https://github.com/example/repo/issues/42" in prd_text
