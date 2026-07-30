"""Tests for publishing agent work to the remote.

Covers ``get_head_sha``, ``validate_safe_changes`` and ``publish_changes``:
remote preflight, branch guards, PR reuse/creation, generated PR bodies and
push-vs-PR failure categorization."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    GeneratedContentConfig,
    GeneratedContentTargetConfig,
    GitConfig,
    IssueSummary,
    WorktreeConfig,
)
from backend.core.use_cases.run_agent_once import (
    get_head_sha,
    publish_changes,
    validate_safe_changes,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner
from tests.support.agent_runner import (
    git_remote_command,
    git_remote_result,
)


def test_get_head_sha() -> None:
    """get_head_sha should return the HEAD SHA."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123def456\n",
                stderr="",
            ),
        }
    )
    sha = get_head_sha(Path("."), fake_runner)
    assert sha == "abc123def456"


def test_publish_changes_no_git_commit() -> None:
    """publish_changes should not call git add or git commit."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            git_remote_command(): git_remote_result("origin"),
        }
    )
    branch, pr_url = publish_changes(issue, Path("."), AppConfig(), fake_client, fake_runner)
    assert branch == "issue-1"
    assert pr_url == "https://github.com/example/repo/pull/1"
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "add", "-A") not in commands
    assert ("git", "commit", "-m", "agent: complete issue #1") not in commands
    assert ("git", "push", "-u", "origin", "issue-1") in commands


def test_publish_changes_reuses_existing_open_pr() -> None:
    """publish_changes should reuse an existing open PR instead of recreating it."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    fake_client = FakeGitHubClient()
    fake_client._open_prs["issue-1"] = "https://github.com/example/repo/pull/52"
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            git_remote_command(): git_remote_result("origin"),
        }
    )
    branch, pr_url = publish_changes(issue, Path("."), AppConfig(), fake_client, fake_runner)
    assert branch == "issue-1"
    assert pr_url == "https://github.com/example/repo/pull/52"
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "push", "-u", "origin", "issue-1") in commands
    method_names = [call["method"] for call in fake_client.calls]
    assert "find_open_pr_by_head" in method_names
    assert "create_draft_pr" not in method_names


def test_publish_changes_creates_pr_when_no_open_pr() -> None:
    """publish_changes should create a draft PR when the branch has no open PR."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            git_remote_command(): git_remote_result("origin"),
        }
    )
    branch, pr_url = publish_changes(issue, Path("."), AppConfig(), fake_client, fake_runner)
    assert branch == "issue-1"
    assert pr_url == "https://github.com/example/repo/pull/1"
    method_names = [call["method"] for call in fake_client.calls]
    assert "find_open_pr_by_head" in method_names
    assert "create_draft_pr" in method_names


def test_publish_changes_rejects_missing_configured_remote() -> None:
    """publish_changes should fail instead of guessing another remote."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            git_remote_command(): git_remote_result("zata", "upstream"),
        }
    )

    with pytest.raises(RuntimeError, match="Configured git remote 'origin'"):
        publish_changes(issue, Path("."), AppConfig(), fake_client, fake_runner)

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "push", "-u", "origin", "issue-1") not in commands
    assert ("git", "push", "-u", "zata", "issue-1") not in commands


def test_publish_changes_uses_configured_existing_remote() -> None:
    """publish_changes should push only to the configured remote."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            git_remote_command(): git_remote_result("origin", "zata"),
        }
    )
    config = AppConfig(git=GitConfig(remote="zata"))

    branch, _ = publish_changes(issue, Path("."), config, fake_client, fake_runner)

    assert branch == "issue-1"
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "push", "-u", "zata", "issue-1") in commands
    assert ("git", "push", "-u", "origin", "issue-1") not in commands


def test_publish_changes_rejects_branch_change() -> None:
    """publish_changes should refuse to push if the worktree branch changed."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="main\n",
                stderr="",
            ),
        }
    )

    with pytest.raises(RuntimeError, match="unexpected branch: main"):
        publish_changes(
            issue,
            Path("."),
            AppConfig(),
            FakeGitHubClient(),
            fake_runner,
            expected_branch="issue-1",
        )

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "status", "--porcelain") not in commands
    assert ("git", "push", "-u", "origin", "main") not in commands


