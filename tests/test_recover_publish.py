"""Tests for publish recovery use case."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    LabelConfig,
    PostPrSupervisorConfig,
    PullRequestContext,
)
from backend.core.use_cases.recover_publish import (
    PublishRecoveryError,
    PublishRecoveryRequest,
    build_recovery_success_comment,
    recover_publish_issue,
    resolve_existing_worktree,
    validate_branch_safety,
    validate_worktree_clean,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def _make_config(*, supervisor_enabled: bool = False) -> AppConfig:
    """Create a minimal test configuration."""
    return AppConfig(
        labels=LabelConfig(
            ready="agent/ready",
            running="agent/running",
            supervising="agent/supervising",
            review="agent/review",
            failed="agent/failed",
        ),
        post_pr_supervisor=PostPrSupervisorConfig(enabled=supervisor_enabled),
    )


def _make_process_runner_with_worktree(
    worktree_path: Path,
    *,
    branch: str = "issue-42",
    has_changes: bool = False,
    remote_exists: bool = True,
    push_succeeds: bool = True,
) -> FakeProcessRunner:
    """Create a process runner that simulates a valid worktree."""
    responses = {
        ("git", "rev-parse", "--git-dir"): type(
            "R",
            (),
            {"command": ("git",), "return_code": 0, "stdout": ".git", "stderr": ""},
        )(),
        ("git", "status", "--porcelain"): type(
            "R",
            (),
            {
                "command": ("git",),
                "return_code": 0,
                "stdout": "M file.txt\n" if has_changes else "",
                "stderr": "",
            },
        )(),
        ("git", "branch", "--show-current"): type(
            "R",
            (),
            {"command": ("git",), "return_code": 0, "stdout": branch, "stderr": ""},
        )(),
        ("git", "rev-parse", "HEAD"): type(
            "R",
            (),
            {
                "command": ("git",),
                "return_code": 0,
                "stdout": "abc123def456",
                "stderr": "",
            },
        )(),
        ("git", "remote"): type(
            "R",
            (),
            {
                "command": ("git",),
                "return_code": 0,
                "stdout": "origin\nupstream\n" if remote_exists else "",
                "stderr": "",
            },
        )(),
        ("git", "push", "-u", "origin", branch): type(
            "R",
            (),
            {
                "command": ("git",),
                "return_code": 0 if push_succeeds else 1,
                "stdout": "",
                "stderr": "" if push_succeeds else "Push failed",
            },
        )(),
    }
    runner = FakeProcessRunner(responses)
    return runner


class TestResolveExistingWorktree:
    """Tests for resolve_existing_worktree."""

    def test_raises_when_worktree_does_not_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Should raise when the resolved worktree path does not exist."""
        nonexistent_path = tmp_path / "nonexistent"
        config = _make_config()
        # Configure path_command to return non-existent path
        config = AppConfig(
            worktree=config.worktree.__class__(path_command=f"echo {nonexistent_path}")
        )
        responses = {
            ("echo", str(nonexistent_path)): type(
                "R",
                (),
                {
                    "command": ("echo",),
                    "return_code": 0,
                    "stdout": str(nonexistent_path),
                    "stderr": "",
                },
            )(),
        }
        runner = FakeProcessRunner(responses)

        with pytest.raises(PublishRecoveryError) as exc_info:
            resolve_existing_worktree(tmp_path, 42, config, runner)

        assert "does not exist" in str(exc_info.value)


class TestValidateWorktreeClean:
    """Tests for validate_worktree_clean."""

    def test_passes_when_worktree_is_clean(self, tmp_path: Path) -> None:
        """Should pass when git status --porcelain is empty."""
        runner = _make_process_runner_with_worktree(tmp_path, has_changes=False)
        # Should not raise
        validate_worktree_clean(tmp_path, runner)

    def test_raises_when_worktree_has_changes(self, tmp_path: Path) -> None:
        """Should raise when git status --porcelain is non-empty."""
        runner = _make_process_runner_with_worktree(tmp_path, has_changes=True)

        with pytest.raises(PublishRecoveryError) as exc_info:
            validate_worktree_clean(tmp_path, runner)

        assert "uncommitted changes" in str(exc_info.value)


