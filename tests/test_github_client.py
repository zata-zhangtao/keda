"""Tests for the GitHub CLI infrastructure adapter."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.shared.models.agent_runner import CommandResult, LabelConfig
from backend.infrastructure.github_client import (
    GitHubCliClient,
    sanitize_github_body,
)
from tests.conftest import FakeProcessRunner


class _BodyCapturingRunner(FakeProcessRunner):
    """Process runner that records the body passed via ``--body-file``."""

    def __init__(self) -> None:
        super().__init__()
        self.body_files: list[str] = []

    def run(self, command, *, cwd, **kwargs):  # type: ignore[override]
        command_list = list(command)
        if "--body-file" in command_list:
            body_path = command_list[command_list.index("--body-file") + 1]
            self.body_files.append(Path(body_path).read_text(encoding="utf-8"))
        return super().run(command, cwd=cwd, **kwargs)


def test_sanitize_github_body_strips_request_breaking_control_characters() -> None:
    """Control characters that trigger GitHub's 400 must be removed."""
    raw_body = "ok\x00 line\x1b[31m colored\x07 bell\x7f del\ttab\nnewline\r"

    sanitized = sanitize_github_body(raw_body)

    assert "\x00" not in sanitized
    assert "\x1b" not in sanitized
    assert "\x07" not in sanitized
    assert "\x7f" not in sanitized
    # Tabs, newlines and carriage returns are valid Markdown and preserved.
    assert "\ttab" in sanitized
    assert "\nnewline" in sanitized
    assert sanitized.endswith("\r")


def test_sanitize_github_body_truncates_oversized_body_keeping_head_and_tail() -> None:
    """Oversized bodies are middle-truncated below GitHub's size limit."""
    raw_body = "H" * 50 + "M" * 100_000 + "T" * 50

    sanitized = sanitize_github_body(raw_body, max_length=2000)

    assert len(sanitized) <= 2000
    assert sanitized.startswith("H" * 50)
    assert sanitized.endswith("T" * 50)
    assert "truncated to fit GitHub's size limit" in sanitized


def test_comment_issue_sanitizes_body_before_posting(tmp_path: Path) -> None:
    """A failure comment with raw control characters must be scrubbed.

    Regression for Issue #84: agent CLI output embedded raw control bytes,
    so ``gh issue comment`` got a 400 and the failure reason was never posted.
    """
    capturing_runner = _BodyCapturingRunner()
    github_client = GitHubCliClient(tmp_path, capturing_runner)

    github_client.comment_issue(84, "## Agent Runner Failed\x00\x1b bad bytes")

    assert capturing_runner.body_files == ["## Agent Runner Failed bad bytes"]


def test_list_issue_comments_requests_comments_field(tmp_path: Path) -> None:
    """Issue comment loading should request and parse the comments field."""
    command = (
        "gh",
        "issue",
        "view",
        "23",
        "--comments",
        "--json",
        "comments",
    )
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=0,
                stdout=json.dumps(
                    {"comments": [{"body": "first"}, {"body": ""}, {"body": "second"}]}
                ),
                stderr="",
            )
        }
    )
    github_client = GitHubCliClient(tmp_path, fake_runner)

    comments = github_client.list_issue_comments(23)

    assert comments == ["first", "second"]
    assert fake_runner.calls == [list(command)]


def test_list_pr_comments_requests_comments_field(tmp_path: Path) -> None:
    """PR comment loading should request and parse the comments field."""
    command = (
        "gh",
        "pr",
        "view",
        "26",
        "--comments",
        "--json",
        "comments",
    )
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=0,
                stdout=json.dumps(
                    {"comments": [{"body": "review"}, {"body": None}, {"body": "done"}]}
                ),
                stderr="",
            )
        }
    )
    github_client = GitHubCliClient(tmp_path, fake_runner)

    comments = github_client.list_pr_comments(26)

    assert comments == ["review", "done"]
    assert fake_runner.calls == [list(command)]