def test_publish_changes_rejects_detached_head(tmp_path: Path) -> None:
    """Publish must refuse to push when the worktree is detached."""
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="",
                stderr="",
            ),
        }
    )
    config = AppConfig(
        worktree=WorktreeConfig(
            create_command="echo",
            reuse_command="echo",
            path_command="echo",
        )
    )

    with pytest.raises(RuntimeError, match="detached HEAD"):
        publish_changes(
            issue=IssueSummary(number=1, title="T", url="U", body="B", labels=()),
            worktree_path=tmp_path / "wt",
            config=config,
            github_client=fake_client,
            process_runner=fake_runner,
        )


def test_publish_failure_category_push_vs_pr_create() -> None:
    """Pre-PR flow: push and PR create are gated separately and report accurately."""
    from backend.core.use_cases.agent_runner_failure import PublishFailureError
    from backend.core.use_cases.agent_runner_publication import (
        _create_draft_pr_with_recovery_context,
        _push_changes_with_recovery_context,
    )
    from backend.core.shared.models.agent_runner import PublishFailureCategory

    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )

    # Scenario 1: git push fails -> category=push
    fake_runner_push_fail = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "remote"): CommandResult(
                command=("git", "remote"),
                return_code=0,
                stdout="origin\n",
                stderr="",
            ),
            ("git", "push", "-u", "origin", "issue-1"): CommandResult(
                command=("git", "push", "-u", "origin", "issue-1"),
                return_code=1,
                stdout="",
                stderr="push rejected",
            ),
        }
    )
    with pytest.raises(PublishFailureError) as exc_info:
        _push_changes_with_recovery_context(
            issue=issue,
            worktree_path=Path("."),
            config=AppConfig(),
            process_runner=fake_runner_push_fail,
            expected_branch="issue-1",
        )
    assert exc_info.value.failure_category == PublishFailureCategory.PUSH

    # Scenario 2: PR creation fails -> category=pr_create
    class _PRCreateFailClient(FakeGitHubClient):
        def create_draft_pr(self, **kwargs: object) -> str:
            raise RuntimeError("gh pr create failed")

    fake_runner_pr_fail = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
        }
    )
    with pytest.raises(PublishFailureError) as exc_info:
        _create_draft_pr_with_recovery_context(
            issue=issue,
            worktree_path=Path("."),
            config=AppConfig(),
            github_client=_PRCreateFailClient(),
            process_runner=fake_runner_pr_fail,
            expected_branch="issue-1",
            content_generator=None,
        )
    assert exc_info.value.failure_category == PublishFailureCategory.PR_CREATE


def test_publish_validation_evidence_after_pr_is_best_effort() -> None:
    """Unlike push/PR-create, an evidence-comment failure must not raise.

    Regression for a production incident: push, PR create, and the label
    transition to ``agent/supervising`` had already succeeded; a transient
    GitHub-edge error on the trailing evidence comment then rolled the
    Issue all the way back to ``agent/failed``, discarding that good state
    over what is purely an audit-trail comment.
    """
    from unittest.mock import patch

    from backend.core.use_cases.agent_runner_publication import (
        _publish_validation_evidence_after_pr,
    )

    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )

    with patch(
        "backend.core.use_cases.agent_runner_validation.publish_validation_evidence",
        side_effect=RuntimeError('non-200 OK status code: 499  body: ""'),
    ):
        _publish_validation_evidence_after_pr(
            issue=issue,
            worktree_path=Path("."),
            config=AppConfig(),
            github_client=FakeGitHubClient(),
            process_runner=FakeProcessRunner(),
            pr_url="https://github.com/example/repo/pull/1",
        )


