"""Tests for the CLI argument parsing and dispatch logic."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.api.cli import build_parser
from backend.infrastructure.logging.logger import Logger


def _reset_logger_singleton() -> None:
    """Reset Logger singleton state for test isolation."""
    if Logger._logger is not None:
        for handler in Logger._logger.handlers[:]:
            handler.close()
            Logger._logger.removeHandler(handler)
    Logger._instance = None
    Logger._logger = None


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
    from backend.api.cli import main

    with patch(
        "backend.api.cli.resolve_repository_targets",
        side_effect=ValueError("not found"),
    ):
        exit_code = main(["run-once", "--repo-id", "nonexistent"])
        assert exit_code == 1


def test_main_labels_sync_iterates_multiple_repos() -> None:
    """labels sync without selector should call sync_labels for each repo."""
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


def test_cli_parser_deliberate_defaults() -> None:
    """deliberate should have sensible defaults."""
    parser = build_parser()
    parsed = parser.parse_args(["deliberate", "test prompt"])
    assert parsed.command == "deliberate"
    assert parsed.prompt == "test prompt"
    assert parsed.agents == "architect,skeptic,implementer"
    assert parsed.rounds is None
    assert parsed.synthesizer is None
    assert parsed.output is None
    assert parsed.session_id is None


def test_cli_parser_deliberate_custom_agents() -> None:
    """deliberate should accept custom agents and rounds."""
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "deliberate",
            "test prompt",
            "--agents",
            "architect,skeptic",
            "--rounds",
            "3",
            "--synthesizer",
            "kimi",
            "--output",
            "/tmp/out",
            "--session-id",
            "sid-1",
        ]
    )
    assert parsed.command == "deliberate"
    assert parsed.prompt == "test prompt"
    assert parsed.agents == "architect,skeptic"
    assert parsed.rounds == 3
    assert parsed.synthesizer == "kimi"
    assert parsed.output == "/tmp/out"
    assert parsed.session_id == "sid-1"


def test_main_deliberate_uses_single_session_output_path(tmp_path) -> None:
    """deliberate should pass the finalized session directory to all writers."""
    from backend.api.cli import main
    from backend.core.shared.models.agent_deliberation import (
        DeliberationAgentProfile,
        DeliberationResult,
    )

    output_root = tmp_path / "deliberations"
    expected_output_path = output_root / "sid-1"
    captured = {}

    def fake_run_agent_deliberation(**kwargs):
        request = kwargs["request"]
        captured["request"] = request
        return DeliberationResult(
            session_id=request.session_id,
            prompt=request.prompt,
            recommendation="do it",
            consensus="agree",
            disagreements="none",
            risks="low",
            next_actions="next",
            events=(),
            agent_outputs={
                "round_1": {
                    "skeptic": "skeptic out",
                    "architect": "architect out",
                }
            },
            output_dir=request.output_dir,
            started_at="2026-05-23T00:00:00+00:00",
            finished_at="2026-05-23T00:01:00+00:00",
        )

    with patch("backend.api.cli.create_process_runner"), patch(
        "backend.api.cli.get_agent_runner_settings"
    ) as mock_settings, patch(
        "backend.api.cli.build_deliberation_config_from_settings"
    ) as mock_config, patch("backend.api.cli.create_transcript_runner"), patch(
        "backend.api.cli.create_event_sink"
    ) as mock_event_sink, patch(
        "backend.api.cli.run_agent_deliberation",
        side_effect=fake_run_agent_deliberation,
    ), patch("backend.api.cli.write_deliberation_outputs") as mock_write:
        mock_settings.return_value.deliberation.default_output_dir = str(output_root)
        mock_settings.return_value.deliberation.default_rounds = 2
        mock_settings.return_value.deliberation.default_synthesizer = "claude"
        mock_config.return_value.profiles = (
            DeliberationAgentProfile(
                profile_id="architect",
                agent="claude",
                role="architect",
                behavior_prompt="be an architect",
            ),
            DeliberationAgentProfile(
                profile_id="skeptic",
                agent="kimi",
                role="skeptic",
                behavior_prompt="be a skeptic",
            ),
        )
        mock_event_sink.return_value = MagicMock()

        exit_code = main(["deliberate", "test prompt", "--session-id", "sid-1"])

    assert exit_code == 0
    assert captured["request"].session_id == "sid-1"
    assert captured["request"].output_dir == str(expected_output_path)
    mock_event_sink.assert_called_once_with(expected_output_path)
    mock_write.assert_called_once()
    assert mock_write.call_args.args[2] == expected_output_path
    assert tuple(
        profile.profile_id for profile in mock_write.call_args.args[1].profiles
    ) == (
        "skeptic",
        "architect",
    )


# New tests for CLI logging configuration


