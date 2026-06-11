"""Tests for the CLI argument parsing and dispatch logic."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from backend.api.cli import build_parser
from backend.infrastructure.logging.logger import Logger


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    """Remove terminal color/control sequences from captured CLI output."""
    return _ANSI_ESCAPE_RE.sub("", text)


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


def test_cli_parser_init() -> None:
    """init should accept repository-local config options."""
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "init",
            "--dry-run",
            "--force",
            "--id",
            "target",
            "--display-name",
            "Target",
            "--remote",
            "upstream",
            "--base-branch",
            "develop",
        ]
    )
    assert parsed.command == "init"
    assert parsed.dry_run is True
    assert parsed.force is True
    assert parsed.repository_id == "target"
    assert parsed.display_name == "Target"
    assert parsed.remote == "upstream"
    assert parsed.base_branch == "develop"


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


def test_cli_parser_issue_from_prd_dependency_options() -> None:
    """issue-from-prd should accept dependency gate options."""
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "issue-from-prd",
            "tasks/example.md",
            "--group",
            "downstream",
            "--depends-on",
            "42",
            "--depends-on",
            "43",
            "--depends-on-group",
            "upstream-a",
        ]
    )
    assert parsed.group == "downstream"
    assert parsed.depends_on == [42, 43]
    assert parsed.depends_on_group == ["upstream-a"]


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


def test_cli_parser_worktree_cleanup() -> None:
    """worktree cleanup should expose dry-run, yes, and force flags."""
    parser = build_parser()
    parsed = parser.parse_args(["worktree", "cleanup", "--dry-run", "--force"])
    assert parsed.command == "worktree"
    assert parsed.worktree_command == "cleanup"
    assert parsed.dry_run is True
    assert parsed.yes is False
    assert parsed.force is True


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


def test_cli_parser_all_repositories() -> None:
    """--all should be accepted by multi-target commands."""
    parser = build_parser()
    parsed = parser.parse_args(["run-once", "--all"])
    assert parsed.all_repositories is True


def test_cli_parser_repo_and_repo_id_individually_parseable() -> None:
    """--repo and --repo-id should each be parseable individually."""
    parser = build_parser()
    parsed_repo = parser.parse_args(["run-once", "--repo", "/tmp/repo"])
    assert parsed_repo.repo == "/tmp/repo"
    assert parsed_repo.repo_id is None

    parsed_id = parser.parse_args(["run-once", "--repo-id", "keda"])
    assert parsed_id.repo_id == "keda"
    assert parsed_id.repo is None


def test_main_no_args_shows_help_without_traceback(capsys) -> None:
    """No-argument Typer entrypoint should show help without leaking internals."""
    from backend.api.cli import main

    exit_code = main([])
    captured = capsys.readouterr()
    combined_output = _strip_ansi(f"{captured.out}\n{captured.err}")

    assert exit_code == 0
    assert "Usage: iar" in combined_output
    assert "Commands" in combined_output
    assert "Traceback" not in combined_output
    assert "NoArgsIsHelpError" not in combined_output


def test_main_completion_show_zsh_outputs_script(capsys) -> None:
    """completion show should print a zsh script for iAR."""
    from backend.api.cli import main

    exit_code = main(["completion", "show", "--shell", "zsh"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "#compdef iar" in captured.out
    assert "_IAR_COMPLETE=complete_zsh" in captured.out


def test_main_completion_install_zsh_writes_user_files(tmp_path, monkeypatch) -> None:
    """completion install should write zsh completion under the user's home."""
    from backend.api.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))

    exit_code = main(["completion", "install", "--shell", "zsh"])

    completion_path = tmp_path / ".zsh" / "completions" / "_iar"
    zshrc_path = tmp_path / ".zshrc"
    assert exit_code == 0
    assert "#compdef iar" in completion_path.read_text(encoding="utf-8")
    zshrc_text = zshrc_path.read_text(encoding="utf-8")
    assert "autoload -Uz compinit && compinit" in zshrc_text
    assert f'[ -f "{completion_path}" ] && source "{completion_path}"' in zshrc_text


