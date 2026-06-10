"""Tests for the local Issue runner."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    AttemptResult,
    CommandResult,
    FailureType,
    GeneratedContentConfig,
    GeneratedContentTargetConfig,
    GitConfig,
    IssueSummary,
    PostPrSupervisorConfig,
    PrePushReviewConfig,
    PromptConfig,
    PullRequestContext,
    RunnerConfig,
    WorktreeConfig,
)
from backend.core.use_cases.run_agent_once import (
    MaxRetriesExceededError,
    PrdDeliveryError,
    build_recovery_prompt,
    build_prompt,
    choose_agent,
    classify_failure,
    commit_requested_changes,
    detect_usage_limit_root_cause,
    ensure_prd_delivery_ready,
    extract_agent_response_text,
    extract_prd_path,
    format_agent_execution_failure,
    format_attempt_history,
    format_command,
    format_failure_comment,
    get_head_sha,
    publish_changes,
    resolve_prd_archive_path,
    run_agent_with_prompt,
    validate_safe_changes,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def test_format_command_substitutes_issue_number() -> None:
    """Command templates should have {issue_number} replaced."""
    result = format_command("echo {issue_number}", issue_number=42)
    assert result == ["echo", "42"]


def test_choose_agent_override() -> None:
    """CLI override should take precedence."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    config = AppConfig()
    assert choose_agent(issue, config, "claude") == "claude"


def test_choose_agent_from_labels() -> None:
    """Issue labels should determine agent when override is auto."""
    issue = IssueSummary(
        number=1, title="T", url="U", body="B", labels=("agent/claude",)
    )
    config = AppConfig()
    assert choose_agent(issue, config, "auto") == "claude"


def test_choose_agent_defaults_to_claude() -> None:
    """Default agent should be claude when no signals are present."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    config = AppConfig()
    assert choose_agent(issue, config, "auto") == "claude"


def test_run_agent_with_prompt_uses_claude_yolo_mode(tmp_path: Path) -> None:
    """Claude runner should bypass permission prompts for unattended execution."""
    fake_runner = FakeProcessRunner()

    run_agent_with_prompt("claude", "Implement the issue.", tmp_path, fake_runner)

    assert fake_runner.calls == [
        [
            "claude",
            "--dangerously-skip-permissions",
            "--verbose",
            "-p",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "Implement the issue.",
        ]
    ]


def test_run_agent_with_prompt_can_capture_output(tmp_path: Path) -> None:
    """Prepared agent runs should opt into captured stdout when needed."""
    command = (
        "codex",
        "--cd",
        str(tmp_path),
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "exec",
        "Review.",
    )
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=0,
                stdout='{"verdict": "approved"}',
                stderr="",
            )
        }
    )

    uncaptured = run_agent_with_prompt("codex", "Review.", tmp_path, fake_runner)
    captured = run_agent_with_prompt(
        "codex",
        "Review.",
        tmp_path,
        fake_runner,
        capture_output=True,
    )

    assert uncaptured.stdout == ""
    assert captured.stdout == '{"verdict": "approved"}'


def test_run_agent_with_prompt_passes_timeout(tmp_path: Path) -> None:
    """Prepared agent runs should pass timeout through to the process runner."""

    class _RecordingTimeoutRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self.timeouts: list[int | None] = []

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            self.timeouts.append(timeout)
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _RecordingTimeoutRunner()

    run_agent_with_prompt(
        "codex",
        "Review.",
        tmp_path,
        fake_runner,
        capture_output=True,
        timeout_seconds=123,
    )

    assert fake_runner.timeouts == [123]


def test_extract_agent_response_text_from_claude_stream_json() -> None:
    """Captured Claude stream-json should be reduced to assistant text."""
    result = CommandResult(
        command=("claude", "--output-format", "stream-json", "-p", "Review."),
        return_code=0,
        stdout=(
            '{"type":"stream_event","event":{"delta":'
            '{"type":"text_delta","text":"```json\\n"}}}\n'
            '{"type":"stream_event","event":{"delta":'
            '{"type":"text_delta","text":"{\\"verdict\\": '
            '\\"approved\\"}\\n```"}}}\n'
        ),
        stderr="",
    )

    assert extract_agent_response_text(result) == (
        '```json\n{"verdict": "approved"}\n```'
    )


def test_run_once_dry_run() -> None:
    """Dry-run should list ready work without mutating labels."""
    fake_client = FakeGitHubClient()
    fake_client.list_ready_issues = lambda ready_label, limit: [
        IssueSummary(
            number=123,
            title="Example",
            url="https://github.com/example/repo/issues/123",
            body="PRD path: `tasks/example.md`",
            labels=("agent/ready", "agent/codex"),
        )
    ]
    fake_runner = FakeProcessRunner()
    config = AppConfig()

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=True,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    edit_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert len(edit_calls) == 0


def test_run_once_preflight_rejects_missing_configured_remote(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """run_once should fail before claiming work when configured remote is absent."""
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
        responses={
            _git_remote_command(): _git_remote_result("zata"),
        }
    )
    caplog.set_level(logging.ERROR, logger="backend.core.use_cases.run_agent_once")

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=AppConfig(),
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 1
    assert fake_client.calls == []
    assert fake_runner.calls == [["git", "remote"]]
    assert "Configured git remote 'origin' does not exist" in caplog.text
    assert "Available remotes: zata" in caplog.text


def test_build_prompt_uses_commit_request_proxy() -> None:
    """Prompt should route commit intent through the runner proxy."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    prompt = build_prompt(issue, Path("/worktree"), PromptConfig())
    assert "Do not merge main, delete branches, push, or create PRs" in prompt
    assert "Do not run `git add` or `git commit`" in prompt
    assert ".agent-runner/commit-request.json" in prompt
    assert "commit_message" in prompt


def test_build_prompt_fallback_to_default() -> None:
    """Empty prompt config should fall back to the built-in default template."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    prompt = build_prompt(issue, Path("/worktree"), PromptConfig())
    assert "Complete GitHub Issue #1: Test" in prompt
    assert "Execution rules:" in prompt


def test_build_prompt_uses_config_template() -> None:
    """Custom phase template in PromptConfig should override the default."""
    issue = IssueSummary(
        number=42,
        title="Custom",
        url="https://github.com/example/repo/issues/42",
        body="Custom body",
        labels=(),
    )
    custom_template = "Issue #{issue_number}: {issue_title}\n{issue_body}"
    prompt_config = PromptConfig(phases={"execution": custom_template})
    prompt = build_prompt(issue, Path("/worktree"), prompt_config)
    assert prompt == "Issue #42: Custom\nCustom body"


def test_build_prompt_replaces_all_placeholders() -> None:
    """All template placeholders should be replaced with issue values."""
    issue = IssueSummary(
        number=7,
        title="Replace Test",
        url="https://github.com/example/repo/issues/7",
        body="PRD path: `docs/prd.md`",
        labels=(),
    )
    template = (
        "num={issue_number} title={issue_title} url={issue_url} "
        "path={worktree_path} body={issue_body} prd={prd_line}"
    )
    prompt_config = PromptConfig(phases={"execution": template})
    prompt = build_prompt(issue, Path("/wt"), prompt_config)
    assert "num=7" in prompt
    assert "title=Replace Test" in prompt
    assert "url=https://github.com/example/repo/issues/7" in prompt
    assert "path=/wt" in prompt
    assert "body=PRD path: `docs/prd.md`" in prompt
    assert "prd=Also read the canonical PRD at `docs/prd.md`" in prompt


def test_build_recovery_prompt_includes_failure_context() -> None:
    """Recovery prompt should give the agent enough detail to fix and retry."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    failure_summary = "\n".join(
        [
            "Verification after runner staged changes with git add -A failed.",
            "Command: `just test`",
            "stdout: failing stdout",
            "stderr: failing stderr",
        ]
    )

    prompt = build_recovery_prompt(
        issue,
        Path("/worktree"),
        recovery_attempt=1,
        max_recovery_attempts=2,
        failure_summary=failure_summary,
    )

    assert "Recovery attempt: 1/2" in prompt
    assert "Verification after runner staged changes with git add -A failed" in prompt
    assert "failing stdout" in prompt
    assert "failing stderr" in prompt
    assert "Do not run `git add` or `git commit`" in prompt
    assert ".agent-runner/commit-request.json" in prompt