class TestValidateBranchSafety:
    """Tests for validate_branch_safety."""

    def test_raises_on_base_branch(self, tmp_path: Path) -> None:
        """Should refuse to publish from base branch."""
        config = _make_config()
        runner = _make_process_runner_with_worktree(tmp_path, branch="main")

        with pytest.raises(PublishRecoveryError) as exc_info:
            validate_branch_safety(
                worktree_path=tmp_path,
                issue_number=42,
                config=config,
                process_runner=runner,
            )

        assert "base branch" in str(exc_info.value)

    def test_raises_on_detached_head(self, tmp_path: Path) -> None:
        """Should refuse to publish from detached HEAD."""
        config = _make_config()
        responses = {
            ("git", "branch", "--show-current"): type(
                "R",
                (),
                {"command": ("git",), "return_code": 0, "stdout": "", "stderr": ""},
            )(),
        }
        runner = FakeProcessRunner(responses)

        with pytest.raises(PublishRecoveryError) as exc_info:
            validate_branch_safety(
                worktree_path=tmp_path,
                issue_number=42,
                config=config,
                process_runner=runner,
            )

        assert "detached HEAD" in str(exc_info.value)

    def test_passes_when_branch_matches_issue_number(self, tmp_path: Path) -> None:
        """Should pass when branch name references issue number."""
        config = _make_config()
        runner = _make_process_runner_with_worktree(tmp_path, branch="issue-42")

        result = validate_branch_safety(
            worktree_path=tmp_path,
            issue_number=42,
            config=config,
            process_runner=runner,
        )

        assert result == "issue-42"

    def test_passes_when_explicit_branch_matches(self, tmp_path: Path) -> None:
        """Should pass when explicit --branch matches current branch."""
        config = _make_config()
        runner = _make_process_runner_with_worktree(tmp_path, branch="feature-xyz")

        result = validate_branch_safety(
            worktree_path=tmp_path,
            issue_number=42,
            config=config,
            process_runner=runner,
            expected_branch="feature-xyz",
        )

        assert result == "feature-xyz"

    def test_raises_when_explicit_branch_mismatch(self, tmp_path: Path) -> None:
        """Should raise when explicit --branch does not match current branch."""
        config = _make_config()
        runner = _make_process_runner_with_worktree(tmp_path, branch="feature-xyz")

        with pytest.raises(PublishRecoveryError) as exc_info:
            validate_branch_safety(
                worktree_path=tmp_path,
                issue_number=42,
                config=config,
                process_runner=runner,
                expected_branch="different-branch",
            )

        assert "does not match" in str(exc_info.value)

    def test_raises_on_suspicious_branch_without_flag(self, tmp_path: Path) -> None:
        """Should raise when branch does not reference issue number."""
        config = _make_config()
        runner = _make_process_runner_with_worktree(tmp_path, branch="random-branch")

        with pytest.raises(PublishRecoveryError) as exc_info:
            validate_branch_safety(
                worktree_path=tmp_path,
                issue_number=42,
                config=config,
                process_runner=runner,
            )

        assert "does not appear to reference" in str(exc_info.value)

    def test_rejects_similar_issue_number_prefix(self, tmp_path: Path) -> None:
        """Should reject issue-421 when looking for Issue #42."""
        config = _make_config()
        runner = _make_process_runner_with_worktree(tmp_path, branch="issue-421")

        with pytest.raises(PublishRecoveryError) as exc_info:
            validate_branch_safety(
                worktree_path=tmp_path,
                issue_number=42,
                config=config,
                process_runner=runner,
            )

        assert "does not appear to reference" in str(exc_info.value)

    def test_rejects_feature_issue_420(self, tmp_path: Path) -> None:
        """Should reject feature/issue-420 when looking for Issue #42."""
        config = _make_config()
        runner = _make_process_runner_with_worktree(
            tmp_path, branch="feature/issue-420"
        )

        with pytest.raises(PublishRecoveryError) as exc_info:
            validate_branch_safety(
                worktree_path=tmp_path,
                issue_number=42,
                config=config,
                process_runner=runner,
            )

        assert "does not appear to reference" in str(exc_info.value)

    def test_rejects_task_142(self, tmp_path: Path) -> None:
        """Should reject task-142 when looking for Issue #42."""
        config = _make_config()
        runner = _make_process_runner_with_worktree(tmp_path, branch="task-142")

        with pytest.raises(PublishRecoveryError) as exc_info:
            validate_branch_safety(
                worktree_path=tmp_path,
                issue_number=42,
                config=config,
                process_runner=runner,
            )

        assert "does not appear to reference" in str(exc_info.value)

    def test_explicit_branch_allows_any_name(self, tmp_path: Path) -> None:
        """Explicit --branch should bypass segment check entirely."""
        config = _make_config()
        runner = _make_process_runner_with_worktree(tmp_path, branch="issue-421")

        result = validate_branch_safety(
            worktree_path=tmp_path,
            issue_number=42,
            config=config,
            process_runner=runner,
            expected_branch="issue-421",
        )

        assert result == "issue-421"