def test_main_completion_protocol_matches_issue_prefix(capsys, monkeypatch) -> None:
    """Shell completion protocol should complete iar is<Tab> to issue commands."""
    from backend.api.cli import main

    monkeypatch.setenv("_IAR_COMPLETE", "complete_bash")
    monkeypatch.setenv("COMP_WORDS", "iar is")
    monkeypatch.setenv("COMP_CWORD", "1")

    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "issue" in captured.out.splitlines()
    assert "issue-from-prd" in captured.out.splitlines()


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


def test_main_passes_all_repositories_selector() -> None:
    """main should pass --all to repository target resolution."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.repo_id = "repo"
    mock_context.display_name = "Repo"

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ) as mock_resolve, patch(
        "backend.api.cli.run_agent_repositories_once", return_value=0
    ), patch("backend.api.cli.create_github_client"):
        exit_code = main(["run-once", "--all", "--dry-run"])

    assert exit_code == 0
    assert mock_resolve.call_args.kwargs["all_repositories"] is True


def test_main_run_alias_passes_all_repositories_selector() -> None:
    """Modern run alias should dispatch to run-once with the same selectors."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.repo_id = "repo"
    mock_context.display_name = "Repo"

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ) as mock_resolve, patch(
        "backend.api.cli.run_agent_repositories_once", return_value=0
    ) as mock_run, patch("backend.api.cli.create_github_client"):
        exit_code = main(["run", "--all", "--dry-run", "--agent", "codex"])

    assert exit_code == 0
    assert mock_resolve.call_args.kwargs["all_repositories"] is True
    assert mock_run.call_args.kwargs["dry_run"] is True
    assert mock_run.call_args.kwargs["agent"] == "codex"


def test_main_typer_top_level_repo_selector_is_honored() -> None:
    """Typer entrypoint should accept repository selectors before the command."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.repo_id = "repo"
    mock_context.display_name = "Repo"

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ) as mock_resolve, patch(
        "backend.api.cli.run_agent_repositories_once", return_value=0
    ), patch("backend.api.cli.create_github_client"):
        exit_code = main(["--repo", "/tmp/repo", "run", "--dry-run"])

    assert exit_code == 0
    assert mock_resolve.call_args.kwargs["repo_path_override"] == "/tmp/repo"


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


def test_main_issue_create_alias_matches_issue_from_prd() -> None:
    """Modern issue create alias should use the existing PRD issue workflow."""
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
    ) as mock_create, patch(
        "backend.api.cli._prompt_and_publish_prd_if_needed"
    ) as mock_prompt:
        exit_code = main(
            ["issue", "create", "tasks/example.md", "--publish-prd", "--ready"]
        )

    assert exit_code == 0
    assert mock_create.call_args.kwargs["request"].prd_path == Path("tasks/example.md")
    assert mock_create.call_args.kwargs["request"].queue_ready is True
    mock_prompt.assert_not_called()


def test_main_issue_create_failure_prints_command_output(capsys) -> None:
    """issue create failures should include captured command stdout and stderr."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path.cwd()
    mock_context.config.labels = MagicMock()
    mock_context.config.git.remote = "origin"
    mock_context.config.git.base_branch = "main"

    commit_error = subprocess.CalledProcessError(
        1,
        [
            "git",
            "commit",
            "-m",
            "docs(prd): publish example",
            "--",
            "tasks/example.md",
        ],
        output="pre-commit stdout\n",
        stderr="trailing whitespace\n",
    )

    with patch(
        "backend.api.cli.resolve_issue_from_prd_target", return_value=mock_context
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.api.cli.create_issue_from_prd",
        return_value="https://github.com/example/issues/1",
    ), patch(
        "backend.api.cli._prompt_and_publish_prd_if_needed",
        side_effect=commit_error,
    ):
        exit_code = main(["issue", "create", "tasks/example.md"])

    captured = capsys.readouterr()
    combined_output = f"{captured.out}\n{captured.err}"

    assert exit_code == 1
    assert "iar failed:" in combined_output
    assert (
        "Command: git commit -m 'docs(prd): publish example' -- tasks/example.md"
        in (combined_output)
    )
    assert "Exit code: 1" in combined_output
    assert "stdout:" in combined_output
    assert "pre-commit stdout" in combined_output
    assert "stderr:" in combined_output
    assert "trailing whitespace" in combined_output


