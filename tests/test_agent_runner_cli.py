"""Tests for the CLI argument parsing and dispatch logic."""

from __future__ import annotations

from backend.api.cli import build_parser


def test_cli_parser_labels_sync() -> None:
    """labels sync subcommand should be recognized."""
    parser = build_parser()
    parsed = parser.parse_args(["labels", "sync"])
    assert parsed.command == "labels"
    assert parsed.labels_command == "sync"


def test_cli_parser_issue_from_prd_defaults() -> None:
    """issue-from-prd should have sensible defaults."""
    parser = build_parser()
    parsed = parser.parse_args(["issue-from-prd", "tasks/example.md"])
    assert parsed.command == "issue-from-prd"
    assert parsed.prd_path == "tasks/example.md"
    assert parsed.type == "feature"
    assert parsed.ready is False
    assert parsed.agent == "auto"
    assert parsed.publish_prd is False
    assert parsed.force is False


def test_cli_parser_issue_from_prd_publish_prd() -> None:
    """issue-from-prd should expose explicit PRD publishing."""
    parser = build_parser()
    parsed = parser.parse_args(
        ["issue-from-prd", "tasks/example.md", "--publish-prd", "--no-ready"]
    )
    assert parsed.command == "issue-from-prd"
    assert parsed.publish_prd is True
    assert parsed.ready is False


def test_cli_parser_run_once() -> None:
    """run-once should accept dry-run and agent flags."""
    parser = build_parser()
    parsed = parser.parse_args(
        ["run-once", "--dry-run", "--agent", "claude", "--max-issues", "5"]
    )
    assert parsed.command == "run-once"
    assert parsed.dry_run is True
    assert parsed.agent == "claude"
    assert parsed.max_issues == 5


def test_cli_parser_daemon() -> None:
    """daemon should accept interval and max-issues."""
    parser = build_parser()
    parsed = parser.parse_args(["daemon", "--interval", "300", "--max-issues", "2"])
    assert parsed.command == "daemon"
    assert parsed.interval == 300
    assert parsed.max_issues == 2


def test_cli_parser_repo_id() -> None:
    """--repo-id should be accepted on subcommands."""
    parser = build_parser()
    parsed = parser.parse_args(["run-once", "--repo-id", "keda"])
    assert parsed.repo_id == "keda"


def test_cli_parser_repo_and_repo_id_individually_parseable() -> None:
    """--repo and --repo-id should each be parseable individually."""
    parser = build_parser()
    parsed_repo = parser.parse_args(["run-once", "--repo", "/tmp/repo"])
    assert parsed_repo.repo == "/tmp/repo"
    assert parsed_repo.repo_id is None

    parsed_id = parser.parse_args(["run-once", "--repo-id", "keda"])
    assert parsed_id.repo_id == "keda"
    assert parsed_id.repo is None


def test_main_rejects_repo_and_repo_id_together() -> None:
    """main should exit 1 when both --repo and --repo-id are given."""
    from backend.api.cli import main

    exit_code = main(["run-once", "--repo", "/tmp/repo", "--repo-id", "keda"])
    assert exit_code == 1


def test_main_rejects_unknown_repo_id() -> None:
    """main should exit 1 when repo-id does not exist in config."""
    from unittest.mock import patch

    from backend.api.cli import main

    with patch(
        "backend.api.cli.resolve_repository_targets",
        side_effect=ValueError("not found"),
    ):
        exit_code = main(["run-once", "--repo-id", "nonexistent"])
        assert exit_code == 1


def test_main_labels_sync_iterates_multiple_repos() -> None:
    """labels sync without selector should call sync_labels for each repo."""
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from backend.api.cli import main

    mock_context_a = MagicMock()
    mock_context_a.repo_path = Path("/tmp/repo-a")
    mock_context_a.config.labels = MagicMock()
    mock_context_b = MagicMock()
    mock_context_b.repo_path = Path("/tmp/repo-b")
    mock_context_b.config.labels = MagicMock()

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context_a, mock_context_b],
    ), patch("backend.api.cli.sync_labels") as mock_sync, patch(
        "backend.api.cli.create_github_client"
    ):
        exit_code = main(["labels", "sync"])
        assert exit_code == 0
        assert mock_sync.call_count == 2


def test_main_issue_from_prd_defaults_to_cwd() -> None:
    """issue-from-prd without --repo or --repo-id should resolve to cwd."""
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path.cwd()
    mock_context.config.labels = MagicMock()
    mock_context.config.git.remote = "origin"
    mock_context.config.git.base_branch = "main"

    with patch(
        "backend.api.cli.resolve_issue_from_prd_target", return_value=mock_context
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.api.cli.create_issue_from_prd",
        return_value="https://github.com/example/issues/1",
    ), patch("backend.api.cli._prompt_and_publish_prd_if_needed", return_value=False):
        exit_code = main(["issue-from-prd", "tasks/example.md"])
        assert exit_code == 0
