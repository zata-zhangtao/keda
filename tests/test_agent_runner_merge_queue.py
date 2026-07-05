"""Tests for the autopilot merge queue use case."""

from __future__ import annotations

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    AutopilotConfig,
    IssueSummary,
    LabelConfig,
    PullRequestContext,
    SafetyConfig,
)
from backend.core.use_cases import agent_runner_merge_queue as merge_queue_module
from backend.core.use_cases.agent_runner_merge_queue import (
    _autopilot_enabled,
    _tick_sign_off_checklist,
    process_merge_queue,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner

# A PR checklist block as produced by build_validation_checklist_block().
_UNTICKED_BODY = (
    "<!-- iar:realistic-validation version=1 total=2 -->\n"
    "## Realistic Validation (human sign-off required)\n\n"
    "Then tick each item:\n\n"
    "- [ ] rv-1: first checklist behaviour\n"
    "- [ ] rv-2: second checklist behaviour\n\n"
    "<!-- iar:realistic-validation-end -->"
)
_TICKED_BODY = (
    "<!-- iar:realistic-validation version=1 total=2 -->\n"
    "## Realistic Validation (human sign-off required)\n\n"
    "Then tick each item:\n\n"
    "- [x] rv-1: first checklist behaviour\n"
    "- [x] rv-2: second checklist behaviour\n\n"
    "<!-- iar:realistic-validation-end -->"
)


def _make_config(
    *,
    autopilot_enabled: bool = True,
    auto_merge: bool = True,
    require_verifier_pass: bool = True,
    auto_sign_off: bool = True,
    merge_check_timeout_seconds: int = 0,
    merge_method: str = "squash",
    forbidden_path_patterns: tuple[str, ...] = (".env", ".env.*", "secrets/*"),
) -> AppConfig:
    """Build a minimal AppConfig for merge queue tests."""
    return AppConfig(
        labels=LabelConfig(),
        safety=SafetyConfig(auto_merge=auto_merge, forbidden_path_patterns=forbidden_path_patterns),
        autopilot=AutopilotConfig(
            enabled=autopilot_enabled,
            merge_method=merge_method,
            require_verifier_pass=require_verifier_pass,
            auto_sign_off=auto_sign_off,
            merge_check_timeout_seconds=merge_check_timeout_seconds,
        ),
    )


def _make_issue(*, number: int, labels: tuple[str, ...] = ()) -> IssueSummary:
    return IssueSummary(
        number=number,
        title=f"Issue #{number}",
        url=f"https://github.com/example/repo/issues/{number}",
        body="",
        labels=labels,
    )


def _make_pr_context(
    *, branch: str, head_sha: str = "abc1234", number: int = 50
) -> PullRequestContext:
    # Default the PR number from the trailing digit if present, else to 50.
    return PullRequestContext(
        pr_url=f"https://github.com/example/repo/pull/{number}",
        branch=branch,
        head_sha=head_sha,
        base_sha="base1234",
        mergeable=True,
        checks_state="SUCCESS",
        number=number,
        body="",
    )


@pytest.fixture
def worktree_path(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Stub ``create_or_reuse_worktree`` to return a temp path with no real git work."""
    path = tmp_path / "worktree"
    path.mkdir()

    def fake_create_or_reuse(repo_path, issue, config, process_runner):  # noqa: ARG001
        return path

    monkeypatch.setattr(merge_queue_module, "create_or_reuse_worktree", fake_create_or_reuse)
    # The helper imports it under a different name; rebind for clarity.
    monkeypatch.setattr(
        "backend.core.use_cases.run_agent_once.create_or_reuse_worktree",
        fake_create_or_reuse,
    )
    return path


def test_kill_switch_off_returns_no_op(tmp_path, monkeypatch) -> None:
    """Both switches must be on; either off is a no-op."""
    github = FakeGitHubClient()
    runner = FakeProcessRunner()
    config = _make_config(autopilot_enabled=True, auto_merge=False)
    exit_code, outcomes = process_merge_queue(
        repo_path=tmp_path,
        config=config,
        github_client=github,
        process_runner=runner,
        supervisor_agent="auto",
    )
    assert exit_code == 0
    assert outcomes == []
    merge_calls = [c for c in github.calls if c.get("method") == "merge_pull_request"]
    assert merge_calls == []


def test_autopilot_off_returns_no_op(tmp_path) -> None:
    config = _make_config(autopilot_enabled=False, auto_merge=True)
    assert _autopilot_enabled(config) is False
    github = FakeGitHubClient()
    runner = FakeProcessRunner()
    exit_code, outcomes = process_merge_queue(
        repo_path=tmp_path,
        config=config,
        github_client=github,
        process_runner=runner,
        supervisor_agent="auto",
    )
    assert exit_code == 0
    assert outcomes == []


def test_fifo_ordering_processes_lowest_issue_first(tmp_path, worktree_path, monkeypatch) -> None:
    """FIFO is by Issue number; lower Issue numbers are processed first."""
    monkeypatch.setattr(
        merge_queue_module,
        "_wait_for_checks_green",
        lambda *a, **kw: (True, _make_pr_context(branch="issue-100")),
    )
    github = FakeGitHubClient()
    runner = FakeProcessRunner()
    cfg = _make_config(merge_check_timeout_seconds=0)
    processed_numbers = []

    def fake_process(repo_path, config, issue, github_client, process_runner, supervisor_agent):  # noqa: ARG001
        processed_numbers.append(issue.number)
        from backend.core.use_cases.agent_runner_merge_queue import MergeQueueOutcome

        return MergeQueueOutcome(issue_number=issue.number, action="merged")

    monkeypatch.setattr(merge_queue_module, "_process_one", fake_process)
    github.set_list_issues_by_label_result(
        [
            _make_issue(number=102),
            _make_issue(number=100),
            _make_issue(number=101),
        ]
    )
    process_merge_queue(
        repo_path=tmp_path,
        config=cfg,
        github_client=github,
        process_runner=runner,
        supervisor_agent="auto",
    )
    assert processed_numbers == [100, 101, 102]


def test_verifier_missing_label_skips_issue(tmp_path, worktree_path, monkeypatch) -> None:
    """When validation is required and the verifier-passed label is missing, skip."""
    monkeypatch.setattr(
        merge_queue_module,
        "_wait_for_checks_green",
        lambda *a, **kw: (True, _make_pr_context(branch="issue-50")),
    )
    github = FakeGitHubClient()
    runner = FakeProcessRunner()

    github.set_list_issues_by_label_result([_make_issue(number=50, labels=("agent/review",))])

    issue_body = (
        "## Realistic Validation\n\n- [ ] rv-1: thing\n"  # forces validation_required=True.
    )
    monkeypatch.setattr(merge_queue_module, "validation_required", lambda *a, **kw: True)

    # PR context for the mock branch.
    pr = _make_pr_context(branch="issue-50", number=50)
    github.set_pr_context("issue-50", pr)

    # Set issue body to ensure validation_required returns True.
    real_issue = IssueSummary(
        number=50,
        title="Issue #50",
        url="https://github.com/example/repo/issues/50",
        body=issue_body,
        labels=("agent/review",),  # NB: no validation/verifier-passed label
    )
    branch_comment = "Build complete on PR Branch: `issue-50`."
    github._issue_comments[50] = [branch_comment]
    github._issue_comment_entries[50] = [(1, branch_comment)]

    real_config = _make_config(require_verifier_pass=True)

    # Drive the real processing pipeline; it must short-circuit before merge.
    outcome = merge_queue_module._process_one(
        repo_path=tmp_path,
        config=real_config,
        issue=real_issue,
        github_client=github,
        process_runner=runner,
        supervisor_agent="auto",
    )
    assert outcome.action == "skipped_verifier_missing"
    # Confirm no merge attempt was made.
    merge_calls = [c for c in github.calls if c.get("method") == "merge_pull_request"]
    assert merge_calls == []
    # The fast-path branch in _process_one exits before doing any body mutation,
    # so auto sign-off should not run.
    body_calls = [c for c in github.calls if c.get("method") == "update_pull_request_body"]
    assert body_calls == []


def test_verifier_passed_then_full_path_merges(tmp_path, worktree_path, monkeypatch) -> None:
    """Full happy path: verifier green → tick sign-off → rebase ok → verify green
    → forbidden path clean → checks green → squash merge + audit comment."""
    cfg = _make_config(require_verifier_pass=True)
    github = FakeGitHubClient()
    runner = FakeProcessRunner()
    branch = "issue-99"
    pr = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/99",
        branch=branch,
        head_sha="deadbeef",
        base_sha="base1234",
        mergeable=True,
        checks_state="SUCCESS",
        number=99,
        body=_UNTICKED_BODY,
    )
    github.set_pr_context(branch, pr)
    issue = _make_issue(number=99, labels=("agent/review", "validation/verifier-passed"))
    github._issue_labels[99] = issue.labels
    github.set_list_issues_by_label_result([issue])

    # Pre-load comments with a "PR Branch: `issue-99`" marker so pr_branch is found.
    branch_marker_comment = "Build complete on PR Branch: `issue-99`."
    github._issue_comments[99] = [branch_marker_comment]
    github._issue_comment_entries[99] = [(1, branch_marker_comment)]

    def fake_run_verification(*args, **kwargs):  # noqa: ARG001,ANN002,ANN003
        from backend.core.shared.models.agent_runner import CommandResult

        return [
            CommandResult(command=("git", "diff", "--check"), return_code=0, stdout="", stderr="")
        ]

    monkeypatch.setattr(merge_queue_module, "run_verification", fake_run_verification)
    monkeypatch.setattr(merge_queue_module, "execute_rebase", lambda **kwargs: [])
    monkeypatch.setattr(merge_queue_module, "_diff_paths", lambda *args, **kwargs: ["src/main.py"])

    exit_code, outcomes = process_merge_queue(
        repo_path=tmp_path,
        config=cfg,
        github_client=github,
        process_runner=runner,
        supervisor_agent="auto",
    )
    assert exit_code == 0
    assert len(outcomes) == 1
    assert outcomes[0].action == "merged"

    merge_calls = [c for c in github.calls if c.get("method") == "merge_pull_request"]
    assert len(merge_calls) == 1
    assert merge_calls[0]["method_kwarg"] == "squash"
    assert merge_calls[0]["pr_number"] == 99

    body_updates = [c for c in github.calls if c.get("method") == "update_pull_request_body"]
    assert len(body_updates) == 1
    assert "- [x] rv-1" in body_updates[0]["body"]
    assert "- [x] rv-2" in body_updates[0]["body"]


def test_idempotent_no_duplicate_sign_off_comment(tmp_path, worktree_path, monkeypatch) -> None:
    """If a previous run already produced an ``iar:auto-sign-off`` comment,
    the merge queue must NOT post another one — that is the crash-re-entry guarantee."""
    cfg = _make_config(require_verifier_pass=False)
    github = FakeGitHubClient()
    runner = FakeProcessRunner()
    branch = "issue-77"
    pr = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/77",
        branch=branch,
        head_sha="sha1",
        base_sha="base1",
        mergeable=True,
        checks_state="SUCCESS",
        number=77,
        body=_UNTICKED_BODY,
    )
    github.set_pr_context(branch, pr)
    issue = _make_issue(number=77, labels=("agent/review",))
    github._issue_labels[77] = issue.labels
    branch_comment = "Build complete on PR Branch: `issue-77`."
    github._issue_comments[77] = [branch_comment, "<!-- iar:auto-sign-off previous -->"]
    github._issue_comment_entries[77] = [
        (1, branch_comment),
        (2, "<!-- iar:auto-sign-off previous -->"),
    ]
    github.set_list_issues_by_label_result([issue])

    monkeypatch.setattr(merge_queue_module, "run_verification", lambda *a, **kw: [])
    monkeypatch.setattr(merge_queue_module, "execute_rebase", lambda **kw: [])
    monkeypatch.setattr(merge_queue_module, "_diff_paths", lambda *args, **kwargs: ["src/main.py"])

    process_merge_queue(
        repo_path=tmp_path,
        config=cfg,
        github_client=github,
        process_runner=runner,
        supervisor_agent="auto",
    )

    # Body still gets ticked (idempotent at the body level), but no comment is added.
    body_updates = [c for c in github.calls if c["method"] == "update_pull_request_body"]
    assert len(body_updates) == 1
    added_comments = github._issue_comments.get(77, [])
    auto_signoff_comment_count = sum(
        1 for body in added_comments if "## Autopilot auto sign-off" in body
    )
    # Two baseline comments + zero auto-sign-off comments.
    assert auto_signoff_comment_count == 0


def test_forbidden_path_blocks_and_comments(tmp_path, worktree_path, monkeypatch) -> None:
    """Forbidden path hit must transition to ``agent/blocked`` and not merge."""
    cfg = _make_config(merge_check_timeout_seconds=0)
    github = FakeGitHubClient()
    runner = FakeProcessRunner()
    branch = "issue-12"
    pr = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/12",
        branch=branch,
        head_sha="x",
        base_sha="y",
        mergeable=True,
        checks_state="SUCCESS",
        number=12,
        body="",
    )
    github.set_pr_context(branch, pr)
    issue = _make_issue(number=12, labels=("agent/review",))
    github._issue_labels[12] = issue.labels
    github._issue_comments[12] = ["Build complete on PR Branch: `issue-12`."]
    github._issue_comment_entries[12] = [(1, "Build complete on PR Branch: `issue-12`.")]
    github.set_list_issues_by_label_result([issue])

    monkeypatch.setattr(merge_queue_module, "run_verification", lambda *a, **kw: [])
    monkeypatch.setattr(merge_queue_module, "execute_rebase", lambda **kw: [])
    monkeypatch.setattr(merge_queue_module, "_diff_paths", lambda *a, **kw: [".env"])

    process_merge_queue(
        repo_path=tmp_path,
        config=cfg,
        github_client=github,
        process_runner=runner,
        supervisor_agent="auto",
    )

    merge_calls = [c for c in github.calls if c["method"] == "merge_pull_request"]
    assert merge_calls == []
    label_calls = [c for c in github.calls if c["method"] == "edit_issue_labels"]
    blocked_adds = [c for c in label_calls if "agent/blocked" in c.get("add", [])]
    assert any(c["issue_number"] == 12 for c in blocked_adds)
    comments = github._issue_comments.get(12, [])
    assert any("Forbidden Path" in body for body in comments)


def test_verification_red_does_not_merge(tmp_path, worktree_path, monkeypatch) -> None:
    """A failing verification command must short-circuit before the merge call."""
    cfg = _make_config(merge_check_timeout_seconds=0)
    github = FakeGitHubClient()
    runner = FakeProcessRunner()
    branch = "issue-33"
    pr = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/33",
        branch=branch,
        head_sha="a",
        base_sha="b",
        mergeable=True,
        checks_state="SUCCESS",
        number=33,
        body="",
    )
    github.set_pr_context(branch, pr)
    issue = _make_issue(number=33, labels=("agent/review",))
    github._issue_labels[33] = issue.labels
    github._issue_comments[33] = ["Build complete on PR Branch: `issue-33`."]
    github.set_list_issues_by_label_result([issue])

    monkeypatch.setattr(merge_queue_module, "execute_rebase", lambda **kw: [])

    from backend.core.shared.models.agent_runner import CommandResult

    def fake_run_verification_red(*args, **kwargs):  # noqa: ARG001
        return [
            CommandResult(
                command=("bash", "-lc", "git diff --check"), return_code=1, stdout="", stderr="boom"
            )
        ]

    monkeypatch.setattr(merge_queue_module, "run_verification", fake_run_verification_red)
    monkeypatch.setattr(merge_queue_module, "_diff_paths", lambda *a, **kw: ["src/main.py"])

    process_merge_queue(
        repo_path=tmp_path,
        config=cfg,
        github_client=github,
        process_runner=runner,
        supervisor_agent="auto",
    )

    merge_calls = [c for c in github.calls if c["method"] == "merge_pull_request"]
    assert merge_calls == []
    comments = github._issue_comments.get(33, [])
    assert any("Verification Failed" in body for body in comments)


def test_tick_sign_off_checklist_helper() -> None:
    """Unit test for the deterministic checklist-ticking helper."""
    assert _tick_sign_off_checklist(_UNTICKED_BODY) is not None
    ticked = _tick_sign_off_checklist(_UNTICKED_BODY)
    assert ticked is not None
    assert "- [x] rv-1" in ticked
    assert "- [x] rv-2" in ticked
    # Idempotent: ticking an already-ticked body returns None (no change).
    assert _tick_sign_off_checklist(_TICKED_BODY) is None
    # No checklist → returns None.
    assert _tick_sign_off_checklist("") is None
