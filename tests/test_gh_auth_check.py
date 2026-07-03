"""Tests for GitHub CLI auth status detection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import CommandResult
from backend.infrastructure.github_client import GhAuthStatus, GitHubCliClient
from tests.conftest import FakeProcessRunner


def test_check_auth_status_authenticated(tmp_path: Path) -> None:
    """When gh auth status shows logged in, return authenticated."""
    command = ("gh", "auth", "status", "--hostname", "github.com")
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=0,
                stdout=("github.com\n" "  ✓ Logged in to github.com as testuser (oauth_token)\n"),
                stderr="",
            )
        }
    )
    client = GitHubCliClient(tmp_path, fake_runner)
    status = client.check_auth_status()
    assert status.authenticated is True
    assert status.account == "testuser"


def test_check_auth_status_token_invalid(tmp_path: Path) -> None:
    """When token is invalid, return not authenticated with reason."""
    command = ("gh", "auth", "status", "--hostname", "github.com")
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=1,
                stdout="",
                stderr=(
                    "github.com\n"
                    "  X Failed to log in to github.com: "
                    "The token in GITHUB_TOKEN is invalid.\n"
                    "  - The token in GITHUB_TOKEN is invalid.\n"
                ),
            )
        }
    )
    client = GitHubCliClient(tmp_path, fake_runner)
    status = client.check_auth_status()
    assert status.authenticated is False
    assert status.failure_reason is not None
    assert "Failed to log in" in status.failure_reason


def test_check_auth_status_not_logged_in(tmp_path: Path) -> None:
    """When not logged in, return not authenticated."""
    command = ("gh", "auth", "status", "--hostname", "github.com")
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=1,
                stdout="",
                stderr=(
                    "github.com\n"
                    "  X Failed to log in to github.com\n"
                    "  - To re-authenticate, run: gh auth login -h github.com\n"
                ),
            )
        }
    )
    client = GitHubCliClient(tmp_path, fake_runner)
    status = client.check_auth_status()
    assert status.authenticated is False
    assert status.failure_reason is not None


def test_ensure_gh_auth_or_prompt_exits_on_failure(tmp_path: Path) -> None:
    """_ensure_gh_auth_or_prompt should exit with code 1 on auth failure."""
    from backend.api.cli import _ensure_gh_auth_or_prompt

    command = ("gh", "auth", "status", "--hostname", "github.com")
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=1,
                stdout="",
                stderr=("github.com\n" "  X Failed to log in to github.com\n"),
            )
        }
    )
    with pytest.raises(SystemExit) as exc_info:
        _ensure_gh_auth_or_prompt(tmp_path, fake_runner)
    assert exc_info.value.code == 1


def test_ensure_gh_auth_or_prompt_skips_with_env_var(tmp_path: Path) -> None:
    """IAR_SKIP_GH_AUTH_CHECK=1 should skip the auth check."""
    from backend.api.cli import _ensure_gh_auth_or_prompt

    fake_runner = FakeProcessRunner()
    os.environ["IAR_SKIP_GH_AUTH_CHECK"] = "1"
    try:
        _ensure_gh_auth_or_prompt(tmp_path, fake_runner)
    finally:
        del os.environ["IAR_SKIP_GH_AUTH_CHECK"]


def test_ensure_gh_auth_or_prompt_passes_when_authenticated(tmp_path: Path) -> None:
    """_ensure_gh_auth_or_prompt should return normally when authenticated."""
    from backend.api.cli import _ensure_gh_auth_or_prompt

    command = ("gh", "auth", "status", "--hostname", "github.com")
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=0,
                stdout=("github.com\n" "  ✓ Logged in to github.com as testuser\n"),
                stderr="",
            )
        }
    )
    _ensure_gh_auth_or_prompt(tmp_path, fake_runner)


def test_main_labels_sync_exits_on_auth_failure(capsys) -> None:
    """iar labels sync should exit 1 with friendly message when gh auth fails."""
    from backend.api.cli import main
    from unittest.mock import MagicMock, patch

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.config.labels = MagicMock()

    with (
        patch(
            "backend.api.cli_helpers.resolve_repository_targets",
            return_value=[mock_context],
        ),
        patch("backend.api.cli_helpers.create_github_client") as mock_gh_client,
        patch("backend.api.cli.require_iar_repository_initialized"),
    ):
        mock_gh_client.return_value.check_auth_status.return_value = GhAuthStatus(
            authenticated=False,
            failure_reason="X Failed to log in to github.com",
        )
        exit_code = main(["labels", "sync"])

    assert exit_code == 1
    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"
    assert "GitHub CLI 认证失败" in combined
    assert "gh auth login -h github.com" in combined