class TestRecoverPublishIssue:
    """Tests for recover_publish_issue."""

    def test_success_creates_new_pr_supervisor_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Should create new PR and move to review when supervisor is disabled."""
        config = _make_config(supervisor_enabled=False)
        worktree_path = tmp_path / "issue-42"
        worktree_path.mkdir()

        config = AppConfig(
            labels=LabelConfig(
                ready="agent/ready",
                running="agent/running",
                supervising="agent/supervising",
                review="agent/review",
                failed="agent/failed",
            ),
            worktree=config.worktree.__class__(path_command=f"echo {worktree_path}"),
            post_pr_supervisor=PostPrSupervisorConfig(enabled=False),
        )

        runner = _make_process_runner_with_worktree(worktree_path, branch="issue-42")
        github_client = FakeGitHubClient()
        github_client._open_prs["issue-42"] = None
        github_client._issue_title = "Fix login timeout"

        request = PublishRecoveryRequest(issue_number=42)

        result = recover_publish_issue(
            request=request,
            repo_path=tmp_path,
            config=config,
            github_client=github_client,
            process_runner=runner,
        )

        assert result.issue_number == 42
        assert result.branch == "issue-42"
        assert result.head_sha == "abc123def456"
        assert result.pr_url == "https://github.com/example/repo/pull/1"
        assert result.pr_reused is False
        assert result.supervisor_action == "supervisor_disabled_fallback"

        pr_create_calls = [
            c for c in github_client.calls if c["method"] == "create_draft_pr"
        ]
        assert len(pr_create_calls) == 1
        assert pr_create_calls[0]["title"] == "[Agent] Fix login timeout"

        label_calls = [
            c for c in github_client.calls if c["method"] == "edit_issue_labels"
        ]
        assert len(label_calls) == 1
        assert "agent/review" in label_calls[0]["add"]
        assert "agent/failed" in label_calls[0]["remove"]

    def test_success_uses_fallback_title_when_issue_lookup_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Should fallback to issue-number title when get_issue fails."""
        config = _make_config(supervisor_enabled=False)
        worktree_path = tmp_path / "issue-42"
        worktree_path.mkdir()

        config = AppConfig(
            labels=LabelConfig(
                ready="agent/ready",
                running="agent/running",
                supervising="agent/supervising",
                review="agent/review",
                failed="agent/failed",
            ),
            worktree=config.worktree.__class__(path_command=f"echo {worktree_path}"),
            post_pr_supervisor=PostPrSupervisorConfig(enabled=False),
        )

        runner = _make_process_runner_with_worktree(worktree_path, branch="issue-42")
        github_client = FakeGitHubClient()
        github_client._open_prs["issue-42"] = None

        # Make get_issue raise so recovered_issue becomes None
        def _raise_get_issue(issue_number: int) -> None:
            raise RuntimeError("network error")

        github_client.get_issue = _raise_get_issue

        request = PublishRecoveryRequest(issue_number=42)

        result = recover_publish_issue(
            request=request,
            repo_path=tmp_path,
            config=config,
            github_client=github_client,
            process_runner=runner,
        )

        assert result.issue_number == 42

        pr_create_calls = [
            c for c in github_client.calls if c["method"] == "create_draft_pr"
        ]
        assert len(pr_create_calls) == 1
        assert pr_create_calls[0]["title"] == "[Agent] Issue #42"

    def test_success_supervisor_enabled_goes_to_supervising(
        self, tmp_path: Path
    ) -> None:
        """Should move to supervising when supervisor is enabled."""
        config = _make_config(supervisor_enabled=True)
        worktree_path = tmp_path / "issue-42"
        worktree_path.mkdir()

        config = AppConfig(
            labels=LabelConfig(
                ready="agent/ready",
                running="agent/running",
                supervising="agent/supervising",
                review="agent/review",
                failed="agent/failed",
            ),
            worktree=config.worktree.__class__(path_command=f"echo {worktree_path}"),
            post_pr_supervisor=PostPrSupervisorConfig(enabled=True),
        )

        runner = _make_process_runner_with_worktree(worktree_path, branch="issue-42")
        github_client = FakeGitHubClient()
        github_client._open_prs["issue-42"] = None
        # Set up PR context so supervisor can run
        github_client._pr_contexts["issue-42"] = PullRequestContext(
            pr_url="https://github.com/example/repo/pull/1",
            branch="issue-42",
            head_sha="abc123def456",
            base_sha="base-sha",
        )

        request = PublishRecoveryRequest(issue_number=42)

        result = recover_publish_issue(
            request=request,
            repo_path=tmp_path,
            config=config,
            github_client=github_client,
            process_runner=runner,
        )

        assert result.issue_number == 42
        assert result.branch == "issue-42"
        # When supervisor is enabled but no agent is available to run,
        # the supervisor cycle will fail; however labels should still be supervising
        label_calls = [
            c for c in github_client.calls if c["method"] == "edit_issue_labels"
        ]
        # First label call moves to supervising
        assert any("agent/supervising" in c["add"] for c in label_calls)

    def test_success_reuses_existing_pr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Should reuse existing PR when one exists."""
        config = _make_config(supervisor_enabled=False)
        worktree_path = tmp_path / "issue-42"
        worktree_path.mkdir()

        config = AppConfig(
            labels=LabelConfig(
                ready="agent/ready",
                running="agent/running",
                supervising="agent/supervising",
                review="agent/review",
                failed="agent/failed",
            ),
            worktree=config.worktree.__class__(path_command=f"echo {worktree_path}"),
            post_pr_supervisor=PostPrSupervisorConfig(enabled=False),
        )

        runner = _make_process_runner_with_worktree(worktree_path, branch="issue-42")
        github_client = FakeGitHubClient()
        github_client._open_prs["issue-42"] = "https://github.com/example/repo/pull/99"

        request = PublishRecoveryRequest(issue_number=42)

        result = recover_publish_issue(
            request=request,
            repo_path=tmp_path,
            config=config,
            github_client=github_client,
            process_runner=runner,
        )

        assert result.pr_url == "https://github.com/example/repo/pull/99"
        assert result.pr_reused is True

    def test_push_failure_posts_comment_and_does_not_modify_labels(
        self, tmp_path: Path
    ) -> None:
        """Push failure should post a failure comment and not modify labels."""
        config = _make_config(supervisor_enabled=False)
        worktree_path = tmp_path / "issue-42"
        worktree_path.mkdir()

        config = AppConfig(
            labels=LabelConfig(
                ready="agent/ready",
                running="agent/running",
                supervising="agent/supervising",
                review="agent/review",
                failed="agent/failed",
            ),
            worktree=config.worktree.__class__(path_command=f"echo {worktree_path}"),
            post_pr_supervisor=PostPrSupervisorConfig(enabled=False),
        )

        runner = _make_process_runner_with_worktree(
            worktree_path, branch="issue-42", push_succeeds=False
        )
        github_client = FakeGitHubClient()

        request = PublishRecoveryRequest(issue_number=42)

        with pytest.raises(PublishRecoveryError) as exc_info:
            recover_publish_issue(
                request=request,
                repo_path=tmp_path,
                config=config,
                github_client=github_client,
                process_runner=runner,
            )

        assert "Failed to push" in str(exc_info.value)
        assert exc_info.value.failure_category == "push"

        # Should post a failure comment
        comment_calls = [
            c for c in github_client.calls if c["method"] == "comment_issue"
        ]
        assert len(comment_calls) == 1
        assert "Publish Recovery Failed" in comment_calls[0]["body"]
        assert "push" in comment_calls[0]["body"]

        # Should NOT modify labels
        label_calls = [
            c for c in github_client.calls if c["method"] == "edit_issue_labels"
        ]
        assert len(label_calls) == 0

    def test_pr_lookup_failure_posts_comment_and_does_not_modify_labels(
        self, tmp_path: Path
    ) -> None:
        """PR lookup failure should post a failure comment and not modify labels."""
        config = _make_config(supervisor_enabled=False)
        worktree_path = tmp_path / "issue-42"
        worktree_path.mkdir()

        config = AppConfig(
            labels=LabelConfig(
                ready="agent/ready",
                running="agent/running",
                supervising="agent/supervising",
                review="agent/review",
                failed="agent/failed",
            ),
            worktree=config.worktree.__class__(path_command=f"echo {worktree_path}"),
            post_pr_supervisor=PostPrSupervisorConfig(enabled=False),
        )

        runner = _make_process_runner_with_worktree(worktree_path, branch="issue-42")
        github_client = FakeGitHubClient()

        # Make find_open_pr_by_head raise
        def _raise_lookup(branch: str) -> str | None:
            raise RuntimeError("gh CLI error")

        github_client.find_open_pr_by_head = _raise_lookup

        request = PublishRecoveryRequest(issue_number=42)

        with pytest.raises(PublishRecoveryError) as exc_info:
            recover_publish_issue(
                request=request,
                repo_path=tmp_path,
                config=config,
                github_client=github_client,
                process_runner=runner,
            )

        assert "pr_lookup" in exc_info.value.failure_category

        comment_calls = [
            c for c in github_client.calls if c["method"] == "comment_issue"
        ]
        assert len(comment_calls) == 1
        assert "pr_lookup" in comment_calls[0]["body"]

        label_calls = [
            c for c in github_client.calls if c["method"] == "edit_issue_labels"
        ]
        assert len(label_calls) == 0

    def test_pr_create_failure_posts_comment_and_does_not_modify_labels(
        self, tmp_path: Path
    ) -> None:
        """PR create failure should post a failure comment and not modify labels."""
        config = _make_config(supervisor_enabled=False)
        worktree_path = tmp_path / "issue-42"
        worktree_path.mkdir()

        config = AppConfig(
            labels=LabelConfig(
                ready="agent/ready",
                running="agent/running",
                supervising="agent/supervising",
                review="agent/review",
                failed="agent/failed",
            ),
            worktree=config.worktree.__class__(path_command=f"echo {worktree_path}"),
            post_pr_supervisor=PostPrSupervisorConfig(enabled=False),
        )

        runner = _make_process_runner_with_worktree(worktree_path, branch="issue-42")
        github_client = FakeGitHubClient()
        github_client._open_prs["issue-42"] = None

        # Make create_draft_pr raise
        def _raise_create(**kwargs: object) -> str:
            raise RuntimeError("gh pr create failed")

        github_client.create_draft_pr = _raise_create

        request = PublishRecoveryRequest(issue_number=42)

        with pytest.raises(PublishRecoveryError) as exc_info:
            recover_publish_issue(
                request=request,
                repo_path=tmp_path,
                config=config,
                github_client=github_client,
                process_runner=runner,
            )

        assert "pr_create" in exc_info.value.failure_category

        comment_calls = [
            c for c in github_client.calls if c["method"] == "comment_issue"
        ]
        assert len(comment_calls) == 1
        assert "pr_create" in comment_calls[0]["body"]

        label_calls = [
            c for c in github_client.calls if c["method"] == "edit_issue_labels"
        ]
        assert len(label_calls) == 0

    def test_raises_when_remote_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Should raise when configured remote does not exist."""
        config = _make_config(supervisor_enabled=False)
        worktree_path = tmp_path / "issue-42"
        worktree_path.mkdir()

        config = AppConfig(
            labels=LabelConfig(
                ready="agent/ready",
                running="agent/running",
                supervising="agent/supervising",
                review="agent/review",
                failed="agent/failed",
            ),
            worktree=config.worktree.__class__(path_command=f"echo {worktree_path}"),
            post_pr_supervisor=PostPrSupervisorConfig(enabled=False),
        )

        runner = _make_process_runner_with_worktree(
            worktree_path, branch="issue-42", remote_exists=False
        )
        github_client = FakeGitHubClient()

        request = PublishRecoveryRequest(issue_number=42)

        with pytest.raises(PublishRecoveryError) as exc_info:
            recover_publish_issue(
                request=request,
                repo_path=tmp_path,
                config=config,
                github_client=github_client,
                process_runner=runner,
            )

        assert "does not exist" in str(exc_info.value)
        assert exc_info.value.failure_category == "push"

    def test_idempotent_rerun(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Should be safe to rerun after success."""
        config = _make_config(supervisor_enabled=False)
        worktree_path = tmp_path / "issue-42"
        worktree_path.mkdir()

        config = AppConfig(
            labels=LabelConfig(
                ready="agent/ready",
                running="agent/running",
                supervising="agent/supervising",
                review="agent/review",
                failed="agent/failed",
            ),
            worktree=config.worktree.__class__(path_command=f"echo {worktree_path}"),
            post_pr_supervisor=PostPrSupervisorConfig(enabled=False),
        )

        runner = _make_process_runner_with_worktree(worktree_path, branch="issue-42")
        github_client = FakeGitHubClient()
        github_client._open_prs["issue-42"] = "https://github.com/example/repo/pull/99"

        request = PublishRecoveryRequest(issue_number=42)

        # First run
        result1 = recover_publish_issue(
            request=request,
            repo_path=tmp_path,
            config=config,
            github_client=github_client,
            process_runner=runner,
        )

        # Second run - should reuse PR and succeed
        result2 = recover_publish_issue(
            request=request,
            repo_path=tmp_path,
            config=config,
            github_client=github_client,
            process_runner=runner,
        )

        assert result1.pr_url == result2.pr_url
        assert result2.pr_reused is True


class TestBuildRecoverySuccessComment:
    """Tests for build_recovery_success_comment."""

    def test_includes_branch_and_sha(self) -> None:
        """Should include branch and SHA in comment."""
        comment = build_recovery_success_comment(
            branch="issue-42",
            head_sha="abc123",
            pr_url="https://github.com/example/repo/pull/1",
            pr_reused=False,
        )

        assert "`issue-42`" in comment
        assert "`abc123`" in comment
        assert "https://github.com/example/repo/pull/1" in comment
        assert "created" in comment

    def test_indicates_reused_pr(self) -> None:
        """Should indicate when PR was reused."""
        comment = build_recovery_success_comment(
            branch="issue-42",
            head_sha="abc123",
            pr_url="https://github.com/example/repo/pull/1",
            pr_reused=True,
        )

        assert "reused" in comment

    def test_includes_event_marker(self) -> None:
        """Should include iar:event marker for review_once parsing."""
        comment = build_recovery_success_comment(
            branch="issue-42",
            head_sha="abc123",
            pr_url="https://github.com/example/repo/pull/1",
            pr_reused=False,
        )

        assert "<!-- iar:event" in comment
        assert "phase=publish_recovered" in comment
        assert "pr_branch=issue-42" in comment
        assert "head=abc123" in comment