def test_get_pull_request_context_uses_supported_rollup_field(
    tmp_path: Path,
) -> None:
    """PR context loading should use current gh statusCheckRollup output."""
    command = (
        "gh",
        "pr",
        "list",
        "--head",
        "issue-28",
        "--state",
        "open",
        "--json",
        "url,number,body,headRefName,headRefOid,baseRefOid,mergeable,statusCheckRollup",
    )
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=0,
                stdout=json.dumps(
                    [
                        {
                            "url": "https://github.com/example/repo/pull/28",
                            "headRefName": "issue-28",
                            "headRefOid": "head-sha",
                            "baseRefOid": "base-sha",
                            "mergeable": "CONFLICTING",
                            "statusCheckRollup": [
                                {
                                    "__typename": "CheckRun",
                                    "name": "lint",
                                    "status": "COMPLETED",
                                    "conclusion": "FAILURE",
                                    "detailsUrl": "https://checks.example/lint",
                                },
                                {
                                    "__typename": "StatusContext",
                                    "context": "unit",
                                    "state": "SUCCESS",
                                },
                            ],
                        }
                    ]
                ),
                stderr="",
            )
        }
    )
    github_client = GitHubCliClient(tmp_path, fake_runner)

    pr_context = github_client.get_pull_request_context("issue-28")

    assert pr_context is not None
    assert pr_context.pr_url == "https://github.com/example/repo/pull/28"
    assert pr_context.mergeable is False
    assert pr_context.checks_state == "FAILURE"
    assert pr_context.checks_summary == (
        "lint (status=COMPLETED, conclusion=FAILURE) https://checks.example/lint",
    )
    assert fake_runner.calls == [list(command)]


def test_get_pull_request_context_empty_rollup_has_no_checks_state(
    tmp_path: Path,
) -> None:
    """Empty check rollup should stay compatible with repositories without CI."""
    command = (
        "gh",
        "pr",
        "list",
        "--head",
        "issue-1",
        "--state",
        "open",
        "--json",
        "url,number,body,headRefName,headRefOid,baseRefOid,mergeable,statusCheckRollup",
    )
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=0,
                stdout=json.dumps(
                    [
                        {
                            "url": "https://github.com/example/repo/pull/1",
                            "headRefName": "issue-1",
                            "headRefOid": "head-sha",
                            "baseRefOid": "base-sha",
                            "mergeable": "MERGEABLE",
                            "statusCheckRollup": [],
                        }
                    ]
                ),
                stderr="",
            )
        }
    )
    github_client = GitHubCliClient(tmp_path, fake_runner)

    pr_context = github_client.get_pull_request_context("issue-1")

    assert pr_context is not None
    assert pr_context.mergeable is True
    assert pr_context.checks_state is None
    assert pr_context.checks_summary == ()


def test_edit_issue_labels_only_removes_attached_labels(tmp_path: Path) -> None:
    """Label editing should not ask gh to remove labels absent from the Issue."""
    view_command = (
        "gh",
        "issue",
        "view",
        "27",
        "--json",
        "labels",
    )
    edit_command = (
        "gh",
        "issue",
        "edit",
        "27",
        "--add-label",
        "agent/failed",
        "--remove-label",
        "agent/running",
    )
    fake_runner = FakeProcessRunner(
        responses={
            view_command: CommandResult(
                command=view_command,
                return_code=0,
                stdout=json.dumps(
                    {
                        "labels": [
                            {"name": "agent/running"},
                            {"name": "source/prd"},
                        ]
                    }
                ),
                stderr="",
            )
        }
    )
    github_client = GitHubCliClient(tmp_path, fake_runner)

    github_client.edit_issue_labels(
        27,
        add=["agent/failed"],
        remove=["agent/ready", "agent/running", "agent/supervising"],
    )

    assert fake_runner.calls == [list(view_command), list(edit_command)]