def test_extract_prd_path_finds_backtick_path() -> None:
    """PRD path should be extracted from Issue body backtick syntax."""
    body = "Some text\nPRD path: `tasks/pending/example.md`\nMore text"
    assert extract_prd_path(body) == "tasks/pending/example.md"


def test_extract_prd_path_returns_none_when_missing() -> None:
    """None should be returned when no PRD path is present."""
    assert extract_prd_path("No PRD here.") is None


def test_build_prompt_includes_prd_closeout_for_pending_prd() -> None:
    """Prompt should instruct the agent to update checklist and archive pending PRDs."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prompt = build_prompt(issue, Path("/worktree"), PromptConfig())
    assert "tasks/pending/example.md" in prompt
    assert "Acceptance Checklist" in prompt
    assert "tasks/pending/" in prompt
    assert "tasks/archive/" in prompt


def test_build_prompt_no_prd_path() -> None:
    """Prompt should give generic PRD advice when no canonical path is present."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Just a regular issue.",
        labels=(),
    )
    prompt = build_prompt(issue, Path("/worktree"), PromptConfig())
    assert "If the Issue references a PRD, read it before editing." in prompt


def test_build_recovery_prompt_includes_prd_closeout() -> None:
    """Recovery prompt should remind the agent about PRD closeout state."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prompt = build_recovery_prompt(
        issue,
        Path("/worktree"),
        recovery_attempt=1,
        max_recovery_attempts=2,
        failure_summary="Something broke.",
    )
    assert "tasks/pending/example.md" in prompt
    assert "Acceptance Checklist" in prompt
    assert "archived if complete" in prompt


def test_resolve_prd_archive_path_converts_pending() -> None:
    """Pending PRD paths should map to the archive directory."""
    assert (
        resolve_prd_archive_path("tasks/pending/example.md")
        == "tasks/archive/example.md"
    )


def test_resolve_prd_archive_path_returns_none_for_non_pending() -> None:
    """Non-pending paths should not resolve to an archive path."""
    assert resolve_prd_archive_path("tasks/archive/example.md") is None
    assert resolve_prd_archive_path("docs/example.md") is None


def test_ensure_prd_delivery_ready_skips_when_no_prd_path(tmp_path: Path) -> None:
    """Gate should be a no-op when the Issue has no canonical PRD path."""
    issue = IssueSummary(number=1, title="T", url="U", body="No PRD.", labels=())
    fake_runner = FakeProcessRunner()
    ensure_prd_delivery_ready(issue, tmp_path, fake_runner)
    assert fake_runner.calls == []


def test_ensure_prd_delivery_ready_raises_when_pending_incomplete(
    tmp_path: Path,
) -> None:
    """Pending PRD with unchecked items should raise PrdDeliveryError."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prd_path = tmp_path / "tasks" / "pending" / "example.md"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "- [x] done",
                "- [ ] undone",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()
    with pytest.raises(PrdDeliveryError, match="unchecked items"):
        ensure_prd_delivery_ready(issue, tmp_path, fake_runner)


def test_ensure_prd_delivery_ready_git_mv_when_pending_complete(
    tmp_path: Path,
) -> None:
    """Complete pending PRD should be moved to archive by git mv."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prd_path = tmp_path / "tasks" / "pending" / "example.md"
    archive_dir = tmp_path / "tasks" / "archive"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "- [x] done",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()
    ensure_prd_delivery_ready(issue, tmp_path, fake_runner)
    assert [
        "git",
        "mv",
        "tasks/pending/example.md",
        "tasks/archive/example.md",
    ] in fake_runner.calls


def test_ensure_prd_delivery_ready_passes_when_archive_complete(
    tmp_path: Path,
) -> None:
    """Archived PRD with all items checked should pass the gate."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    archive_path = tmp_path / "tasks" / "archive" / "example.md"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(
        "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "- [x] done",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()
    ensure_prd_delivery_ready(issue, tmp_path, fake_runner)
    assert fake_runner.calls == []


def test_ensure_prd_delivery_ready_raises_when_missing_section(
    tmp_path: Path,
) -> None:
    """PRD without Acceptance Checklist section should raise PrdDeliveryError."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prd_path = tmp_path / "tasks" / "pending" / "example.md"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text("# PRD\n", encoding="utf-8")
    fake_runner = FakeProcessRunner()
    with pytest.raises(PrdDeliveryError, match="Acceptance Checklist section missing"):
        ensure_prd_delivery_ready(issue, tmp_path, fake_runner)


def test_ensure_prd_delivery_ready_raises_when_prd_missing(
    tmp_path: Path,
) -> None:
    """Missing canonical PRD should raise PrdDeliveryError."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    fake_runner = FakeProcessRunner()
    with pytest.raises(PrdDeliveryError, match="Canonical PRD not found"):
        ensure_prd_delivery_ready(issue, tmp_path, fake_runner)