def test_main_issue_from_prd_ready_without_publish_defers_label() -> None:
    """--ready without --publish-prd should not ready the Issue until PRD is pushed.

    时序说明：
    ┌─────────────────────────────────────────────────────────┐
    │ cli.py                                                 │
    │   queue_ready_for_request = False  # 无 --publish-prd   │
    │   create_issue_from_prd(queue_ready=False)  → Issue不含ready │
    │   _prompt_and_publish_prd_if_needed(queue_ready=True)   │
    │     └─ 用户确认push → edit_issue_labels add ready      │
    └─────────────────────────────────────────────────────────┘

    关键断言：
    1. create_issue_from_prd 收到 queue_ready=False（Issue 创建时不带 ready）
    2. _prompt_and_publish_prd_if_needed 收到 queue_ready=True（交互 prompt 可以在 push 成功后补 ready）
    """
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
    ) as mock_create, patch(
        "backend.api.cli._prompt_and_publish_prd_if_needed", return_value=False
    ) as mock_prompt:
        exit_code = main(["issue-from-prd", "tasks/example.md", "--ready"])
        assert exit_code == 0
        # create_issue_from_prd should be called with queue_ready=False
        assert mock_create.call_args.kwargs["request"].queue_ready is False
        # prompt should still receive queue_ready=True so it can add the label after push
        assert mock_prompt.call_args.kwargs["queue_ready"] is True


def test_main_issue_from_prd_ready_with_publish_keeps_label() -> None:
    """--ready with --publish-prd should let create_issue_from_prd handle ready gating.

    时序说明：
    ┌─────────────────────────────────────────────────────────┐
    │ cli.py                                                 │
    │   queue_ready_for_request = True   # 有 --publish-prd   │
    │   create_issue_from_prd(queue_ready=True)  → Issue含ready │
    │   # 不进入 _prompt_and_publish_prd_if_needed 分支       │
    │   # （ready 由 core 内部在 push 成功后添加）            │
    └─────────────────────────────────────────────────────────┘

    关键断言：
    1. create_issue_from_prd 收到 queue_ready=True（core 内部处理 ready gating）
    2. _prompt_and_publish_prd_if_needed 不被调用（--publish-prd 时走非交互路径）
    """
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
    ) as mock_create, patch(
        "backend.api.cli._prompt_and_publish_prd_if_needed"
    ) as mock_prompt:
        exit_code = main(
            ["issue-from-prd", "tasks/example.md", "--publish-prd", "--ready"]
        )
        assert exit_code == 0
        assert mock_create.call_args.kwargs["request"].queue_ready is True
        # prompt should not be called when --publish-prd is used
        mock_prompt.assert_not_called()


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
    mock_event_sink.assert_called_once_with(expected_output_path, ANY)
    mock_write.assert_called_once()
    assert mock_write.call_args.args[2] == expected_output_path
    assert tuple(
        profile.profile_id for profile in mock_write.call_args.args[1].profiles
    ) == (
        "skeptic",
        "architect",
    )