def test_edit_issue_labels_skips_noop_update(tmp_path: Path) -> None:
    """No-op label updates should not call gh issue edit."""
    view_command = (
        "gh",
        "issue",
        "view",
        "27",
        "--json",
        "labels",
    )
    fake_runner = FakeProcessRunner(
        responses={
            view_command: CommandResult(
                command=view_command,
                return_code=0,
                stdout=json.dumps({"labels": [{"name": "agent/failed"}]}),
                stderr="",
            )
        }
    )
    github_client = GitHubCliClient(tmp_path, fake_runner)

    github_client.edit_issue_labels(
        27,
        add=["agent/failed"],
        remove=["agent/ready", "agent/running"],
    )

    assert fake_runner.calls == [list(view_command)]


def test_list_review_candidate_issues_uses_or_label_semantics(
    tmp_path: Path,
) -> None:
    """Review candidate query must combine results across labels (OR semantics)."""
    supervising_command = (
        "gh",
        "issue",
        "list",
        "--state",
        "open",
        "--label",
        "agent/supervising",
        "--limit",
        "20",
        "--json",
        "number,title,url,labels,body,state",
    )
    review_command = (
        "gh",
        "issue",
        "list",
        "--state",
        "open",
        "--label",
        "agent/review",
        "--limit",
        "20",
        "--json",
        "number,title,url,labels,body,state",
    )
    fake_runner = FakeProcessRunner(
        responses={
            supervising_command: CommandResult(
                command=supervising_command,
                return_code=0,
                stdout=json.dumps(
                    [
                        {
                            "number": 90,
                            "title": "supervising only",
                            "url": "https://example/90",
                            "labels": [{"name": "agent/supervising"}],
                            "body": "",
                        },
                        {
                            "number": 92,
                            "title": "both labels",
                            "url": "https://example/92",
                            "labels": [
                                {"name": "agent/supervising"},
                                {"name": "agent/review"},
                            ],
                            "body": "",
                        },
                    ]
                ),
                stderr="",
            ),
            review_command: CommandResult(
                command=review_command,
                return_code=0,
                stdout=json.dumps(
                    [
                        {
                            "number": 91,
                            "title": "review only",
                            "url": "https://example/91",
                            "labels": [{"name": "agent/review"}],
                            "body": "",
                        },
                        {
                            "number": 92,
                            "title": "both labels",
                            "url": "https://example/92",
                            "labels": [
                                {"name": "agent/supervising"},
                                {"name": "agent/review"},
                            ],
                            "body": "",
                        },
                    ]
                ),
                stderr="",
            ),
        }
    )
    github_client = GitHubCliClient(tmp_path, fake_runner)

    candidates = github_client.list_review_candidate_issues(
        ["agent/supervising", "agent/review"], 20
    )

    candidate_numbers = {candidate.number for candidate in candidates}
    assert candidate_numbers == {90, 91, 92}
    # 92 must appear exactly once even though it matches both labels.
    assert len(candidates) == 3
    assert fake_runner.calls == [list(supervising_command), list(review_command)]


def test_sync_labels_creates_rework_prd_label(tmp_path: Path) -> None:
    """sync_labels must register agent/rework-prd, the Issue->PRD trigger label.

    Regression: agent/rework-prd drives create_prd_from_issue, but it was
    missing from sync_labels' specs, so ``iar init`` / ``iar labels sync`` never
    created it and the Issue-driven PRD workflow could not be triggered.
    """
    fake_runner = FakeProcessRunner()
    github_client = GitHubCliClient(tmp_path, fake_runner)

    github_client.sync_labels(LabelConfig())

    created_labels = [
        call[call.index("create") + 1]
        for call in fake_runner.calls
        if call[:3] == ["gh", "label", "create"]
    ]
    assert "agent/rework-prd" in created_labels