def test_ensure_prd_delivery_ready_raises_when_archive_dir_missing(
    tmp_path: Path,
) -> None:
    """Pending PRD ready for archive but missing archive dir should raise PrdDeliveryError."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prd_path = tmp_path / "tasks" / "pending" / "example.md"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "- [x] done",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()
    with pytest.raises(PrdDeliveryError, match="Archive directory does not exist"):
        ensure_prd_delivery_ready(issue, tmp_path, fake_runner)


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
            _git_remote_command(): _git_remote_result("origin"),
        }
    )
    branch, pr_url = publish_changes(
        issue, Path("."), AppConfig(), fake_client, fake_runner
    )
    assert branch == "issue-1"
    assert pr_url == "https://github.com/example/repo/pull/1"
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "add", "-A") not in commands
    assert ("git", "commit", "-m", "agent: complete issue #1") not in commands
    assert ("git", "push", "-u", "origin", "issue-1") in commands


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
            _git_remote_command(): _git_remote_result("zata", "upstream"),
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
            _git_remote_command(): _git_remote_result("origin", "zata"),
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


def test_publish_failure_category_push_vs_pr_create() -> None:
    """publish_changes wrapper should report push vs pr_create accurately."""
    from backend.core.use_cases.agent_runner_failure import PublishFailureError
    from backend.core.use_cases.agent_runner_publication import (
        _publish_changes_with_recovery_context,
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
        _publish_changes_with_recovery_context(
            issue=issue,
            worktree_path=Path("."),
            config=AppConfig(),
            github_client=FakeGitHubClient(),
            process_runner=fake_runner_push_fail,
            expected_branch="issue-1",
            content_generator=None,
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
            ("git", "remote"): CommandResult(
                command=("git", "remote"),
                return_code=0,
                stdout="origin\n",
                stderr="",
            ),
            ("git", "push", "-u", "origin", "issue-1"): CommandResult(
                command=("git", "push", "-u", "origin", "issue-1"),
                return_code=0,
                stdout="",
                stderr="",
            ),
        }
    )
    with pytest.raises(PublishFailureError) as exc_info:
        _publish_changes_with_recovery_context(
            issue=issue,
            worktree_path=Path("."),
            config=AppConfig(),
            github_client=_PRCreateFailClient(),
            process_runner=fake_runner_pr_fail,
            expected_branch="issue-1",
            content_generator=None,
        )
    assert exc_info.value.failure_category == PublishFailureCategory.PR_CREATE


def test_validate_safe_changes_rejects_forbidden_path(tmp_path: Path) -> None:
    """Runner should not publish configured secret-like paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    from tests.test_create_issue_from_prd import _init_repo

    _init_repo(repo)
    (repo / ".env").write_text("SECRET=value\n", encoding="utf-8")

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M .env\n",
                stderr="",
            ),
        }
    )

    with pytest.raises(RuntimeError, match="Refusing to publish forbidden paths: .env"):
        validate_safe_changes(repo, AppConfig(), fake_runner)


def _make_ready_issue() -> IssueSummary:
    return IssueSummary(
        number=123,
        title="Example",
        url="https://github.com/example/repo/issues/123",
        body="Example body",
        labels=("agent/ready", "agent/codex"),
    )


def _make_prd_issue(
    prd_path: str = "tasks/pending/example.md",
) -> IssueSummary:
    return IssueSummary(
        number=123,
        title="Example",
        url="https://github.com/example/repo/issues/123",
        body=f"PRD path: `{prd_path}`",
        labels=("agent/ready", "agent/codex"),
    )


def _config_for_worktree(
    worktree_path: Path,
    *verification_commands: str,
    max_recovery_attempts: int = 2,
    recovery_retry_delay_seconds: int = 0,
) -> AppConfig:
    commands = verification_commands or ("just test",)
    return AppConfig(
        runner=RunnerConfig(
            max_recovery_attempts=max_recovery_attempts,
            recovery_retry_delay_seconds=recovery_retry_delay_seconds,
            verification_commands=commands,
        ),
        worktree=WorktreeConfig(path_command=f"echo {worktree_path}"),
    )


def _config_with_review_disabled(
    worktree_path: Path | None = None,
    *verification_commands: str,
    max_recovery_attempts: int = 2,
    recovery_retry_delay_seconds: int = 0,
) -> AppConfig:
    """Return a config with pre-push review and post-PR supervisor disabled."""
    commands = verification_commands or ("just test",)
    worktree_cfg = (
        WorktreeConfig(path_command=f"echo {worktree_path}")
        if worktree_path
        else WorktreeConfig()
    )
    return AppConfig(
        runner=RunnerConfig(
            max_recovery_attempts=max_recovery_attempts,
            recovery_retry_delay_seconds=recovery_retry_delay_seconds,
            verification_commands=commands,
        ),
        worktree=worktree_cfg,
        pre_push_review=PrePushReviewConfig(enabled=False),
        post_pr_supervisor=PostPrSupervisorConfig(enabled=False),
    )


def _worktree_path_response(
    worktree_path: Path,
) -> tuple[tuple[str, ...], CommandResult]:
    command = ("echo", str(worktree_path))
    return command, CommandResult(
        command=command,
        return_code=0,
        stdout=f"{worktree_path}\n",
        stderr="",
    )


def _git_remote_command() -> tuple[str, ...]:
    return ("git", "remote")


def _git_remote_result(*remote_names: str) -> CommandResult:
    command = _git_remote_command()
    return CommandResult(
        command=command,
        return_code=0,
        stdout="".join(f"{remote_name}\n" for remote_name in remote_names),
        stderr="",
    )


def _write_commit_request(worktree_path: Path, commit_message: str) -> None:
    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        f'{{"commit_message": "{commit_message}"}}\n',
        encoding="utf-8",
    )


