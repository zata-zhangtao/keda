"""Tests for the local Issue runner."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    GitConfig,
    IssueSummary,
    PromptConfig,
    RunnerConfig,
    WorktreeConfig,
)
from backend.core.use_cases.run_agent_once import (
    PrdDeliveryError,
    build_recovery_prompt,
    build_prompt,
    choose_agent,
    commit_requested_changes,
    ensure_prd_delivery_ready,
    extract_prd_path,
    format_command,
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


def test_choose_agent_defaults_to_codex() -> None:
    """Default agent should be codex when no signals are present."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    config = AppConfig()
    assert choose_agent(issue, config, "auto") == "codex"


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

    from backend.core.use_cases.run_agent_once import run_once

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

    from backend.core.use_cases.run_agent_once import run_once

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
    config = _config_for_worktree(worktree_path, "npm test")
    caplog.set_level(logging.WARNING, logger="backend.core.use_cases.run_agent_once")

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
    config = _config_for_worktree(worktree_path)

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
    config = _config_for_worktree(worktree_path, recovery_retry_delay_seconds=7)
    caplog.set_level(logging.WARNING, logger="backend.core.use_cases.run_agent_once")

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
    config = _config_for_worktree(worktree_path)

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
    config = _config_for_worktree(worktree_path)

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
    config = _config_for_worktree(worktree_path)

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


def test_run_once_success() -> None:
    """run_once should succeed when the agent commits changes."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]

    # _ShaSequenceRunner returns a different SHA on the second call to
    # simulate a new commit.
    class _ShaSequenceRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            if tuple(command) == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(
                    command=tuple(command), return_code=0, stdout=f"{sha}\n", stderr=""
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _ShaSequenceRunner()
    fake_runner.responses = {
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
    config = AppConfig()

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
    review_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.review in c.get("add", [])
    ]
    assert len(review_calls) == 1
    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1


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
    config = _config_for_worktree(worktree_path, "npm test")

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
    config = _config_for_worktree(worktree_path)

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