def test_list_rework_prd_issues_filters_label(tmp_path: Path) -> None:
    """list_rework_prd_issues should query gh with the given label and limit."""
    command = (
        "gh",
        "issue",
        "list",
        "--state",
        "open",
        "--label",
        "agent/rework-prd",
        "--limit",
        "5",
        "--json",
        "number,title,url,labels,body,state",
    )
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=0,
                stdout=json.dumps(
                    [
                        {
                            "number": 7,
                            "title": "Rework PRD",
                            "url": "https://example/7",
                            "labels": [{"name": "agent/rework-prd"}],
                            "body": "",
                            "state": "OPEN",
                        }
                    ]
                ),
                stderr="",
            )
        }
    )
    github_client = GitHubCliClient(tmp_path, fake_runner)

    issues = github_client.list_rework_prd_issues("agent/rework-prd", 5)

    assert len(issues) == 1
    assert issues[0].number == 7
    assert fake_runner.calls == [list(command)]


def test_edit_issue_body_writes_via_file(tmp_path: Path) -> None:
    """edit_issue_body should use gh issue edit --body-file."""

    class CapturingRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self.captured_body: str | None = None

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            input_text=None,
        ):
            self.calls.append(list(command))
            if "--body-file" in command:
                body_file_path = Path(command[command.index("--body-file") + 1])
                self.captured_body = body_file_path.read_text(encoding="utf-8")
            return CommandResult(command=tuple(command), return_code=0, stdout="", stderr="")

    runner = CapturingRunner()
    github_client = GitHubCliClient(tmp_path, runner)

    github_client.edit_issue_body(9, "New body text.")

    assert len(runner.calls) == 1
    assert runner.calls[0][:4] == ["gh", "issue", "edit", "9"]
    assert runner.calls[0][4] == "--body-file"
    assert runner.captured_body == "New body text."


class _FlakyProcessRunner(FakeProcessRunner):
    """Process runner that fails a fixed number of times before succeeding."""

    def __init__(self, fail_count: int, error_text: str) -> None:
        super().__init__()
        self.fail_count = fail_count
        self.error_text = error_text
        self.attempts = 0

    def run(self, command, *, cwd, check=True, **kwargs):  # type: ignore[override]
        self.attempts += 1
        if self.attempts <= self.fail_count:
            exc = subprocess.CalledProcessError(
                returncode=1,
                cmd=list(command),
                output="",
                stderr=self.error_text,
            )
            if check:
                raise exc
            return CommandResult(
                command=tuple(command),
                return_code=1,
                stdout="",
                stderr=self.error_text,
            )
        return CommandResult(
            command=tuple(command),
            return_code=0,
            stdout="https://github.com/org/repo/issues/42",
            stderr="",
        )


def test_create_issue_retries_on_transient_tls_timeout(tmp_path: Path) -> None:
    """create_issue should retry when gh fails with a TLS handshake timeout."""
    runner = _FlakyProcessRunner(
        fail_count=1,
        error_text='Post "https://api.github.com/graphql": net/http: TLS handshake timeout',
    )
    github_client = GitHubCliClient(tmp_path, runner)

    with patch("backend.infrastructure.github_client.time.sleep") as mock_sleep:
        issue_url = github_client.create_issue(title="title", body="body", labels=["type/feature"])

    assert issue_url == "https://github.com/org/repo/issues/42"
    assert runner.attempts == 2
    mock_sleep.assert_called_once()


def test_create_issue_does_not_retry_permanent_errors(tmp_path: Path) -> None:
    """create_issue should fail immediately on non-transient errors."""
    runner = _FlakyProcessRunner(
        fail_count=1,
        error_text="HTTP 401: Bad credentials",
    )
    github_client = GitHubCliClient(tmp_path, runner)

    with patch("backend.infrastructure.github_client.time.sleep") as mock_sleep:
        with pytest.raises(subprocess.CalledProcessError):
            github_client.create_issue(title="title", body="body", labels=[])

    assert runner.attempts == 1
    mock_sleep.assert_not_called()