def _write_complete_prd(
    worktree_path: Path, relative_path: str = "tasks/example.md"
) -> None:
    prd_path = worktree_path / relative_path
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        "\n".join(
            [
                "# PRD: Example",
                "",
                "## 7. Acceptance Checklist",
                "",
                "- [x] item 1",
                "- [x] item 2",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_incomplete_prd(
    worktree_path: Path, relative_path: str = "tasks/example.md"
) -> None:
    prd_path = worktree_path / relative_path
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        "\n".join(
            [
                "# PRD: Example",
                "",
                "## 7. Acceptance Checklist",
                "",
                "- [x] item 1",
                "- [ ] item 2",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_run_once_uncommitted_changes_runner_commits(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """run_once should commit requested agent changes before publishing."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")

    class _FallbackCommitRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._status_calls = 0

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self.calls.append(list(command))
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=f"{sha}\n",
                    stderr="",
                )
            if command_tuple == ("git", "status", "--porcelain"):
                self.calls.append(list(command))
                self._status_calls += 1
                status_stdout = (
                    " M file.txt\n?? .agent-runner/commit-request.json\n"
                    if self._status_calls == 1
                    else " M file.txt\n"
                    if self._status_calls < 4
                    else ""
                )
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=status_stdout,
                    stderr="",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _FallbackCommitRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        ("git", "branch", "--show-current"): CommandResult(
            command=("git", "branch", "--show-current"),
            return_code=0,
            stdout="issue-123\n",
            stderr="",
        ),
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_with_review_disabled(worktree_path, "npm test")
    caplog.set_level(logging.WARNING, logger="backend.core.use_cases.run_agent_once")

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    commands = [tuple(command) for command in fake_runner.calls]
    commit_command = (
        "git",
        "commit",
        "-m",
        "agent: implement example",
    )
    validation_indices = [
        index for index, command in enumerate(commands) if command == ("npm", "test")
    ]
    add_index = commands.index(("git", "add", "-A"))
    commit_index = commands.index(commit_command)
    head_indices = [
        index
        for index, command in enumerate(commands)
        if command == ("git", "rev-parse", "HEAD")
    ]
    assert len(validation_indices) == 2
    assert validation_indices[0] < add_index < validation_indices[1] < commit_index
    assert commit_index < head_indices[-1]
    assert ("just", "test") not in commands
    assert not (worktree_path / ".agent-runner" / "commit-request.json").exists()
    assert ("git", "push", "-u", "origin", "issue-123") in commands
    assert (
        "Agent left uncommitted changes for Issue #123; "
        "runner processing commit request." in caplog.text
    )
    review_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.review in c.get("add", [])
    ]
    assert len(review_calls) == 1
    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 0


def test_run_once_recovers_after_staged_verification_failure(
    tmp_path: Path,
) -> None:
    """run_once should ask the agent to fix failures found after git add."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: initial attempt")

    class _StagedRecoveryRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._test_calls = 0
            self._committed = False

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] == ("codex",):
                prompt = command_tuple[-1]
                if "Recovery attempt: 1/2" in prompt:
                    _write_commit_request(worktree_path, "agent: recovered fix")
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(command_tuple, 0, f"{sha}\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                stdout = "" if self._committed else " M file.txt\n"
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple == ("just", "test"):
                self._test_calls += 1
                if self._test_calls == 2:
                    return CommandResult(
                        command_tuple,
                        1,
                        "staged stdout\n",
                        "staged stderr\n",
                    )
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "commit", "-m", "agent: recovered fix"):
                self._committed = True
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _StagedRecoveryRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_with_review_disabled(worktree_path)

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    commands = [tuple(command) for command in fake_runner.calls]
    add_indices = [
        index
        for index, command in enumerate(commands)
        if command == ("git", "add", "-A")
    ]
    test_indices = [
        index for index, command in enumerate(commands) if command == ("just", "test")
    ]
    reset_index = commands.index(("git", "reset", "--mixed"))
    recovery_prompt = [
        command[-1] for command in commands if command[:1] == ("codex",)
    ][1]
    assert len(add_indices) == 2
    assert len(test_indices) == 4
    assert add_indices[0] < test_indices[1] < reset_index
    assert reset_index < add_indices[1] < test_indices[3]
    assert (
        "Verification after runner staged changes with git add -A failed"
        in recovery_prompt
    )
    assert "staged stdout" in recovery_prompt
    assert "staged stderr" in recovery_prompt
    assert ("git", "commit", "-m", "agent: recovered fix") in commands


def test_run_once_recovers_after_agent_command_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_once should retry when the agent CLI exits before verification."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _AgentCommandRecoveryRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._agent_calls = 0
            self._sha_calls = 0
            self._committed = False

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                return self.responses[command_tuple]
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                if self._agent_calls == 1:
                    raise RuntimeError("API Error: 400 Invalid request Error")
                prompt = command_tuple[-1]
                assert "Recovery attempt: 1/2" in prompt
                assert "API Error: 400 Invalid request Error" in prompt
                _write_commit_request(worktree_path, "agent: recovered after api error")
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(command_tuple, 0, f"{sha}\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                stdout = "" if self._committed else " M file.txt\n"
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple == (
                "git",
                "commit",
                "-m",
                "agent: recovered after api error",
            ):
                self._committed = True
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _AgentCommandRecoveryRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        _git_remote_command(): _git_remote_result("origin"),
    }
    sleep_calls: list[int] = []
    monkeypatch.setattr(
        "backend.core.use_cases.run_agent_once.time.sleep",
        lambda seconds: sleep_calls.append(seconds),
    )
    config = _config_with_review_disabled(worktree_path, recovery_retry_delay_seconds=7)
    caplog.set_level(logging.WARNING, logger="backend.core.use_cases.run_agent_once")

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    commands = [tuple(command) for command in fake_runner.calls]
    agent_commands = [command for command in commands if command[:1] == ("codex",)]
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
    ]
    assert exit_code == 0
    assert len(agent_commands) == 2
    assert sleep_calls == [7]
    assert ("git", "commit", "-m", "agent: recovered after api error") in commands
    assert len(failed_calls) == 0
    assert "Agent command failed for Issue #123" in caplog.text


def test_run_once_uncommitted_changes_validation_failure_does_not_stage(
    tmp_path: Path,
) -> None:
    """run_once should not stage fallback changes when validation fails."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")
    _write_complete_prd(worktree_path)

    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner = FakeProcessRunner(
        responses={
            path_command: path_result,
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="before-sha\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M file.txt\n",
                stderr="",
            ),
            ("just", "test"): CommandResult(
                command=("just", "test"),
                return_code=1,
                stdout="",
                stderr="tests failed\n",
            ),
            _git_remote_command(): _git_remote_result("origin"),
        }
    )
    config = _config_with_review_disabled(worktree_path)

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 1
    commands = [tuple(command) for command in fake_runner.calls]
    assert commands.count(("just", "test")) == 3
    assert ("git", "add", "-A") not in commands
    assert ("git", "commit", "-m", "[Agent] Issue #123: Example") not in commands
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 1
    comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue" and "Command failed" in c.get("body", "")
    ]
    assert len(comment_calls) == 1


def test_run_once_uncommitted_changes_missing_request_fails(tmp_path: Path) -> None:
    """run_once should not commit changes without an agent commit request."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_complete_prd(worktree_path)

    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner = FakeProcessRunner(
        responses={
            path_command: path_result,
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="before-sha\n",
                stderr="",
            ),
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-123\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M file.txt\n",
                stderr="",
            ),
            _git_remote_command(): _git_remote_result("origin"),
        }
    )
    config = _config_with_review_disabled(worktree_path)

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 1
    commands = [tuple(command) for command in fake_runner.calls]
    assert ("git", "add", "-A") not in commands
    assert ("git", "commit", "-m", "agent: implement example") not in commands
    comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue" and "commit request" in c.get("body", "")
    ]
    assert len(comment_calls) == 1


def test_run_once_uncommitted_changes_commit_failure_fails(tmp_path: Path) -> None:
    """run_once should fail if the runner fallback commit fails."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "[Agent] Issue #123: Example")
    _write_complete_prd(worktree_path)

    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner = FakeProcessRunner(
        responses={
            path_command: path_result,
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="before-sha\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M file.txt\n",
                stderr="",
            ),
            ("git", "commit", "-m", "[Agent] Issue #123: Example"): CommandResult(
                command=("git", "commit", "-m", "[Agent] Issue #123: Example"),
                return_code=1,
                stdout="",
                stderr="commit failed\n",
            ),
            _git_remote_command(): _git_remote_result("origin"),
        }
    )
    config = _config_with_review_disabled(worktree_path)

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 1
    commands = [tuple(command) for command in fake_runner.calls]
    commit_command = ("git", "commit", "-m", "[Agent] Issue #123: Example")
    test_indices = [
        index for index, command in enumerate(commands) if command == ("just", "test")
    ]
    add_index = commands.index(("git", "add", "-A"))
    assert len(test_indices) == 2
    assert test_indices[0] < add_index < test_indices[1]
    assert test_indices[1] < commands.index(commit_command)
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 1
    comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue" and "Command failed" in c.get("body", "")
    ]
    assert len(comment_calls) == 1


def test_commit_requested_changes_rejects_branch_change(tmp_path: Path) -> None:
    """Commit proxy should only commit on the expected branch."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")
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
        commit_requested_changes(
            _make_ready_issue(),
            worktree_path,
            AppConfig(),
            fake_runner,
            expected_branch="issue-123",
        )

    commands = [tuple(command) for command in fake_runner.calls]
    assert ("git", "add", "-A") not in commands