def test_validate_safe_changes_rejects_forbidden_path(tmp_path: Path) -> None:
    """Runner should not publish configured secret-like paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    from tests.test_create_issue_from_prd import _init_repo

    _init_repo(repo)
    (repo / ".env").write_text("SECRET=value\n", encoding="utf-8")

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain", "-z"): CommandResult(
                command=("git", "status", "--porcelain", "-z"),
                return_code=0,
                stdout=" M .env\0",
                stderr="",
            ),
        }
    )

    with pytest.raises(RuntimeError, match="Refusing to publish forbidden paths: .env"):
        validate_safe_changes(repo, AppConfig(), fake_runner)


def test_publish_changes_generated_pr_template_mode() -> None:
    """Template mode should render PR title and body when enabled."""
    issue = IssueSummary(
        number=42,
        title="Test Feature",
        url="https://github.com/example/repo/issues/42",
        body="Test body",
        labels=(),
    )
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-42\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            git_remote_command(): git_remote_result("origin"),
            ("git", "log", "main..HEAD", "--pretty=format:%s"): CommandResult(
                command=("git", "log", "main..HEAD", "--pretty=format:%s"),
                return_code=0,
                stdout="feat: implement feature\n",
                stderr="",
            ),
            ("git", "diff", "--stat", "main...HEAD"): CommandResult(
                command=("git", "diff", "--stat", "main...HEAD"),
                return_code=0,
                stdout="1 file changed, 10 insertions\n",
                stderr="",
            ),
        }
    )
    gc_config = GeneratedContentConfig(
        enabled=True,
        draft_pr=GeneratedContentTargetConfig(
            enabled=True,
            mode="template",
            title_template="[Agent] {issue_title}",
            body_template="Closes #{issue_number}\n\n{commit_log}\n\n{diff_stat}",
            include_commit_log=True,
            include_diff_stat=True,
        ),
    )
    from backend.core.shared.models.agent_runner import AppConfig, GitConfig

    app_config = AppConfig(
        git=GitConfig(remote="origin", base_branch="main"),
        generated_content=gc_config,
    )

    branch, pr_url = publish_changes(issue, Path("."), app_config, fake_client, fake_runner)

    assert branch == "issue-42"
    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1
    assert pr_calls[0]["title"] == "[Agent] Test Feature"
    assert "Closes #42" in pr_calls[0]["body"]
    assert "feat: implement feature" in pr_calls[0]["body"]


def test_publish_changes_generated_pr_fallback_on_missing_closes() -> None:
    """Generated PR missing Closes anchor should fallback to deterministic template."""
    issue = IssueSummary(
        number=42,
        title="Test Feature",
        url="https://github.com/example/repo/issues/42",
        body="Test body",
        labels=(),
    )
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-42\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            git_remote_command(): git_remote_result("origin"),
        }
    )
    gc_config = GeneratedContentConfig(
        enabled=True,
        draft_pr=GeneratedContentTargetConfig(
            enabled=True,
            mode="template",
            title_template="Title",
            body_template="No closes here.",
        ),
    )
    from backend.core.shared.models.agent_runner import AppConfig, GitConfig

    app_config = AppConfig(
        git=GitConfig(remote="origin", base_branch="main"),
        generated_content=gc_config,
    )

    branch, pr_url = publish_changes(issue, Path("."), app_config, fake_client, fake_runner)

    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1
    assert pr_calls[0]["title"] == "[Agent] Test Feature"
    assert "Closes #42" in pr_calls[0]["body"]


def test_publish_changes_disabled_uses_fallback() -> None:
    """When generated content is disabled, deterministic PR body should be used."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            git_remote_command(): git_remote_result("origin"),
        }
    )
    gc_config = GeneratedContentConfig(enabled=False)
    from backend.core.shared.models.agent_runner import AppConfig, GitConfig

    app_config = AppConfig(
        git=GitConfig(remote="origin", base_branch="main"),
        generated_content=gc_config,
    )

    branch, pr_url = publish_changes(issue, Path("."), app_config, fake_client, fake_runner)

    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1
    assert pr_calls[0]["title"] == "[Agent] Test"
    assert "Closes #1" in pr_calls[0]["body"]