def test_create_issue_gives_up_after_max_retries(tmp_path: Path) -> None:
    """create_issue should stop retrying after the maximum number of attempts."""
    runner = _FlakyProcessRunner(
        fail_count=10,
        error_text="net/http: TLS handshake timeout",
    )
    github_client = GitHubCliClient(tmp_path, runner)

    with patch("backend.infrastructure.github_client.time.sleep"):
        with pytest.raises(subprocess.CalledProcessError):
            github_client.create_issue(title="title", body="body", labels=[])

    assert runner.attempts == 3


def test_create_issue_retries_on_client_closed_request(tmp_path: Path) -> None:
    """create_issue should retry on HTTP 499 (client/edge closed the connection)."""
    runner = _FlakyProcessRunner(
        fail_count=1,
        error_text='non-200 OK status code: 499  body: ""',
    )
    github_client = GitHubCliClient(tmp_path, runner)

    with patch("backend.infrastructure.github_client.time.sleep") as mock_sleep:
        issue_url = github_client.create_issue(title="title", body="body", labels=["type/feature"])

    assert issue_url == "https://github.com/org/repo/issues/42"
    assert runner.attempts == 2
    mock_sleep.assert_called_once()


def test_create_issue_retries_on_edge_whoa_there_400(tmp_path: Path) -> None:
    """create_issue should retry GitHub's generic edge 'Whoa there!' 400 page.

    This is the exact failure shape seen in production: a sanitized comment
    body still tripped GitHub's edge-level abuse/validation page, which
    permanently stranded the Issue in ``agent/failed`` because nothing
    retried it. Whether the underlying cause is transient (worth retrying)
    or a deterministic content issue, retrying is safe: it either recovers
    or falls through to the exact same failure after a couple of seconds.
    """
    runner = _FlakyProcessRunner(
        fail_count=1,
        error_text=(
            'non-200 OK status code: 400 Bad Request body: "\\r\\n<html>\\r\\n'
            "  <head>\\r\\n    <title>Bad request &middot; GitHub</title>\\r\\n"
            '  </head>\\r\\n  <body>\\r\\n    <div class=\\"c\\">\\r\\n'
            "      <h1>Whoa there!</h1>\\r\\n      <p>You have sent an invalid "
            'request.</p>\\r\\n    </div>\\r\\n  </body>\\r\\n</html>\\r\\n"'
        ),
    )
    github_client = GitHubCliClient(tmp_path, runner)

    with patch("backend.infrastructure.github_client.time.sleep") as mock_sleep:
        issue_url = github_client.create_issue(title="title", body="body", labels=["type/feature"])

    assert issue_url == "https://github.com/org/repo/issues/42"
    assert runner.attempts == 2
    mock_sleep.assert_called_once()


def test_create_issue_does_not_retry_structured_400(tmp_path: Path) -> None:
    """A normal structured 400 (real validation error) must not be retried.

    Negative control for the "Whoa there!" pattern above: a clean JSON-style
    API rejection is a deterministic client-side bug, not a transient edge
    error, and must still fail fast rather than being masked by retries.
    """
    runner = _FlakyProcessRunner(
        fail_count=1,
        error_text='non-200 OK status code: 400 Bad Request body: "{\\"message\\":\\"Validation Failed\\"}"',
    )
    github_client = GitHubCliClient(tmp_path, runner)

    with patch("backend.infrastructure.github_client.time.sleep") as mock_sleep:
        with pytest.raises(subprocess.CalledProcessError):
            github_client.create_issue(title="title", body="body", labels=[])

    assert runner.attempts == 1
    mock_sleep.assert_not_called()