def test_main_review_alias_dispatches_review_once() -> None:
    """Modern review alias should dispatch to the review-once workflow."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.config = MagicMock()
    mock_context.repo_id = "repo"

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.create_github_client") as mock_client, patch(
        "backend.api.cli.review_once", return_value=0
    ) as mock_review:
        exit_code = main(["review", "--dry-run", "--agent", "claude"])

    assert exit_code == 0
    mock_client.assert_called_with(mock_context.repo_path, ANY)
    assert mock_review.call_args.kwargs["dry_run"] is True
    assert mock_review.call_args.kwargs["agent"] == "claude"


def test_cli_parser_recover_publish_required_args() -> None:
    """recover-publish should require --issue."""
    parser = build_parser()
    parsed = parser.parse_args(["recover-publish", "--issue", "5"])
    assert parsed.command == "recover-publish"
    assert parsed.issue == 5
    assert parsed.branch is None


def test_cli_parser_recover_publish_with_branch() -> None:
    """recover-publish should accept optional --branch."""
    parser = build_parser()
    parsed = parser.parse_args(
        ["recover-publish", "--issue", "5", "--branch", "feature-xyz"]
    )
    assert parsed.command == "recover-publish"
    assert parsed.issue == 5
    assert parsed.branch == "feature-xyz"


def test_cli_parser_recover_publish_missing_issue() -> None:
    """recover-publish should fail without --issue."""
    import pytest as _pytest

    parser = build_parser()
    with _pytest.raises(SystemExit):
        parser.parse_args(["recover-publish"])


def test_cli_parser_blocked_continue_required_args() -> None:
    """blocked-continue should require --issue."""
    parser = build_parser()
    parsed = parser.parse_args(["blocked-continue", "--issue", "7"])
    assert parsed.command == "blocked-continue"
    assert parsed.issue == 7
    assert parsed.agent == "auto"


def test_cli_parser_blocked_continue_with_agent() -> None:
    """blocked-continue should accept --agent override."""
    parser = build_parser()
    parsed = parser.parse_args(
        ["blocked-continue", "--issue", "7", "--agent", "claude"]
    )
    assert parsed.command == "blocked-continue"
    assert parsed.issue == 7
    assert parsed.agent == "claude"


def test_cli_parser_blocked_continue_missing_issue() -> None:
    """blocked-continue should fail without --issue."""
    import pytest as _pytest

    parser = build_parser()
    with _pytest.raises(SystemExit):
        parser.parse_args(["blocked-continue"])


def test_main_blocked_continue_success(capsys) -> None:
    """blocked-continue should print success when claimed."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.config = MagicMock()
    mock_context.config.labels.blocked = "agent/blocked"

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.core.use_cases.blocked_continue.blocked_continue_issue",
        return_value=True,
    ) as mock_blocked:
        exit_code = main(["blocked-continue", "--issue", "42"])

    assert exit_code == 0
    mock_blocked.assert_called_once()
    assert mock_blocked.call_args.kwargs["issue_number"] == 42
    captured = capsys.readouterr()
    assert "Issue #42 resumed successfully" in captured.out


def test_main_blocked_continue_already_claimed(capsys) -> None:
    """blocked-continue should print skip message when another runner claimed it."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.config = MagicMock()

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.core.use_cases.blocked_continue.blocked_continue_issue",
        return_value=False,
    ) as mock_blocked:
        exit_code = main(["blocked-continue", "--issue", "42"])

    assert exit_code == 0
    mock_blocked.assert_called_once()
    captured = capsys.readouterr()
    assert "Issue #42 was claimed by another runner" in captured.out


def test_main_blocked_continue_failure_prints_error(capsys) -> None:
    """blocked-continue should print a concise error on BlockedContinueError."""
    from backend.api.cli import main
    from backend.core.use_cases.blocked_continue import BlockedContinueError

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.config = MagicMock()

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.core.use_cases.blocked_continue.blocked_continue_issue",
        side_effect=BlockedContinueError("Worktree has uncommitted changes."),
    ):
        exit_code = main(["blocked-continue", "--issue", "42"])

    assert exit_code == 1
    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"
    assert "blocked-continue failed" in combined
    assert "Worktree has uncommitted changes" in combined
