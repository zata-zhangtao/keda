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