def test_list_pull_requests_for_issue_normalises_states(tmp_path: Path) -> None:
    """list_pull_requests_for_issue maps gh states to the 4-bucket view model."""
    command = (
        "gh",
        "pr",
        "list",
        "--repo",
        "owner/repo",
        "--search",
        "closes:#7 OR fixes:#7 OR resolves:#7 OR refs:#7",
        "--state",
        "all",
        "--limit",
        "100",
        "--json",
        "number,title,state,url,isDraft,mergedAt",
    )
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=0,
                stdout=json.dumps(
                    [
                        {
                            "number": 42,
                            "title": "merged PR",
                            "state": "MERGED",
                            "url": "https://example/pr/42",
                            "isDraft": False,
                            "mergedAt": "2026-05-01T00:00:00Z",
                        },
                        {
                            "number": 43,
                            "title": "draft PR",
                            "state": "OPEN",
                            "url": "https://example/pr/43",
                            "isDraft": True,
                            "mergedAt": "",
                        },
                        {
                            "number": 44,
                            "title": "closed PR",
                            "state": "CLOSED",
                            "url": "https://example/pr/44",
                            "isDraft": False,
                            "mergedAt": "",
                        },
                    ]
                ),
                stderr="",
            )
        }
    )
    github_client = GitHubCliClient(tmp_path, fake_runner)

    pulls = github_client.list_pull_requests_for_issue("owner/repo", 7)

    states = [pull.state for pull in pulls]
    # Open/draft sort before closed, merged last.
    assert states == ["draft", "closed", "merged"]
    assert pulls[0].is_draft is True
    assert pulls[-1].merged is True
    assert fake_runner.calls == [list(command)]


def test_list_pull_requests_for_issue_empty_when_no_prs(tmp_path: Path) -> None:
    """An empty gh response is reported as an empty list."""
    command = (
        "gh",
        "pr",
        "list",
        "--repo",
        "owner/repo",
        "--search",
        "closes:#7 OR fixes:#7 OR resolves:#7 OR refs:#7",
        "--state",
        "all",
        "--limit",
        "100",
        "--json",
        "number,title,state,url,isDraft,mergedAt",
    )
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=0,
                stdout="[]",
                stderr="",
            )
        }
    )
    github_client = GitHubCliClient(tmp_path, fake_runner)

    pulls = github_client.list_pull_requests_for_issue("owner/repo", 7)

    assert pulls == []
    assert fake_runner.calls == [list(command)]


def test_list_issues_by_label_omits_label_flag_when_none(tmp_path: Path) -> None:
    """When label is None the gh command must not include --label."""
    command_without_label = (
        "gh",
        "issue",
        "list",
        "--state",
        "all",
        "--limit",
        "100",
        "--json",
        "number,title,url,labels,body,state",
    )
    fake_runner = FakeProcessRunner(
        responses={
            command_without_label: CommandResult(
                command=command_without_label,
                return_code=0,
                stdout=json.dumps(
                    [
                        {
                            "number": 1,
                            "title": "all-issues",
                            "url": "https://example/1",
                            "labels": [],
                            "body": "",
                            "state": "OPEN",
                        }
                    ]
                ),
                stderr="",
            )
        }
    )
    github_client = GitHubCliClient(tmp_path, fake_runner)

    issues = github_client.list_issues_by_label(label=None, limit=100, state="all")

    assert len(issues) == 1
    assert issues[0].number == 1


def test_sync_labels_creates_verifier_passed_label(tmp_path: Path) -> None:
    """sync_labels must register validation/verifier-passed so post-PR verdicts can tag.

    Regression: when ``validation/verifier-passed`` was missing from the
    spec table, fresh repositories ran ``gh issue edit --add-label`` and
    GitHub rejected the call with ``'validation/verifier-passed' not
    found``, which surfaced as ``agent/failed`` during publication.
    """
    fake_runner = FakeProcessRunner()
    github_client = GitHubCliClient(tmp_path, fake_runner)

    github_client.sync_labels(LabelConfig())

    created_labels = [
        call[call.index("create") + 1]
        for call in fake_runner.calls
        if call[:3] == ["gh", "label", "create"]
    ]
    assert "validation/verifier-passed" in created_labels


