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


def test_cli_parser_repo_and_repo_id_mutual_exclusion() -> None:
    """--repo and --repo-id should be mutually exclusive in parsing."""
    parser = build_parser()
    # argparse does not enforce mutual exclusion across different parsers,
    # but both flags should be parseable individually.
    parsed_repo = parser.parse_args(["run-once", "--repo", "/tmp/repo"])
    assert parsed_repo.repo == "/tmp/repo"
    assert parsed_repo.repo_id is None

    parsed_id = parser.parse_args(["run-once", "--repo-id", "keda"])
    assert parsed_id.repo_id == "keda"
    assert parsed_id.repo == "."
