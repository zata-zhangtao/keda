"""Tests for the GitHub CLI infrastructure adapter."""

from __future__ import annotations

import json
from pathlib import Path

from backend.core.shared.models.agent_runner import CommandResult
from backend.infrastructure.github_client import GitHubCliClient
from tests.conftest import FakeProcessRunner


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
        "url,headRefName,headRefOid,baseRefOid,mergeable,statusCheckRollup",
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
        "url,headRefName,headRefOid,baseRefOid,mergeable,statusCheckRollup",
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
        "number,title,url,labels,body",
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
        "number,title,url,labels,body",
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