def test_edit_issue_labels_creates_missing_label_and_retries(tmp_path: Path) -> None:
    """``gh issue edit`` failure on a missing label should auto-create + retry.

    Real-world scenario: a fresh repository that never ran ``labels sync``
    is missing ``validation/verifier-passed``. The runner still tries to
    add it after a green verifier verdict; instead of crashing, it should
    ``gh label create --force`` the missing labels and retry the edit.
    """

    class _MissingLabelRunner(FakeProcessRunner):
        """First ``gh issue edit`` fails with a not-found stderr; everything else OK."""

        def __init__(self) -> None:
            super().__init__()
            self.edit_attempts = 0

        def run(self, command, *, cwd, check=True, **kwargs):  # type: ignore[override]
            command_list = list(command)
            if command_list[:3] == ["gh", "issue", "edit"] and "--add-label" in command_list:
                self.edit_attempts += 1
                if self.edit_attempts == 1:
                    exc = subprocess.CalledProcessError(
                        returncode=1,
                        cmd=command_list,
                        output="",
                        stderr=(
                            "failed to update "
                            "https://github.com/GetRichTogether/TransMaster/pull/24: "
                            "'validation/verifier-passed' not found"
                        ),
                    )
                    if check:
                        raise exc
            return super().run(command, cwd=cwd, check=check, **kwargs)

    fake_runner = _MissingLabelRunner()
    github_client = GitHubCliClient(tmp_path, fake_runner)

    github_client.edit_issue_labels(24, add=["validation/verifier-passed"])

    # Order: view (current labels) → gh label create --force → gh issue edit
    # (retry). The first gh issue edit raises before being recorded in
    # ``calls`` (the FakeProcessRunner subclass raises before delegating to
    # ``super().run``, mirroring how a real subprocess ``CalledProcessError``
    # surfaces).
    assert fake_runner.calls == [
        [
            "gh",
            "issue",
            "view",
            "24",
            "--json",
            "labels",
        ],
        [
            "gh",
            "label",
            "create",
            "validation/verifier-passed",
            "--force",
        ],
        [
            "gh",
            "issue",
            "edit",
            "24",
            "--add-label",
            "validation/verifier-passed",
        ],
    ]
    assert fake_runner.edit_attempts == 2


def test_edit_issue_labels_does_not_swallow_unrelated_errors(tmp_path: Path) -> None:
    """Failures that do not look like missing-label errors must propagate as-is.

    We simulate a TLS timeout — transient infrastructure failure. The
    fallback must not attempt ``gh label create`` because that would mask
    the real cause and could even succeed on a non-existent label just to
    hide the underlying network failure.
    """

    class _TransientErrorRunner(FakeProcessRunner):
        """First ``gh issue edit`` raises a CalledProcessError with a TLS stderr."""

        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def run(self, command, *, cwd, check=True, **kwargs):  # type: ignore[override]
            command_list = list(command)
            if command_list[:3] == ["gh", "issue", "edit"]:
                self.attempts += 1
                exc = subprocess.CalledProcessError(
                    returncode=1,
                    cmd=command_list,
                    output="",
                    stderr="Post https://api.github.com/graphql: net/http: TLS handshake timeout",
                )
                if check:
                    raise exc
            return super().run(command, cwd=cwd, check=check, **kwargs)

    fake_runner = _TransientErrorRunner()
    github_client = GitHubCliClient(tmp_path, fake_runner)

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        github_client.edit_issue_labels(24, add=["validation/verifier-passed"])
    assert "TLS handshake timeout" in (excinfo.value.stderr or "")

    ensure_calls = [call for call in fake_runner.calls if call[:3] == ["gh", "label", "create"]]
    assert ensure_calls == []
    # Confirm we did NOT silently retry the edit; only the view call succeeded.
    edit_calls = [call for call in fake_runner.calls if call[:3] == ["gh", "issue", "edit"]]
    assert edit_calls == []