def test_commit_requested_changes_rejects_forbidden_paths(tmp_path: Path) -> None:
    """Commit proxy should apply forbidden path checks before staging."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-123\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M .env\n",
                stderr="",
            ),
        }
    )

    with pytest.raises(RuntimeError, match="Refusing to publish forbidden paths"):
        commit_requested_changes(
            _make_ready_issue(),
            worktree_path,
            AppConfig(),
            fake_runner,
            expected_branch="issue-123",
        )

    commands = [tuple(command) for command in fake_runner.calls]
    assert ("git", "add", "-A") not in commands


def test_commit_requested_changes_restages_tracked_verification_edits(
    tmp_path: Path,
) -> None:
    """Commit proxy should sync formatter edits made during verification."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-123\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M tests/test_example.py\n",
                stderr="",
            ),
            ("git", "diff", "--quiet"): CommandResult(
                command=("git", "diff", "--quiet"),
                return_code=1,
                stdout="",
                stderr="",
            ),
        }
    )
    config = AppConfig(runner=RunnerConfig(verification_commands=("just test",)))

    commit_requested_changes(
        _make_ready_issue(),
        worktree_path,
        config,
        fake_runner,
        expected_branch="issue-123",
    )

    commands = [tuple(command) for command in fake_runner.calls]
    initial_stage_index = commands.index(("git", "add", "-A"))
    verification_index = commands.index(("just", "test"))
    tracked_diff_index = commands.index(("git", "diff", "--quiet"))
    tracked_restage_index = commands.index(("git", "add", "-u"))
    commit_index = commands.index(("git", "commit", "-m", "agent: implement example"))
    assert (
        initial_stage_index
        < verification_index
        < tracked_diff_index
        < tracked_restage_index
        < commit_index
    )


def test_run_once_no_new_commits_fails() -> None:
    """run_once should fail when the agent produces no new commits."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="same-sha\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            _git_remote_command(): _git_remote_result("origin"),
        }
    )
    config = AppConfig(runner=RunnerConfig(recovery_retry_delay_seconds=0))

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 1
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 1
    comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue" and "no git commits" in c.get("body", "")
    ]
    assert len(comment_calls) == 1


def test_run_once_success(tmp_path: Path) -> None:
    """run_once should succeed through pre-push review and supervisor approval."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    fake_client._pr_contexts["issue-123"] = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-123",
        head_sha="after-sha",
        base_sha="before-sha",
    )
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _SuccessRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._agent_calls = 0

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(
                    command=command_tuple, return_code=0, stdout=f"{sha}\n", stderr=""
                )
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                # Agent 1: implementation, Agent 2: pre-push review, Agent 3: supervisor
                if self._agent_calls == 2:
                    return CommandResult(
                        command=command_tuple,
                        return_code=0,
                        stdout='{"verdict": "approved", "summary": "LGTM"}'
                        if capture_output
                        else "",
                        stderr="",
                    )
                if self._agent_calls == 3:
                    return CommandResult(
                        command=command_tuple,
                        return_code=0,
                        stdout=(
                            '{"action": "approve_for_human_review", "summary": "LGTM"}'
                        )
                        if capture_output
                        else "",
                        stderr="",
                    )
                return CommandResult(
                    command=command_tuple, return_code=0, stdout="", stderr=""
                )
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            return CommandResult(
                command=command_tuple, return_code=0, stdout="", stderr=""
            )

    fake_runner = _SuccessRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        ("git", "status", "--porcelain"): CommandResult(
            command=("git", "status", "--porcelain"),
            return_code=0,
            stdout="",
            stderr="",
        ),
        ("git", "branch", "--show-current"): CommandResult(
            command=("git", "branch", "--show-current"),
            return_code=0,
            stdout="issue-123\n",
            stderr="",
        ),
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = AppConfig(worktree=WorktreeConfig(path_command=f"echo {worktree_path}"))

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=tmp_path,
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    # Labels: ready -> running -> supervising -> review
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    added_labels = [label for c in label_calls for label in c.get("add", [])]
    assert config.labels.review in added_labels
    assert config.labels.supervising in added_labels
    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    bodies = [c["body"] for c in comment_calls]
    assert any("Implementation Complete" in b for b in bodies)
    assert any("Pre-Push Review" in b for b in bodies)
    assert any("Draft PR Created" in b for b in bodies)
    assert any("Post-PR Supervisor" in b for b in bodies)


