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
