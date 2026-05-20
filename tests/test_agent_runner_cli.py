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
    assert parsed.ready is True
    assert parsed.agent == "auto"
    assert parsed.force is False


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