def test_run_once_failure_removes_supervising_label(tmp_path: Path) -> None:
    """Failure after Draft PR creation should not leave supervising with failed."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    fake_client._pr_contexts["issue-123"] = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-123",
        head_sha="after-sha",
        base_sha="before-sha",
    )
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _SupervisorFailureRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._agent_calls = 0

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(
                    command=command_tuple, return_code=0, stdout=f"{sha}\n", stderr=""
                )
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                if self._agent_calls == 2:
                    return CommandResult(
                        command=command_tuple,
                        return_code=0,
                        stdout='{"verdict": "approved", "summary": "LGTM"}',
                        stderr="",
                    )
                if self._agent_calls == 3:
                    raise RuntimeError("supervisor crashed")
                return CommandResult(
                    command=command_tuple, return_code=0, stdout="", stderr=""
                )
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            return CommandResult(
                command=command_tuple, return_code=0, stdout="", stderr=""
            )

    fake_runner = _SupervisorFailureRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        ("git", "status", "--porcelain"): CommandResult(
            command=("git", "status", "--porcelain"),
            return_code=0,
            stdout="",
            stderr="",
        ),
        ("git", "branch", "--show-current"): CommandResult(
            command=("git", "branch", "--show-current"),
            return_code=0,
            stdout="issue-123\n",
            stderr="",
        ),
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = AppConfig(worktree=WorktreeConfig(path_command=f"echo {worktree_path}"))

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=tmp_path,
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 1
    failed_calls = [
        call
        for call in fake_client.calls
        if call["method"] == "edit_issue_labels"
        and config.labels.failed in call.get("add", [])
    ]
    assert len(failed_calls) == 1
    assert config.labels.supervising in failed_calls[0]["remove"]
    assert config.labels.running in failed_calls[0]["remove"]
    assert config.labels.agent_labels["codex"] not in failed_calls[0]["remove"]


def test_run_once_git_mv_prd_before_commit(tmp_path: Path) -> None:
    """run_once should git mv a complete pending PRD before staging and committing."""
    fake_client = FakeGitHubClient()
    issue = _make_prd_issue("tasks/pending/example.md")
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")
    _write_complete_prd(worktree_path, "tasks/pending/example.md")
    (worktree_path / "tasks" / "archive").mkdir(parents=True, exist_ok=True)
    src_file = worktree_path / "src" / "file.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("# code\n", encoding="utf-8")

    class _PrdSuccessRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._status_calls = 0

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self.calls.append(list(command))
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=f"{sha}\n",
                    stderr="",
                )
            if command_tuple == ("git", "status", "--porcelain"):
                self.calls.append(list(command))
                self._status_calls += 1
                status_stdout = (
                    " M src/file.py\n?? .agent-runner/commit-request.json\n"
                    if self._status_calls == 1
                    else " M src/file.py\nR  tasks/pending/example.md -> tasks/archive/example.md\n"
                    if self._status_calls == 2
                    else ""
                )
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=status_stdout,
                    stderr="",
                )
            if command_tuple == (
                "git",
                "mv",
                "tasks/pending/example.md",
                "tasks/archive/example.md",
            ):
                self.calls.append(list(command))
                return CommandResult(
                    command=command_tuple, return_code=0, stdout="", stderr=""
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _PrdSuccessRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        ("git", "branch", "--show-current"): CommandResult(
            command=("git", "branch", "--show-current"),
            return_code=0,
            stdout="issue-123\n",
            stderr="",
        ),
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_with_review_disabled(worktree_path, "npm test")

    from backend.core.use_cases.run_agent_once import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    commands = [tuple(command) for command in fake_runner.calls]
    mv_index = commands.index(
        ("git", "mv", "tasks/pending/example.md", "tasks/archive/example.md")
    )
    add_index = commands.index(("git", "add", "-A"))
    commit_index = commands.index(("git", "commit", "-m", "agent: implement example"))
    assert mv_index < add_index < commit_index
    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1


def test_run_once_recovers_after_prd_delivery_failure(tmp_path: Path) -> None:
    """run_once should recover when the pending PRD checklist is initially incomplete."""
    fake_client = FakeGitHubClient()
    issue = _make_prd_issue("tasks/pending/example.md")
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_incomplete_prd(worktree_path, "tasks/pending/example.md")
    (worktree_path / "tasks" / "archive").mkdir(parents=True, exist_ok=True)

    class _PrdRecoveryRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._agent_calls = 0
            self._committed = False

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                prompt = command_tuple[-1]
                if "Recovery attempt: 1/2" in prompt:
                    assert "PRD delivery check failed" in prompt
                    assert "unchecked items" in prompt
                    _write_commit_request(worktree_path, "agent: recovered fix")
                    _write_complete_prd(worktree_path, "tasks/pending/example.md")
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(command_tuple, 0, f"{sha}\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                stdout = "" if self._committed else " M file.txt\n"
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple == ("git", "commit", "-m", "agent: recovered fix"):
                self._committed = True
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == (
                "git",
                "mv",
                "tasks/pending/example.md",
                "tasks/archive/example.md",
            ):
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _PrdRecoveryRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_with_review_disabled(worktree_path)

    from backend.core.use_cases.run_agent_once import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    commands = [tuple(command) for command in fake_runner.calls]
    agent_commands = [command for command in commands if command[:1] == ("codex",)]
    assert len(agent_commands) == 2
    mv_index = commands.index(
        ("git", "mv", "tasks/pending/example.md", "tasks/archive/example.md")
    )
    add_index = commands.index(("git", "add", "-A"))
    commit_index = commands.index(("git", "commit", "-m", "agent: recovered fix"))
    assert mv_index < add_index < commit_index
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 0


def test_run_agent_repositories_once_aggregates_exit_code() -> None:
    """Multi-repo run-once should return 1 if any repository fails."""
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
            _git_remote_command(): _git_remote_result("origin"),
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

    branch, pr_url = publish_changes(
        issue, Path("."), app_config, fake_client, fake_runner
    )

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
            _git_remote_command(): _git_remote_result("origin"),
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

    branch, pr_url = publish_changes(
        issue, Path("."), app_config, fake_client, fake_runner
    )

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
            _git_remote_command(): _git_remote_result("origin"),
        }
    )
    gc_config = GeneratedContentConfig(enabled=False)
    from backend.core.shared.models.agent_runner import AppConfig, GitConfig

    app_config = AppConfig(
        git=GitConfig(remote="origin", base_branch="main"),
        generated_content=gc_config,
    )

    branch, pr_url = publish_changes(
        issue, Path("."), app_config, fake_client, fake_runner
    )

    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1
    assert pr_calls[0]["title"] == "[Agent] Test"
    assert "Closes #1" in pr_calls[0]["body"]


def test_classify_failure_uncommitted() -> None:
    """classify_failure should return UNCOMMITTED_CHANGES when worktree is dirty."""
    agent_result = CommandResult(("codex",), 0, "", "")
    failure_type = classify_failure(
        before_sha="abc",
        after_sha="abc",
        has_uncommitted=True,
        agent_result=agent_result,
        verification_results=[],
        exc=None,
    )
    assert failure_type == FailureType.UNCOMMITTED_CHANGES


def test_classify_failure_no_commits() -> None:
    """classify_failure should return NO_COMMITS when SHA did not change."""
    agent_result = CommandResult(("codex",), 0, "", "")
    failure_type = classify_failure(
        before_sha="abc",
        after_sha="abc",
        has_uncommitted=False,
        agent_result=agent_result,
        verification_results=[],
        exc=None,
    )
    assert failure_type == FailureType.NO_COMMITS


def test_classify_failure_verification_failed() -> None:
    """classify_failure should return VERIFICATION_FAILED when a check fails."""
    agent_result = CommandResult(("codex",), 0, "", "")
    verification_results = [
        CommandResult(("just", "test"), 1, "", "tests failed"),
    ]
    failure_type = classify_failure(
        before_sha="abc",
        after_sha="def",
        has_uncommitted=False,
        agent_result=agent_result,
        verification_results=verification_results,
        exc=None,
    )
    assert failure_type == FailureType.VERIFICATION_FAILED


def test_classify_failure_agent_error() -> None:
    """classify_failure should return AGENT_ERROR when agent exits non-zero."""
    agent_result = CommandResult(("codex",), 1, "", "API error")
    failure_type = classify_failure(
        before_sha="abc",
        after_sha="def",
        has_uncommitted=False,
        agent_result=agent_result,
        verification_results=[CommandResult(("just", "test"), 0, "", "")],
        exc=None,
    )
    assert failure_type == FailureType.AGENT_ERROR


def test_classify_failure_unrecoverable_forbidden_paths() -> None:
    """classify_failure should return UNRECOVERABLE for forbidden path violations."""
    agent_result = CommandResult(("codex",), 0, "", "")
    exc = RuntimeError("Refusing to publish forbidden paths: .env")
    failure_type = classify_failure(
        before_sha="abc",
        after_sha="abc",
        has_uncommitted=False,
        agent_result=agent_result,
        verification_results=[],
        exc=exc,
    )
    assert failure_type == FailureType.UNRECOVERABLE


def test_classify_failure_success() -> None:
    """classify_failure should return SUCCESS when everything passes."""
    agent_result = CommandResult(("codex",), 0, "", "")
    failure_type = classify_failure(
        before_sha="abc",
        after_sha="def",
        has_uncommitted=False,
        agent_result=agent_result,
        verification_results=[CommandResult(("just", "test"), 0, "", "")],
        exc=None,
    )
    assert failure_type == FailureType.SUCCESS


def test_recovery_loop_success_on_second_attempt(tmp_path: Path) -> None:
    """Runner should succeed when recovery agent fixes the issue on attempt 2."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _RecoverySuccessRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._agent_calls = 0
            self._committed = False

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                if self._agent_calls == 1:
                    # First attempt: produce no commits
                    return CommandResult(command_tuple, 0, "", "")
                if self._agent_calls == 2:
                    # Recovery: write commit request and succeed
                    _write_commit_request(worktree_path, "agent: recovered")
                    return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(command_tuple, 0, f"{sha}\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                stdout = "" if self._committed else " M file.txt\n"
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple == ("git", "commit", "-m", "agent: recovered"):
                self._committed = True
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _RecoverySuccessRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_with_review_disabled(worktree_path)

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    commands = [tuple(command) for command in fake_runner.calls]
    agent_commands = [command for command in commands if command[:1] == ("codex",)]
    assert len(agent_commands) == 2
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    implementation_comment = [
        c for c in comment_calls if "Implementation Complete" in c.get("body", "")
    ]
    assert len(implementation_comment) == 1
    assert "Attempt History" in implementation_comment[0]["body"]


def test_recovery_loop_exhausted_raises_max_retries(tmp_path: Path) -> None:
    """Runner should fail with MaxRetriesExceededError when all attempts fail."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _ExhaustedRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                return self.responses[command_tuple]
            if command_tuple[:1] == ("codex",):
                # Always produce no commits
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                return CommandResult(command_tuple, 0, "same-sha\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _ExhaustedRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_with_review_disabled(
        worktree_path, max_recovery_attempts=1, recovery_retry_delay_seconds=0
    )

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 1
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 1
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    failure_comment = comment_calls[-1]
    assert "Attempt History" in failure_comment["body"]
    assert "Failed after 2 attempts" in failure_comment["body"]
    assert "no_commits" in failure_comment["body"]


def test_run_once_reuses_existing_clean_local_commit(tmp_path: Path) -> None:
    """Runner should publish an existing clean commit without invoking the agent."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _ExistingCommitRunner(FakeProcessRunner):
        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                return self.responses[command_tuple]
            if command_tuple[:1] == ("codex",):
                raise AssertionError("agent should not be invoked")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                return CommandResult(command_tuple, 0, "existing-sha\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == (
                "git",
                "rev-list",
                "--count",
                "origin/main..HEAD",
            ):
                return CommandResult(command_tuple, 0, "1\n", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _ExistingCommitRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_with_review_disabled(worktree_path)

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    commands = [tuple(command) for command in fake_runner.calls]
    assert not [command for command in commands if command[:1] == ("codex",)]
    assert ("git", "push", "-u", "origin", "issue-123") in commands
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    implementation_comment = [
        c for c in comment_calls if "Implementation Complete" in c.get("body", "")
    ]
    assert len(implementation_comment) == 1
    assert "Reused 1 existing local commit" in implementation_comment[0]["body"]


def test_run_once_recovers_running_issue_with_existing_local_commit(
    tmp_path: Path,
) -> None:
    """Running Issues with clean local commits should resume publish without agent."""
    fake_client = FakeGitHubClient()
    issue = IssueSummary(
        number=123,
        title="Example",
        url="https://github.com/example/repo/issues/123",
        body="Example body",
        labels=("agent/running", "agent/codex"),
    )
    fake_client.list_ready_issues = lambda ready_label, limit: []
    fake_client.list_review_candidate_issues = lambda labels, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _RunningRecoveryRunner(FakeProcessRunner):
        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                return self.responses[command_tuple]
            if command_tuple[:1] == ("codex",):
                raise AssertionError("agent should not be invoked")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                return CommandResult(command_tuple, 0, "existing-sha\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == (
                "git",
                "rev-list",
                "--count",
                "origin/main..HEAD",
            ):
                return CommandResult(command_tuple, 0, "1\n", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _RunningRecoveryRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_with_review_disabled(worktree_path)

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    commands = [tuple(command) for command in fake_runner.calls]
    assert not [command for command in commands if command[:1] == ("codex",)]
    assert ("git", "push", "-u", "origin", "issue-123") in commands
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert any(config.labels.review in c.get("add", []) for c in label_calls)


def test_attempt_history_in_issue_comment(tmp_path: Path) -> None:
    """Successful run should include Attempt History in the implementation comment."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _HistoryRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._agent_calls = 0

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                if self._agent_calls == 1:
                    # First attempt fails verification
                    return CommandResult(command_tuple, 0, "", "")
                if self._agent_calls == 2:
                    # Recovery succeeds
                    _write_commit_request(worktree_path, "agent: fix")
                    return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(command_tuple, 0, f"{sha}\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                stdout = " M file.txt\n" if self._agent_calls < 2 else ""
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple == ("git", "commit", "-m", "agent: fix"):
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _HistoryRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_with_review_disabled(worktree_path, "echo ok")

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    implementation_comment = [
        c for c in comment_calls if "Implementation Complete" in c.get("body", "")
    ]
    assert len(implementation_comment) == 1
    body = implementation_comment[0]["body"]
    assert "Attempt History" in body
    assert "success" in body
    assert "| 1 |" in body
    assert "| 2 |" in body


def test_format_attempt_history_empty() -> None:
    """format_attempt_history should return empty string for empty results."""
    assert format_attempt_history([]) == ""


def test_format_attempt_history_table() -> None:
    """format_attempt_history should render a markdown table."""
    results = [
        AttemptResult(
            attempt_number=1,
            failure_type=FailureType.NO_COMMITS,
            recovered=False,
            detail="No commits produced.",
        ),
        AttemptResult(
            attempt_number=2,
            failure_type=FailureType.SUCCESS,
            recovered=True,
            detail="Agent fixed the issue.",
        ),
    ]
    table = format_attempt_history(results)
    assert "| Attempt | Failure Type | Recovered | Detail |" in table
    assert "| 1 | no_commits | No | No commits produced. |" in table
    assert "| 2 | success | Yes | Agent fixed the issue. |" in table


_USAGE_LIMIT_STDOUT = (
    "\n[agent error] API Error: Request rejected (429) · usage limit exceeded, "
    "5-hour usage limit reached for Token Plan Max (9917000/9917000 used), "
    "resets at 2026-06-10T15:00:00+08:00 (2056)\n"
)


def _usage_limit_agent_error() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        1,
        ["claude", "--dangerously-skip-permissions", "-p", "HUGE_RECOVERY_PROMPT"],
        output=_USAGE_LIMIT_STDOUT,
        stderr="",
    )


def test_format_attempt_history_keeps_error_tail() -> None:
    """The Detail column should surface the actual error, not boilerplate."""
    detail = format_agent_execution_failure(_usage_limit_agent_error())
    table = format_attempt_history(
        [
            AttemptResult(
                attempt_number=1,
                failure_type=FailureType.AGENT_ERROR,
                recovered=False,
                detail=detail,
            )
        ]
    )
    assert "usage limit exceeded" in table
    assert "resets at 2026-06-10T15:00:00+08:00" in table
    assert "Agent command failed before runner verification" not in table


def test_format_attempt_history_escapes_table_pipes() -> None:
    """Pipes in the detail must not break the Markdown table."""
    table = format_attempt_history(
        [
            AttemptResult(
                attempt_number=1,
                failure_type=FailureType.AGENT_ERROR,
                recovered=False,
                detail="left | right",
            )
        ]
    )
    assert "left \\| right" in table


def test_detect_usage_limit_root_cause() -> None:
    """Usage-limit errors should yield a summary with the reset time."""
    summary = detect_usage_limit_root_cause(_USAGE_LIMIT_STDOUT)
    assert summary is not None
    assert "429" in summary
    assert "2026-06-10T15:00:00+08:00" in summary
    assert detect_usage_limit_root_cause("just lint failed with exit code 1") is None


def test_format_failure_comment_surfaces_usage_limit_root_cause() -> None:
    """The comment should lead with a root-cause line for usage limit failures."""
    attempt_history = [
        AttemptResult(
            attempt_number=1,
            failure_type=FailureType.AGENT_ERROR,
            recovered=False,
            detail=format_agent_execution_failure(_usage_limit_agent_error()),
        )
    ]
    body = format_failure_comment(
        MaxRetriesExceededError(attempt_history), attempt_history
    )
    root_cause_index = body.index("**Root cause:**")
    assert "2026-06-10T15:00:00+08:00" in body
    assert root_cause_index < body.index("### Attempt History")


def test_format_failure_comment_omits_agent_prompt_from_cause() -> None:
    """A CalledProcessError cause must not echo the full agent prompt."""
    attempt_history = [
        AttemptResult(
            attempt_number=1,
            failure_type=FailureType.AGENT_ERROR,
            recovered=False,
            detail="Agent command failed.",
        )
    ]
    failure = MaxRetriesExceededError(attempt_history)
    failure.__cause__ = _usage_limit_agent_error()
    body = format_failure_comment(failure, attempt_history)
    assert "HUGE_RECOVERY_PROMPT" not in body
    assert "Command: `claude`" in body
    assert "usage limit exceeded" in body


def test_scenario_b_precommit_lint_failure_recovery(tmp_path: Path) -> None:
    """Scene B: Agent committed, just lint failed, recovery fixed, 2nd pass.

    Steps:
    1. Agent writes commit-request (runner will stage and commit on its behalf).
    2. Runner stages with ``git add -A``.
    3. ``just lint`` returns non-zero -> VERIFICATION_FAILED.
    4. Runner injects stderr into recovery prompt.
    5. Recovery agent fixes and writes new commit-request.
    6. Runner re-stages, re-runs ``just lint`` -> passes.
    7. Runner commits and publishes.
    """
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: initial attempt")

    class _LintRecoveryRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._lint_calls = 0
            self._committed = False

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] == ("codex",):
                prompt = command_tuple[-1]
                if "Recovery attempt: 1/2" in prompt:
                    assert (
                        "Verification after runner staged changes with git add -A failed"
                        in prompt
                    )
                    assert "lint stdout" in prompt
                    assert "lint stderr" in prompt
                    _write_commit_request(worktree_path, "agent: fix lint")
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(command_tuple, 0, f"{sha}\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                stdout = "" if self._committed else " M file.txt\n"
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple == ("just", "lint"):
                self._lint_calls += 1
                if self._lint_calls == 2:
                    return CommandResult(
                        command_tuple,
                        1,
                        "lint stdout\n",
                        "lint stderr\n",
                    )
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "commit", "-m", "agent: fix lint"):
                self._committed = True
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _LintRecoveryRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_with_review_disabled(worktree_path, "just lint")

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    commands = [tuple(command) for command in fake_runner.calls]
    add_indices = [
        index
        for index, command in enumerate(commands)
        if command == ("git", "add", "-A")
    ]
    lint_indices = [
        index for index, command in enumerate(commands) if command == ("just", "lint")
    ]
    reset_index = commands.index(("git", "reset", "--mixed"))
    recovery_prompt = [
        command[-1] for command in commands if command[:1] == ("codex",)
    ][1]

    # Two staging rounds (initial + recovery)
    assert len(add_indices) == 2
    # just lint runs: pre-stage attempt 0, staged attempt 0, pre-stage recovery, staged recovery
    assert len(lint_indices) == 4
    assert add_indices[0] < lint_indices[1] < reset_index
    assert reset_index < add_indices[1] < lint_indices[3]
    assert (
        "Verification after runner staged changes with git add -A failed"
        in recovery_prompt
    )
    assert "lint stdout" in recovery_prompt
    assert "lint stderr" in recovery_prompt
    assert ("git", "commit", "-m", "agent: fix lint") in commands

    # Verify attempt history records the failed attempt then success.
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    implementation_comment = [
        c for c in comment_calls if "Implementation Complete" in c.get("body", "")
    ]
    assert len(implementation_comment) == 1
    body = implementation_comment[0]["body"]
    assert "Attempt History" in body
    assert "verification_failed" in body
    assert "success" in body


def test_scenario_e_lint_exhausted_max_retries(tmp_path: Path) -> None:
    """Scene E: staged verification fails on all 3 attempts, MaxRetriesExceededError.

    Steps:
    1. Attempt 0: Agent writes commit-request, runner stages, ``just lint`` fails.
    2. Attempt 1 (recovery): Agent fixes, runner re-stages, ``just lint`` still fails.
    3. Attempt 2 (recovery): Agent fixes again, runner re-stages, ``just lint`` still fails.
    4. All attempts exhausted → runner marks issue as failed.
    5. Issue comment contains Attempt History with 3 rows of ``verification_failed``.
    """
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _LintExhaustedRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._agent_calls = 0

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                _write_commit_request(
                    worktree_path, f"agent: attempt {self._agent_calls}"
                )
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(command_tuple, 0, f"{sha}\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                return CommandResult(command_tuple, 0, " M file.txt\n", "")
            if command_tuple == ("just", "lint"):
                return CommandResult(command_tuple, 1, "lint stdout\n", "lint stderr\n")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _LintExhaustedRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_with_review_disabled(worktree_path, "just lint")

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 1
    commands = [tuple(command) for command in fake_runner.calls]
    lint_indices = [
        index for index, command in enumerate(commands) if command == ("just", "lint")
    ]
    add_indices = [
        index
        for index, command in enumerate(commands)
        if command == ("git", "add", "-A")
    ]
    reset_indices = [
        index
        for index, command in enumerate(commands)
        if command == ("git", "reset", "--mixed")
    ]

    # just lint always fails at pre-staging verification, so never reaches staged
    # verification or commit_requested_changes.
    assert len(lint_indices) == 3
    assert len(add_indices) == 0
    assert len(reset_indices) == 0

    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 1
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    failure_comment = comment_calls[-1]
    assert "Attempt History" in failure_comment["body"]
    assert "Failed after 3 attempts" in failure_comment["body"]
    assert "verification_failed" in failure_comment["body"]
    assert "| 1 |" in failure_comment["body"]
    assert "| 2 |" in failure_comment["body"]
    assert "| 3 |" in failure_comment["body"]
