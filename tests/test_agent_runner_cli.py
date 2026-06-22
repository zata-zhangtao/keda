"""Tests for the CLI argument parsing and dispatch logic."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from backend.api.cli import _expand_prd_paths, main
from backend.api.cli_parser import build_parser
from backend.core.shared.interfaces.runner_console import RunnerProcessKind
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


def test_cli_parser_issue_create_defaults() -> None:
    """issue create should have sensible defaults."""
    parser = build_parser()
    parsed = parser.parse_args(["issue", "create", "tasks/example.md"])
    assert parsed.command == "issue create"
    assert parsed.issue_command == "create"
    assert parsed.prd_paths == ["tasks/example.md"]
    assert parsed.type == "feature"
    assert parsed.ready is False
    assert parsed.agent == "auto"
    assert parsed.publish_prd is True
    assert parsed.force is False


def test_cli_parser_issue_create_multiple_paths() -> None:
    """issue create should accept multiple PRD paths."""
    parser = build_parser()
    parsed = parser.parse_args(
        ["issue", "create", "tasks/a.md", "tasks/b.md", "--ready"]
    )
    assert parsed.command == "issue create"
    assert parsed.issue_command == "create"
    assert parsed.prd_paths == ["tasks/a.md", "tasks/b.md"]
    assert parsed.ready is True


def test_cli_parser_issue_create_publish_prd() -> None:
    """issue create should expose explicit PRD publishing."""
    parser = build_parser()
    parsed = parser.parse_args(
        ["issue", "create", "tasks/example.md", "--publish-prd", "--no-ready"]
    )
    assert parsed.command == "issue create"
    assert parsed.issue_command == "create"
    assert parsed.publish_prd is True
    assert parsed.ready is False


def test_cli_parser_issue_create_no_publish_prd() -> None:
    """issue create should allow opting out of the default PRD publishing."""
    parser = build_parser()
    parsed = parser.parse_args(
        ["issue", "create", "tasks/example.md", "--no-publish-prd"]
    )
    assert parsed.command == "issue create"
    assert parsed.issue_command == "create"
    assert parsed.publish_prd is False


def test_cli_parser_issue_create_dependency_options() -> None:
    """issue create should accept dependency gate options."""
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "issue",
            "create",
            "tasks/example.md",
            "--depends-on",
            "42",
            "--depends-on",
            "43",
            "--depends-on-group",
            "upstream-a",
        ]
    )
    assert parsed.depends_on == [42, 43]
    assert parsed.depends_on_group == ["upstream-a"]


def test_cli_parser_issue_create_accepts_directory() -> None:
    """issue create should accept a directory as a PRD path argument."""
    parser = build_parser()
    parsed = parser.parse_args(["issue", "create", "tasks/pending"])
    assert parsed.command == "issue create"
    assert parsed.issue_command == "create"
    assert parsed.prd_paths == ["tasks/pending"]


def test_expand_prd_paths_directory(tmp_path: Path) -> None:
    """_expand_prd_paths should expand a directory to its *.md files sorted."""
    (tmp_path / "tasks" / "pending").mkdir(parents=True)
    (tmp_path / "tasks" / "pending" / "b.md").write_text("# B", encoding="utf-8")
    (tmp_path / "tasks" / "pending" / "a.md").write_text("# A", encoding="utf-8")
    (tmp_path / "tasks" / "pending" / ".gitkeep").write_text("", encoding="utf-8")

    expanded = _expand_prd_paths(tmp_path, ["tasks/pending"])

    assert expanded == ["tasks/pending/a.md", "tasks/pending/b.md"]


def test_expand_prd_paths_mixed_file_and_directory(tmp_path: Path) -> None:
    """_expand_prd_paths should preserve input order when mixing files and dirs."""
    (tmp_path / "tasks" / "pending").mkdir(parents=True)
    (tmp_path / "tasks" / "pending" / "a.md").write_text("# A", encoding="utf-8")
    (tmp_path / "tasks" / "root.md").write_text("# Root", encoding="utf-8")

    expanded = _expand_prd_paths(tmp_path, ["tasks/root.md", "tasks/pending"])

    assert expanded == ["tasks/root.md", "tasks/pending/a.md"]


def test_expand_prd_paths_deduplicates(tmp_path: Path) -> None:
    """_expand_prd_paths should deduplicate repeated files."""
    (tmp_path / "tasks" / "pending").mkdir(parents=True)
    (tmp_path / "tasks" / "pending" / "a.md").write_text("# A", encoding="utf-8")

    expanded = _expand_prd_paths(tmp_path, ["tasks/pending", "tasks/pending/a.md"])

    assert expanded == ["tasks/pending/a.md"]


def test_expand_prd_paths_empty_directory_rejected(tmp_path: Path) -> None:
    """_expand_prd_paths should reject a directory with no *.md files."""
    (tmp_path / "tasks" / "pending").mkdir(parents=True)

    with pytest.raises(ValueError, match="contains no PRD Markdown files"):
        _expand_prd_paths(tmp_path, ["tasks/pending"])


def test_expand_prd_paths_missing_path_passed_through(tmp_path: Path) -> None:
    """_expand_prd_paths should pass through non-existent paths unchanged."""
    expanded = _expand_prd_paths(tmp_path, ["tasks/missing.md"])

    assert expanded == ["tasks/missing.md"]


def test_expand_prd_paths_non_md_file_rejected(tmp_path: Path) -> None:
    """_expand_prd_paths should reject an existing non-Markdown file."""
    (tmp_path / "tasks").mkdir(parents=True)
    (tmp_path / "tasks" / "readme.txt").write_text("txt", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a Markdown file"):
        _expand_prd_paths(tmp_path, ["tasks/readme.txt"])


def test_cli_parser_run() -> None:
    """run should accept dry-run and agent flags."""
    parser = build_parser()
    parsed = parser.parse_args(
        ["run", "--dry-run", "--agent", "claude", "--max-issues", "5"]
    )
    assert parsed.command == "run"
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


def test_cli_parser_registry_scan() -> None:
    """registry scan should accept an optional scan root."""
    parser = build_parser()
    parsed = parser.parse_args(["registry", "scan", "/Users/me/code"])
    assert parsed.command == "registry"
    assert parsed.registry_command == "scan"
    assert parsed.scan_root == "/Users/me/code"


def test_cli_parser_registry_sync_defaults() -> None:
    """registry sync should default to current directory and not dry-run."""
    parser = build_parser()
    parsed = parser.parse_args(["registry", "sync"])
    assert parsed.command == "registry"
    assert parsed.registry_command == "sync"
    assert parsed.scan_root == "."
    assert parsed.dry_run is False


def test_cli_parser_registry_sync_dry_run() -> None:
    """registry sync should accept --dry-run."""
    parser = build_parser()
    parsed = parser.parse_args(["registry", "sync", "--dry-run", "/tmp"])
    assert parsed.command == "registry"
    assert parsed.registry_command == "sync"
    assert parsed.dry_run is True
    assert parsed.scan_root == "/tmp"


def test_cli_parser_registry_reinit() -> None:
    """registry reinit should accept repo-id, remote, base-branch, and start-daemons."""
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "registry",
            "reinit",
            "--repo-id",
            "zata-zhangtao-fsense",
            "--remote",
            "upstream",
            "--base-branch",
            "develop",
            "--start-daemons",
        ]
    )
    assert parsed.command == "registry"
    assert parsed.registry_command == "reinit"
    assert parsed.repo_id == "zata-zhangtao-fsense"
    assert parsed.remote == "upstream"
    assert parsed.base_branch == "develop"
    assert parsed.start_daemons is True


def test_cli_parser_registry_reinit_defaults() -> None:
    """registry reinit should default remote to origin and not start daemons."""
    parser = build_parser()
    parsed = parser.parse_args(["registry", "reinit", "--repo-id", "foo-bar"])
    assert parsed.remote == "origin"
    assert parsed.base_branch is None
    assert parsed.start_daemons is False


def test_cli_parser_registry_remove() -> None:
    """registry remove should accept repo-id and optional --delete."""
    parser = build_parser()
    parsed = parser.parse_args(
        ["registry", "remove", "--repo-id", "zata-zhangtao-fsense", "--delete"]
    )
    assert parsed.command == "registry"
    assert parsed.registry_command == "remove"
    assert parsed.repo_id == "zata-zhangtao-fsense"
    assert parsed.delete is True


def test_cli_parser_daemon() -> None:
    """daemon should accept interval and max-issues."""
    parser = build_parser()
    parsed = parser.parse_args(["daemon", "--interval", "300", "--max-issues", "2"])
    assert parsed.command == "daemon"
    assert parsed.interval == 300
    assert parsed.max_issues == 2


def test_cli_parser_daemon_default_interval_is_none() -> None:
    """daemon --interval should default to None so config supplies the value."""
    parser = build_parser()
    parsed = parser.parse_args(["daemon"])
    assert parsed.interval is None


def test_cli_parser_review_daemon_default_interval_is_none() -> None:
    """review-daemon --interval should default to None so config supplies the value."""
    parser = build_parser()
    parsed = parser.parse_args(["review-daemon"])
    assert parsed.interval is None


def test_main_daemon_default_interval_uses_config(monkeypatch) -> None:
    """Typer daemon without --interval should use the configured 120s default."""
    from backend.api.cli import main

    monkeypatch.setenv("IAR_SKIP_GH_AUTH_CHECK", "1")

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.repo_id = "repo"
    mock_context.display_name = "Repo"

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.run_agent_daemon") as mock_daemon, patch(
        "backend.api.cli.require_iar_repository_initialized"
    ):
        exit_code = main(["daemon", "--all"])

    assert exit_code == 0
    assert mock_daemon.call_args.kwargs["interval"] == 120


def test_main_review_daemon_default_interval_uses_config(monkeypatch) -> None:
    """Typer review-daemon without --interval should use the configured 120s default."""
    from backend.api.cli import main

    monkeypatch.setenv("IAR_SKIP_GH_AUTH_CHECK", "1")

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.repo_id = "repo"
    mock_context.display_name = "Repo"

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.run_review_daemon") as mock_daemon, patch(
        "backend.api.cli.require_iar_repository_initialized"
    ):
        exit_code = main(["review-daemon", "--all"])

    assert exit_code == 0
    assert mock_daemon.call_args.kwargs["interval"] == 120


def test_main_daemon_interval_override(monkeypatch) -> None:
    """Typer daemon --interval should override the config default."""
    from backend.api.cli import main

    monkeypatch.setenv("IAR_SKIP_GH_AUTH_CHECK", "1")

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.repo_id = "repo"
    mock_context.display_name = "Repo"

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.run_agent_daemon") as mock_daemon, patch(
        "backend.api.cli.require_iar_repository_initialized"
    ):
        exit_code = main(["daemon", "--all", "--interval", "300"])

    assert exit_code == 0
    assert mock_daemon.call_args.kwargs["interval"] == 300


def test_cli_parser_repo_id() -> None:
    """--repo-id should be accepted on subcommands."""
    parser = build_parser()
    parsed = parser.parse_args(["run", "--repo-id", "keda"])
    assert parsed.repo_id == "keda"


def test_cli_parser_all_repositories() -> None:
    """--all should be accepted by multi-target commands."""
    parser = build_parser()
    parsed = parser.parse_args(["run", "--all"])
    assert parsed.all_repositories is True


def test_cli_parser_repo_and_repo_id_individually_parseable() -> None:
    """--repo and --repo-id should each be parseable individually."""
    parser = build_parser()
    parsed_repo = parser.parse_args(["run", "--repo", "/tmp/repo"])
    assert parsed_repo.repo == "/tmp/repo"
    assert parsed_repo.repo_id is None

    parsed_id = parser.parse_args(["run", "--repo-id", "keda"])
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


def test_main_top_level_help_alias_h(capsys) -> None:
    """Top-level -h should behave like --help."""
    from backend.api.cli import main

    exit_code = main(["-h"])
    captured = capsys.readouterr()
    combined_output = _strip_ansi(f"{captured.out}\n{captured.err}")

    assert exit_code == 0
    assert "Usage: iar" in combined_output
    assert "Commands" in combined_output


def test_main_worktree_help_alias_h(capsys) -> None:
    """Subcommand group worktree -h should behave like --help."""
    from backend.api.cli import main

    exit_code = main(["worktree", "-h"])
    captured = capsys.readouterr()
    combined_output = _strip_ansi(f"{captured.out}\n{captured.err}")

    assert exit_code == 0
    assert "Usage: iar worktree" in combined_output
    assert "create" in combined_output


def test_main_worktree_create_help_alias_h(capsys) -> None:
    """Leaf subcommand worktree create -h should behave like --help."""
    from backend.api.cli import main

    exit_code = main(["worktree", "create", "-h"])
    captured = capsys.readouterr()
    combined_output = _strip_ansi(f"{captured.out}\n{captured.err}")

    assert exit_code == 0
    assert "Usage: iar worktree create" in combined_output
    assert "--branch" in combined_output


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


def test_main_rejects_repo_and_repo_id_together() -> None:
    """main should exit 1 when both --repo and --repo-id are given."""
    from backend.api.cli import main

    exit_code = main(["run", "--repo", "/tmp/repo", "--repo-id", "keda"])
    assert exit_code == 1


def test_main_rejects_unknown_repo_id() -> None:
    """main should exit 1 when repo-id does not exist in config."""
    from backend.api.cli import main

    with patch(
        "backend.api.cli.resolve_repository_targets",
        side_effect=ValueError("not found"),
    ):
        exit_code = main(["run", "--repo-id", "nonexistent"])
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
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.api.cli.require_iar_repository_initialized"
    ):
        exit_code = main(["run", "--all", "--dry-run"])

    assert exit_code == 0
    assert mock_resolve.call_args.kwargs["all_repositories"] is True


def test_main_run_passes_all_repositories_selector() -> None:
    """run command should dispatch to run_agent_repositories_once with same selectors."""
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
    ) as mock_run, patch("backend.api.cli.create_github_client"), patch(
        "backend.api.cli.require_iar_repository_initialized"
    ):
        exit_code = main(["run", "--all", "--dry-run", "--agent", "codex"])

    assert exit_code == 0
    assert mock_resolve.call_args.kwargs["all_repositories"] is True
    assert mock_run.call_args.kwargs["dry_run"] is True
    assert mock_run.call_args.kwargs["agent"] == "codex"


def test_main_daemon_defaults_to_all_repositories(monkeypatch) -> None:
    """daemon without selectors should default to all configured repositories."""
    from backend.api.cli import main

    monkeypatch.setenv("IAR_SKIP_GH_AUTH_CHECK", "1")

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.repo_id = "repo"
    mock_context.display_name = "Repo"

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ) as mock_resolve, patch("backend.api.cli.run_agent_daemon"), patch(
        "backend.api.cli.require_iar_repository_initialized"
    ):
        exit_code = main(["daemon"])

    assert exit_code == 0
    assert mock_resolve.call_args.kwargs["all_repositories"] is True


def test_main_review_daemon_defaults_to_all_repositories(monkeypatch) -> None:
    """review-daemon without selectors should default to all configured repositories."""
    from backend.api.cli import main

    monkeypatch.setenv("IAR_SKIP_GH_AUTH_CHECK", "1")

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.repo_id = "repo"
    mock_context.display_name = "Repo"

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ) as mock_resolve, patch("backend.api.cli.run_review_daemon"), patch(
        "backend.api.cli.require_iar_repository_initialized"
    ):
        exit_code = main(["review-daemon"])

    assert exit_code == 0
    assert mock_resolve.call_args.kwargs["all_repositories"] is True


def test_main_daemon_with_repo_id_does_not_default_to_all(monkeypatch) -> None:
    """daemon with --repo-id should still target only the specified repository."""
    from backend.api.cli import main

    monkeypatch.setenv("IAR_SKIP_GH_AUTH_CHECK", "1")

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.repo_id = "keda"
    mock_context.display_name = "Keda"

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ) as mock_resolve, patch("backend.api.cli.run_agent_daemon"), patch(
        "backend.api.cli.require_iar_repository_initialized"
    ):
        exit_code = main(["daemon", "--repo-id", "keda"])

    assert exit_code == 0
    assert mock_resolve.call_args.kwargs["repo_id"] == "keda"
    assert mock_resolve.call_args.kwargs["all_repositories"] is False


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
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.api.cli.require_iar_repository_initialized"
    ):
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
    ), patch("backend.api.cli.require_iar_repository_initialized"):
        exit_code = main(["labels", "sync"])
        assert exit_code == 0
        assert mock_sync.call_count == 2


def test_main_issue_create_defaults_to_cwd() -> None:
    """issue create without --repo or --repo-id should resolve to cwd."""
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
        exit_code = main(["issue", "create", "tasks/example.md"])
        assert exit_code == 0


def test_main_issue_create_uses_prd_issue_workflow() -> None:
    """issue create should use the existing PRD issue workflow."""
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
        exit_code = main(["issue", "create", "tasks/example.md", "--no-publish-prd"])

    captured = capsys.readouterr()
    combined_output = f"{captured.out}\n{captured.err}"

    assert exit_code == 1
    assert "Failed to create Issue from tasks/example.md:" in combined_output
    assert (
        "Command: git commit -m 'docs(prd): publish example' -- tasks/example.md"
        in (combined_output)
    )
    assert "Exit code: 1" in combined_output
    assert "stdout:" in combined_output
    assert "pre-commit stdout" in combined_output
    assert "stderr:" in combined_output
    assert "trailing whitespace" in combined_output


def test_main_issue_create_ready_without_publish_defers_label() -> None:
    """--ready without --publish-prd should not ready the Issue until PRD is pushed.

    时序说明：
    ┌─────────────────────────────────────────────────────────┐
    │ cli.py                                                 │
    │   queue_ready_for_request = False  # --no-publish-prd   │
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
        exit_code = main(
            ["issue", "create", "tasks/example.md", "--ready", "--no-publish-prd"]
        )
        assert exit_code == 0
        # create_issue_from_prd should be called with queue_ready=False
        assert mock_create.call_args.kwargs["request"].queue_ready is False
        # prompt should still receive queue_ready=True so it can add the label after push
        assert mock_prompt.call_args.kwargs["queue_ready"] is True


def test_main_issue_create_ready_with_publish_keeps_label() -> None:
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
            ["issue", "create", "tasks/example.md", "--publish-prd", "--ready"]
        )
        assert exit_code == 0
        assert mock_create.call_args.kwargs["request"].queue_ready is True
        # prompt should not be called when --publish-prd is used
        mock_prompt.assert_not_called()


def test_main_issue_create_multiple_prds() -> None:
    """issue create should create an Issue for each supplied PRD path."""
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
    ):
        exit_code = main(["issue", "create", "tasks/a.md", "tasks/b.md", "--ready"])

    assert exit_code == 0
    assert mock_create.call_count == 2
    created_prd_paths = [
        call.kwargs["request"].prd_path for call in mock_create.call_args_list
    ]
    assert created_prd_paths == [Path("tasks/a.md"), Path("tasks/b.md")]


def test_main_issue_create_multiple_prds_rejects_shared_title() -> None:
    """--title cannot be shared across multiple PRD-created Issues."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path.cwd()
    mock_context.config.labels = MagicMock()
    mock_context.config.git.remote = "origin"
    mock_context.config.git.base_branch = "main"

    with patch(
        "backend.api.cli.resolve_issue_from_prd_target", return_value=mock_context
    ), patch("backend.api.cli.create_github_client") as mock_client, patch(
        "backend.api.cli.create_issue_from_prd"
    ) as mock_create:
        exit_code = main(
            [
                "issue",
                "create",
                "tasks/a.md",
                "tasks/b.md",
                "--title",
                "Shared Title",
            ]
        )

    assert exit_code == 1
    mock_client.assert_not_called()
    mock_create.assert_not_called()


def test_main_issue_create_multiple_prds_continues_on_failure() -> None:
    """A failure for one PRD should not stop creation for the remaining PRDs."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path.cwd()
    mock_context.config.labels = MagicMock()
    mock_context.config.git.remote = "origin"
    mock_context.config.git.base_branch = "main"

    def _fake_create(*, request, **kwargs):
        if request.prd_path.name == "bad.md":
            raise ValueError("bad PRD")
        return "https://github.com/example/issues/1"

    with patch(
        "backend.api.cli.resolve_issue_from_prd_target", return_value=mock_context
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.api.cli.create_issue_from_prd", side_effect=_fake_create
    ) as mock_create, patch(
        "backend.api.cli._prompt_and_publish_prd_if_needed", return_value=False
    ):
        exit_code = main(
            ["issue", "create", "tasks/bad.md", "tasks/good.md", "--ready"]
        )

    assert exit_code == 1
    assert mock_create.call_count == 2
    created_prd_paths = [
        call.kwargs["request"].prd_path.name for call in mock_create.call_args_list
    ]
    assert created_prd_paths == ["bad.md", "good.md"]


def test_main_issue_create_directory_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue create should expand a directory argument to its *.md files."""
    from backend.api.cli import main

    pending_dir = tmp_path / "tasks" / "pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / "b.md").write_text("# B", encoding="utf-8")
    (pending_dir / "a.md").write_text("# A", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    mock_context = MagicMock()
    mock_context.repo_path = tmp_path
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
    ), patch("backend.api.cli.require_iar_repository_initialized"):
        exit_code = main(["issue", "create", "tasks/pending", "--ready"])

    assert exit_code == 0
    assert mock_create.call_count == 2
    created_prd_paths = [
        call.kwargs["request"].prd_path for call in mock_create.call_args_list
    ]
    assert created_prd_paths == [
        Path("tasks/pending/a.md"),
        Path("tasks/pending/b.md"),
    ]


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
        DeliberationConfig,
        DeliberationResult,
    )
    from backend.core.shared.models.agent_runner import AppConfig

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

    mock_config = AppConfig(
        deliberation=DeliberationConfig(
            default_output_dir=str(output_root),
            default_rounds=2,
            default_synthesizer="claude",
            profiles=(
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
            ),
        )
    )
    mock_context = MagicMock()
    mock_context.repo_path = tmp_path / "repo"
    mock_context.config = mock_config

    with patch("backend.api.cli.create_process_runner"), patch(
        "backend.api.cli.get_agent_runner_settings"
    ), patch(
        "backend.engines.agent_runner.factory.build_app_config_from_settings",
        return_value=mock_config,
    ), patch(
        "backend.api.cli._resolve_cli_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.create_transcript_runner"), patch(
        "backend.api.cli.create_event_sink"
    ) as mock_event_sink, patch(
        "backend.api.cli.run_agent_deliberation",
        side_effect=fake_run_agent_deliberation,
    ), patch("backend.api.cli.write_deliberation_outputs") as mock_write, patch(
        "backend.api.cli.require_iar_repository_initialized"
    ):
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


def test_main_review_dispatches_review_workflow() -> None:
    """review should dispatch to the review workflow."""
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
    ) as mock_review, patch("backend.api.cli.require_iar_repository_initialized"):
        exit_code = main(["review", "--dry-run", "--agent", "claude"])

    assert exit_code == 0
    mock_client.assert_called_with(mock_context.repo_path, ANY)
    assert mock_review.call_args.kwargs["dry_run"] is True
    assert mock_review.call_args.kwargs["agent"] == "claude"


def test_cli_parser_recover_required_args() -> None:
    """recover should require --issue."""
    parser = build_parser()
    parsed = parser.parse_args(["recover", "--issue", "5"])
    assert parsed.command == "recover"
    assert parsed.issue == 5
    assert parsed.branch is None


def test_cli_parser_recover_with_branch() -> None:
    """recover should accept optional --branch."""
    parser = build_parser()
    parsed = parser.parse_args(["recover", "--issue", "5", "--branch", "feature-xyz"])
    assert parsed.command == "recover"
    assert parsed.issue == 5
    assert parsed.branch == "feature-xyz"


def test_cli_parser_recover_missing_issue() -> None:
    """recover should fail without --issue."""
    import pytest as _pytest

    parser = build_parser()
    with _pytest.raises(SystemExit):
        parser.parse_args(["recover"])


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
    ) as mock_blocked, patch("backend.api.cli.require_iar_repository_initialized"):
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
    ) as mock_blocked, patch("backend.api.cli.require_iar_repository_initialized"):
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
    ), patch("backend.api.cli.require_iar_repository_initialized"):
        exit_code = main(["blocked-continue", "--issue", "42"])

    assert exit_code == 1
    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"
    assert "blocked-continue failed" in combined
    assert "Worktree has uncommitted changes" in combined


def test_main_run_rebase_conflict_detached_head() -> None:
    """iar run should dispatch without error for rework rebase with detached HEAD."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.config = MagicMock()
    mock_context.repo_id = "repo"

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.api.cli.run_agent_repositories_once", return_value=0
    ) as mock_run, patch("backend.api.cli.require_iar_repository_initialized"):
        exit_code = main(["run", "--dry-run", "--agent", "claude"])

    assert exit_code == 0
    mock_run.assert_called_once()


def test_cli_parser_ask_defaults() -> None:
    """ask should have sensible defaults."""
    parser = build_parser()
    parsed = parser.parse_args(["ask", "what should I do"])
    assert parsed.command == "ask"
    assert parsed.prompt == "what should I do"
    assert parsed.agent == "auto"
    assert parsed.plan_only is False
    assert parsed.execute is False
    assert parsed.yes is False
    assert parsed.output is None


def test_cli_parser_ask_with_options() -> None:
    """ask should accept all defined options."""
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "ask",
            "create issue from prd",
            "--agent",
            "codex",
            "--plan-only",
            "--execute",
            "--yes",
            "--output",
            "/tmp/out",
            "--repo",
            "/tmp/repo",
        ]
    )
    assert parsed.command == "ask"
    assert parsed.prompt == "create issue from prd"
    assert parsed.agent == "codex"
    assert parsed.plan_only is True
    assert parsed.execute is True
    assert parsed.yes is True
    assert parsed.output == "/tmp/out"
    assert parsed.repo == "/tmp/repo"


def test_main_ask_plan_only_writes_audit(tmp_path, monkeypatch) -> None:
    """ask --plan-only should write audit files without executing."""
    from backend.api.cli import main

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    (repo_path / "tasks").mkdir()
    (repo_path / "tasks" / "pending").mkdir()

    fake_planner_stdout = (
        '{"decision_id": "dec-test-123", '
        '"user_prompt": "test", '
        '"intent_summary": "Do nothing", '
        '"risk_level": "low", '
        '"actions": [{'
        '"action_id": "A1", '
        '"action_type": "no_op", '
        '"title": "No action", '
        '"rationale": "Nothing to do", '
        '"parameters": {}, '
        '"writes_external_state": false, '
        '"confirmation_required": false'
        "}], "
        '"assumptions": [], '
        '"warnings": [], '
        '"requires_confirmation": false}'
    )

    mock_context = MagicMock()
    mock_context.repo_path = repo_path
    mock_context.config = MagicMock()
    mock_context.config.interactive_decision.default_agent = "codex"
    mock_context.config.interactive_decision.default_output_dir = str(
        tmp_path / "decisions"
    )
    mock_context.config.labels = MagicMock()
    mock_context.config.git.remote = "origin"
    mock_context.config.git.base_branch = "main"
    mock_context.config.prompts.default_phase = "execution"

    mock_planner = MagicMock()
    mock_planner.generate.return_value = MagicMock(
        return_code=0,
        stdout=fake_planner_stdout,
    )

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.api.cli.create_planner_runner",
        return_value=mock_planner,
    ), patch("backend.api.cli._ensure_gh_auth_or_prompt"), patch(
        "backend.api.cli.require_iar_repository_initialized"
    ):
        exit_code = main(
            [
                "ask",
                "what should I do",
                "--plan-only",
                "--repo",
                str(repo_path),
                "--output",
                str(tmp_path / "decisions"),
            ]
        )

    assert exit_code == 0
    decision_dir = tmp_path / "decisions" / "dec-test-123"
    assert (decision_dir / "plan.json").exists()
    assert (decision_dir / "plan.md").exists()
    assert (decision_dir / "context-summary.json").exists()


def test_main_ask_rejects_unknown_action() -> None:
    """ask should return non-zero when planner returns unknown action."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.config = MagicMock()
    mock_context.config.interactive_decision.default_agent = "codex"
    mock_context.config.interactive_decision.default_output_dir = "logs/decisions"
    mock_context.config.labels = MagicMock()

    fake_planner_stdout = (
        '{"decision_id": "dec-test-456", '
        '"user_prompt": "test", '
        '"intent_summary": "Bad action", '
        '"risk_level": "low", '
        '"actions": [{'
        '"action_id": "A1", '
        '"action_type": "git_push", '
        '"title": "Push", '
        '"rationale": "Bad", '
        '"parameters": {}, '
        '"writes_external_state": true, '
        '"confirmation_required": true'
        "}], "
        '"assumptions": [], '
        '"warnings": [], '
        '"requires_confirmation": false}'
    )

    mock_planner = MagicMock()
    mock_planner.generate.return_value = MagicMock(
        return_code=0,
        stdout=fake_planner_stdout,
    )

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.api.cli.create_planner_runner",
        return_value=mock_planner,
    ), patch("backend.api.cli._ensure_gh_auth_or_prompt"):
        exit_code = main(["ask", "push to main", "--plan-only", "--repo", "/tmp/repo"])

    assert exit_code == 1


def test_main_ask_run_once_dry_run_dispatches_existing_use_case() -> None:
    """ask --execute for run_once_dry_run should dispatch run_agent_repositories_once."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.repo_id = "repo"
    mock_context.display_name = "Repo"
    mock_context.config = MagicMock()
    mock_context.config.interactive_decision.default_agent = "codex"
    mock_context.config.interactive_decision.default_output_dir = "logs/decisions"
    mock_context.config.labels = MagicMock()
    mock_context.config.prompts.default_phase = "execution"

    fake_planner_stdout = (
        '{"decision_id": "dec-test-789", '
        '"user_prompt": "test", '
        '"intent_summary": "Dry run", '
        '"risk_level": "low", '
        '"actions": [{'
        '"action_id": "A1", '
        '"action_type": "run_once_dry_run", '
        '"title": "Dry run", '
        '"rationale": "Preview", '
        '"parameters": {"agent": "auto", "max_issues": 1}, '
        '"writes_external_state": false, '
        '"confirmation_required": false'
        "}], "
        '"assumptions": [], '
        '"warnings": [], '
        '"requires_confirmation": false}'
    )

    mock_planner = MagicMock()
    mock_planner.generate.return_value = MagicMock(
        return_code=0,
        stdout=fake_planner_stdout,
    )

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.api.cli.create_planner_runner",
        return_value=mock_planner,
    ), patch(
        "backend.core.use_cases.interactive_decision.run_agent_repositories_once",
        return_value=0,
    ) as mock_run, patch("backend.api.cli._ensure_gh_auth_or_prompt"), patch(
        "backend.api.cli.require_iar_repository_initialized"
    ):
        exit_code = main(
            [
                "ask",
                "dry run",
                "--execute",
                "--yes",
                "--repo",
                "/tmp/repo",
            ]
        )

    assert exit_code == 0
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["dry_run"] is True


def test_main_ask_execute_confirmation_required_for_write_action() -> None:
    """ask --execute for create_issue_from_prd requires confirmation and fails without TTY."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.repo_id = "repo"
    mock_context.display_name = "Repo"
    mock_context.config = MagicMock()
    mock_context.config.interactive_decision.default_agent = "codex"
    mock_context.config.interactive_decision.default_output_dir = "logs/decisions"
    mock_context.config.labels = MagicMock()
    mock_context.config.prompts.default_phase = "execution"

    fake_planner_stdout = (
        '{"decision_id": "dec-test-abc", '
        '"user_prompt": "test", '
        '"intent_summary": "Create issue", '
        '"risk_level": "medium", '
        '"actions": [{'
        '"action_id": "A1", '
        '"action_type": "create_issue_from_prd", '
        '"title": "Create issue", '
        '"rationale": "PRD is ready", '
        '"parameters": {"prd_path": "tasks/pending/example.md", "ready": false}, '
        '"writes_external_state": true, '
        '"confirmation_required": true'
        '}], "assumptions": [], "warnings": ["This will create a GitHub Issue."], '
        '"requires_confirmation": true}'
    )

    mock_planner = MagicMock()
    mock_planner.generate.return_value = MagicMock(
        return_code=0,
        stdout=fake_planner_stdout,
    )

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.api.cli.create_planner_runner",
        return_value=mock_planner,
    ), patch("backend.api.cli._ensure_gh_auth_or_prompt"):
        exit_code = main(
            [
                "ask",
                "create issue from PRD",
                "--execute",
                "--repo",
                "/tmp/repo",
            ]
        )

    assert exit_code == 1


def test_main_ask_execute_confirmation_wrong_input_skips_action(monkeypatch) -> None:
    """ask --execute skips write action when user confirmation input does not match."""
    from backend.api.cli import main

    mock_context = MagicMock()
    mock_context.repo_path = Path("/tmp/repo")
    mock_context.repo_id = "repo"
    mock_context.display_name = "Repo"
    mock_context.config = MagicMock()
    mock_context.config.interactive_decision.default_agent = "codex"
    mock_context.config.interactive_decision.default_output_dir = "logs/decisions"
    mock_context.config.labels = MagicMock()
    mock_context.config.prompts.default_phase = "execution"

    fake_planner_stdout = (
        '{"decision_id": "dec-test-wrong", '
        '"user_prompt": "test", '
        '"intent_summary": "Create issue", '
        '"risk_level": "medium", '
        '"actions": [{'
        '"action_id": "A1", '
        '"action_type": "create_issue_from_prd", '
        '"title": "Create issue", '
        '"rationale": "PRD is ready", '
        '"parameters": {"prd_path": "tasks/pending/example.md", "ready": false}, '
        '"writes_external_state": true, '
        '"confirmation_required": true'
        '}], "assumptions": [], "warnings": [], "requires_confirmation": true}'
    )

    mock_planner = MagicMock()
    mock_planner.generate.return_value = MagicMock(
        return_code=0,
        stdout=fake_planner_stdout,
    )

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "wrong-confirmation")

    with patch(
        "backend.api.cli.resolve_repository_targets",
        return_value=[mock_context],
    ), patch("backend.api.cli.create_github_client"), patch(
        "backend.api.cli.create_planner_runner",
        return_value=mock_planner,
    ), patch(
        "backend.core.use_cases.interactive_decision.create_issue_from_prd"
    ) as mock_create, patch("backend.api.cli._ensure_gh_auth_or_prompt"):
        exit_code = main(
            [
                "ask",
                "create issue from PRD",
                "--execute",
                "--repo",
                "/tmp/repo",
            ]
        )

    assert exit_code == 1
    mock_create.assert_not_called()


def _init_bare_git_repository(tmp_path: Path, name: str) -> Path:
    """Create a throwaway Git repository without .iar.toml."""
    repo_path = tmp_path / name
    repo_path.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    (repo_path / "README.md").write_text("placeholder", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    return repo_path


def test_main_labels_sync_fails_when_repository_not_initialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar labels sync` should fail fast without .iar.toml."""
    repo_path = _init_bare_git_repository(tmp_path, "uninitialized")
    monkeypatch.chdir(repo_path)

    exit_code = main(["labels", "sync"])
    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"

    assert exit_code == 1
    assert "Repository is not initialized for iar" in _strip_ansi(combined)
    assert "iar init" in _strip_ansi(combined)
    assert ".iar.toml" in _strip_ansi(combined)


def test_main_run_fails_when_repository_not_initialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar run --dry-run` should fail fast without .iar.toml."""
    repo_path = _init_bare_git_repository(tmp_path, "uninitialized")
    monkeypatch.chdir(repo_path)

    exit_code = main(["run", "--dry-run"])
    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"

    assert exit_code == 1
    assert "Repository is not initialized for iar" in _strip_ansi(combined)
    assert "iar init" in _strip_ansi(combined)


def test_main_issue_create_fails_when_repository_not_initialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar issue create` should fail fast without .iar.toml."""
    repo_path = _init_bare_git_repository(tmp_path, "uninitialized")
    prd_path = repo_path / "tasks" / "pending" / "test.md"
    prd_path.parent.mkdir(parents=True)
    prd_path.write_text("# Test PRD\n\n## Summary\n\nTest.\n", encoding="utf-8")
    monkeypatch.chdir(repo_path)

    exit_code = main(["issue", "create", str(prd_path)])
    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"

    assert exit_code == 1
    assert "Repository is not initialized for iar" in _strip_ansi(combined)
    assert "iar init" in _strip_ansi(combined)


def test_main_worktree_create_fails_when_repository_not_initialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar worktree create` should fail fast without .iar.toml."""
    repo_path = _init_bare_git_repository(tmp_path, "uninitialized")
    monkeypatch.chdir(repo_path)

    exit_code = main(
        ["worktree", "create", "--branch", "feature-x", "--base-branch", "main"]
    )
    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"

    assert exit_code == 1
    assert "Repository is not initialized for iar" in _strip_ansi(combined)
    assert "iar init" in _strip_ansi(combined)


def test_main_init_succeeds_when_repository_not_initialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`iar init` must be exempt from the initialization gate."""
    repo_path = _init_bare_git_repository(tmp_path, "uninitialized")
    monkeypatch.chdir(repo_path)

    exit_code = main(["init", "--dry-run"])

    assert exit_code == 0


def _write_iar_toml(repo_root: Path, repo_id: str) -> None:
    """Write a minimal .iar.toml for CLI discovery tests."""
    iar_toml = repo_root / ".iar.toml"
    iar_toml.write_text(
        "[agent_runner]\n" "[agent_runner.repository]\n" f'id = "{repo_id}"\n',
        encoding="utf-8",
    )


def test_main_registry_scan_lists_discovered_repos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar registry scan` should print discovered repositories."""
    scan_root = tmp_path / "code"
    scan_root.mkdir()
    repo_path = scan_root / "foo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    _write_iar_toml(repo_path, "foo")

    exit_code = main(["registry", "scan", str(scan_root)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[foo]" in _strip_ansi(captured.out)
    assert "(new)" in _strip_ansi(captured.out)


def test_main_registry_sync_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar registry sync --dry-run` should not write config.toml."""
    monkeypatch.chdir(tmp_path)
    scan_root = tmp_path / "code"
    scan_root.mkdir()
    repo_path = scan_root / "bar"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    _write_iar_toml(repo_path, "bar")

    config_path = tmp_path / "config.toml"
    config_path.write_text("[agent_runner]\n", encoding="utf-8")

    exit_code = main(["registry", "sync", "--dry-run", str(scan_root)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would register" in _strip_ansi(captured.out)
    assert "[agent_runner.repositories.bar]" not in config_path.read_text(
        encoding="utf-8"
    )


def test_main_registry_sync_registers_new_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar registry sync` should write discovered repos to config.toml."""
    monkeypatch.chdir(tmp_path)
    scan_root = tmp_path / "code"
    scan_root.mkdir()
    repo_path = scan_root / "baz"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    _write_iar_toml(repo_path, "baz")

    config_path = tmp_path / "config.toml"
    config_path.write_text("[agent_runner]\n", encoding="utf-8")

    exit_code = main(["registry", "sync", str(scan_root)])

    assert exit_code == 0
    config_text = config_path.read_text(encoding="utf-8")
    assert "[agent_runner.repositories.baz]" in config_text


def test_main_registry_reinit_updates_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar registry reinit` should rewrite .iar.toml with the given remote."""
    monkeypatch.chdir(tmp_path)
    repo_path = _init_bare_git_repository(tmp_path, "fsense")
    _write_iar_toml(repo_path, "zata-zhangtao-fsense")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[agent_runner]\n[agent_runner.repositories.zata-zhangtao-fsense]\n"
        f'path = "{repo_path}"\nenabled = true\ndisplay_name = "fsense"\n',
        encoding="utf-8",
    )

    exit_code = main(["registry", "reinit", "--repo-id", "zata-zhangtao-fsense"])
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert "Reinitialized" in _strip_ansi(captured.out)
    config_text = config_path.read_text(encoding="utf-8")
    assert "[agent_runner.repositories.zata-zhangtao-fsense]" in config_text
    iar_toml_text = (repo_path / ".iar.toml").read_text(encoding="utf-8")
    assert 'remote = "origin"' in iar_toml_text


def test_main_registry_reinit_missing_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar registry reinit` should fail when repo_id is not in registry."""
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("[agent_runner]\n", encoding="utf-8")

    exit_code = main(["registry", "reinit", "--repo-id", "no-such-repo"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "not found in registry" in _strip_ansi(captured.out + captured.err)


def test_main_registry_reinit_start_daemons_uses_project_root_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar registry reinit --start-daemons` should spawn daemons from the keda project root so they read the same config.toml as the parent CLI."""
    monkeypatch.chdir(tmp_path)
    repo_path = _init_bare_git_repository(tmp_path, "fsense")
    _write_iar_toml(repo_path, "zata-zhangtao-fsense")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[agent_runner]\n[agent_runner.repositories.zata-zhangtao-fsense]\n"
        f'path = "{repo_path}"\nenabled = true\ndisplay_name = "fsense"\n',
        encoding="utf-8",
    )

    project_root_cwd = tmp_path / "project-root"
    project_root_cwd.mkdir()

    fake_context = MagicMock()
    fake_context.repo_id = "zata-zhangtao-fsense"

    fake_settings = MagicMock()
    fake_settings.console.runner_command = ["iar"]

    from backend.infrastructure.console.process_supervisor import RunnerProcessRecord

    def _fake_start(
        *,
        repo_id,
        kind,
        contexts,
        supervisor,
        runner_command,
        spawn_cwd,
        issue_number=None,
    ):
        return RunnerProcessRecord(
            process_id=f"fake-{kind.value}",
            repo_id=repo_id,
            kind=kind.value,
            pid=1,
            status="running",
            exit_code=None,
            log_path="/tmp/fake.log",
            command=tuple(runner_command) + (kind.value, "--repo-id", repo_id),
            started_at="2026-06-22T00:00:00+00:00",
            stopped_at=None,
        )

    with (
        patch(
            "backend.api.cli_registry.initialize_repository_local_config"
        ) as mock_init,
        patch(
            "backend.api.cli_registry.load_fresh_agent_runner_settings",
            return_value=fake_settings,
        ),
        patch(
            "backend.api.cli_registry.resolve_repository_targets_with_diagnostics",
            return_value=([fake_context], []),
        ),
        patch(
            "backend.api.cli_registry.create_process_supervisor"
        ) as mock_create_supervisor,
        patch(
            "backend.api.cli_registry.start_runner_process",
            side_effect=_fake_start,
        ) as mock_start,
        patch(
            "backend.api.cli_registry.resolve_console_spawn_cwd",
            return_value=project_root_cwd,
        ) as mock_spawn_cwd,
    ):
        mock_supervisor = MagicMock()
        mock_supervisor.list_processes.return_value = []
        mock_create_supervisor.return_value = mock_supervisor

        exit_code = main(
            [
                "registry",
                "reinit",
                "--repo-id",
                "zata-zhangtao-fsense",
                "--start-daemons",
            ]
        )
        captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert "Reinitialized" in _strip_ansi(captured.out)
    assert "Started daemon" in _strip_ansi(captured.out)
    assert "Started review_daemon" in _strip_ansi(captured.out)

    # The local config initializer was invoked as part of reinit.
    mock_init.assert_called_once()

    # spawn_cwd must come from resolve_console_spawn_cwd(), not the repository path.
    mock_spawn_cwd.assert_called_once()
    assert mock_start.call_count == 2
    for call in mock_start.call_args_list:
        assert call.kwargs["spawn_cwd"] == project_root_cwd
        assert isinstance(call.kwargs["kind"], RunnerProcessKind)


def test_main_registry_remove_deletes_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar registry remove` should remove the registry entry but keep files."""
    monkeypatch.chdir(tmp_path)
    repo_path = _init_bare_git_repository(tmp_path, "fsense")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[agent_runner]\n[agent_runner.repositories.zata-zhangtao-fsense]\n"
        f'path = "{repo_path}"\nenabled = true\ndisplay_name = "fsense"\n',
        encoding="utf-8",
    )

    exit_code = main(["registry", "remove", "--repo-id", "zata-zhangtao-fsense"])
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert "Removed" in _strip_ansi(captured.out)
    config_text = config_path.read_text(encoding="utf-8")
    assert "[agent_runner.repositories.zata-zhangtao-fsense]" not in config_text
    assert repo_path.exists()


def test_main_registry_remove_delete_removes_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar registry remove --delete` should remove the entry and the clone."""
    monkeypatch.chdir(tmp_path)
    repo_path = _init_bare_git_repository(tmp_path, "fsense")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[agent_runner]\n[agent_runner.repositories.zata-zhangtao-fsense]\n"
        f'path = "{repo_path}"\nenabled = true\ndisplay_name = "fsense"\n',
        encoding="utf-8",
    )

    exit_code = main(
        ["registry", "remove", "--repo-id", "zata-zhangtao-fsense", "--delete"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert "Deleted" in _strip_ansi(captured.out)
    config_text = config_path.read_text(encoding="utf-8")
    assert "[agent_runner.repositories.zata-zhangtao-fsense]" not in config_text
    assert not repo_path.exists()


def test_cli_parser_registry_list() -> None:
    """registry list should be recognized as a subcommand."""
    parser = build_parser()
    parsed = parser.parse_args(["registry", "list"])
    assert parsed.command == "registry"
    assert parsed.registry_command == "list"


def test_main_registry_list_shows_repositories_and_daemon_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar registry list` should print registered repos and daemon status."""
    monkeypatch.chdir(tmp_path)
    repo_a = _init_bare_git_repository(tmp_path, "repo-a")
    repo_b = _init_bare_git_repository(tmp_path, "repo-b")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[agent_runner]\n"
        "[agent_runner.repositories.repo-a]\n"
        f'path = "{repo_a}"\nenabled = true\ndisplay_name = "Repo A"\n'
        "[agent_runner.repositories.repo-b]\n"
        f'path = "{repo_b}"\nenabled = true\ndisplay_name = "Repo B"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("IAR_CONFIG", str(config_path))

    from backend.infrastructure.console.process_supervisor import RunnerProcessRecord

    records = [
        RunnerProcessRecord(
            process_id="daemon-1",
            repo_id="repo-a",
            kind="daemon",
            pid=123,
            status="running",
            exit_code=None,
            log_path="/tmp/d1.log",
            command=("iar", "daemon"),
            started_at="2026-06-22T00:00:00+00:00",
            stopped_at=None,
        ),
        RunnerProcessRecord(
            process_id="review-1",
            repo_id="repo-a",
            kind="review_daemon",
            pid=124,
            status="running",
            exit_code=None,
            log_path="/tmp/r1.log",
            command=("iar", "review-daemon"),
            started_at="2026-06-22T00:00:00+00:00",
            stopped_at=None,
        ),
    ]

    mock_supervisor = MagicMock()
    mock_supervisor.list_processes.return_value = records

    with patch(
        "backend.api.cli_registry.create_process_supervisor",
        return_value=mock_supervisor,
    ):
        exit_code = main(["registry", "list"])

    captured = capsys.readouterr()
    output = _strip_ansi(captured.out)

    assert exit_code == 0, captured.err
    assert "repo-a" in output
    assert "Repo A" in output
    assert repo_a.name in output
    assert "repo-b" in output
    assert "Repo B" in output
    assert repo_b.name in output
    assert "running" in output
    assert "daemon-1" in output
    assert "review-1" in output
    assert "stopped" in output


def test_main_registry_list_empty_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar registry list` should succeed with an empty registry."""
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("[agent_runner]\n", encoding="utf-8")
    monkeypatch.setenv("IAR_CONFIG", str(config_path))

    mock_supervisor = MagicMock()
    mock_supervisor.list_processes.return_value = []

    with patch(
        "backend.api.cli_registry.create_process_supervisor",
        return_value=mock_supervisor,
    ):
        exit_code = main(["registry", "list"])

    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    assert "Registered repositories" in _strip_ansi(captured.out)


def test_cli_parser_workflow_install() -> None:
    """workflow install should expose name + force + dry-run + common flags."""
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "workflow",
            "install",
            "preview",
            "--force",
            "--dry-run",
            "--repo",
            "/tmp/skip",
        ]
    )
    assert parsed.command == "workflow install"
    assert parsed.workflow_command == "install"
    assert parsed.name == "preview"
    assert parsed.force is True
    assert parsed.dry_run is True
    assert parsed.repo == "/tmp/skip"


def test_cli_parser_workflow_install_minimal() -> None:
    """workflow install without flags should default force/dry-run to False."""
    parser = build_parser()
    parsed = parser.parse_args(["workflow", "install", "preview"])
    assert parsed.command == "workflow install"
    assert parsed.name == "preview"
    assert parsed.force is False
    assert parsed.dry_run is False
    assert getattr(parsed, "repo", None) is None


def test_main_workflow_install_unknown_name_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unknown workflow names must exit non-zero without writing files."""
    monkeypatch.chdir(tmp_path)
    _write_iar_toml(tmp_path, "demo")
    (tmp_path / "config.toml").write_text("", encoding="utf-8")

    with patch(
        "backend.api.cli.detect_git_repository_root", return_value=tmp_path
    ), patch("backend.api.cli.require_iar_repository_initialized"):
        exit_code = main(["workflow", "install", "missing"])

    assert exit_code == 1
    assert not (tmp_path / "deploy").exists()


def test_main_workflow_install_rejects_global_repo_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receiving --repo must reject and not write any template file."""
    monkeypatch.chdir(tmp_path)
    _write_iar_toml(tmp_path, "demo")
    (tmp_path / "config.toml").write_text("", encoding="utf-8")

    exit_code = main(["workflow", "install", "preview", "--repo", str(tmp_path)])

    assert exit_code == 1
    assert not (tmp_path / "deploy").exists()
    assert not (tmp_path / "scripts").exists()


def test_main_workflow_install_rejects_global_config_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receiving --config must reject and not write any template file."""
    monkeypatch.chdir(tmp_path)
    _write_iar_toml(tmp_path, "demo")
    (tmp_path / "config.toml").write_text("", encoding="utf-8")

    exit_code = main(["workflow", "install", "preview", "--config", "/tmp/cfg"])

    assert exit_code == 1
    assert not (tmp_path / "deploy").exists()


def test_main_workflow_install_rejects_global_repo_id_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receiving --repo-id must reject and not write any template file."""
    monkeypatch.chdir(tmp_path)
    _write_iar_toml(tmp_path, "demo")
    (tmp_path / "config.toml").write_text("", encoding="utf-8")

    exit_code = main(["workflow", "install", "preview", "--repo-id", "demo"])

    assert exit_code == 1
    assert not (tmp_path / "deploy").exists()


def test_cli_parser_takeover_defaults() -> None:
    """takeover should have sensible defaults."""
    parser = build_parser()
    parsed = parser.parse_args(["takeover"])
    assert parsed.command == "takeover"
    assert parsed.owner is None
    assert parsed.limit == 100
    assert parsed.clone_root is None
    assert parsed.repos == []
    assert parsed.no_start is False
    assert parsed.dry_run is False


def test_cli_parser_takeover_with_options() -> None:
    """takeover should accept all defined options."""
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "takeover",
            "--owner",
            "myorg",
            "--limit",
            "50",
            "--clone-root",
            "/tmp/repos",
            "--repos",
            "owner/repo-a",
            "owner/repo-b",
            "--no-start",
            "--dry-run",
        ]
    )
    assert parsed.command == "takeover"
    assert parsed.owner == "myorg"
    assert parsed.limit == 50
    assert parsed.clone_root == "/tmp/repos"
    assert parsed.repos == ["owner/repo-a", "owner/repo-b"]
    assert parsed.no_start is True
    assert parsed.dry_run is True


def test_main_takeover_rejects_unauthenticated_gh(capsys) -> None:
    """takeover should fail gracefully when gh is not authenticated."""
    from backend.api.cli import main

    auth_client = MagicMock()
    auth_client.check_auth_status.return_value = MagicMock(
        authenticated=False,
        failure_reason="not logged in",
        account=None,
    )

    with patch(
        "backend.api.cli_takeover.create_github_client",
        return_value=auth_client,
    ):
        exit_code = main(["takeover"])

    assert exit_code == 1
    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"
    assert "GitHub CLI" in combined
    assert "gh auth login" in combined


def test_main_takeover_noninteractive_repos(capsys, monkeypatch) -> None:
    """takeover --repos should bypass interactive selection and execute."""
    from backend.api.cli import main

    monkeypatch.setenv("IAR_SKIP_GH_AUTH_CHECK", "1")

    auth_client = MagicMock()
    auth_client.check_auth_status.return_value = MagicMock(
        authenticated=True,
        failure_reason=None,
        account="user",
    )

    mock_result = MagicMock()
    mock_result.attempted = 1
    mock_result.succeeded = 1
    mock_result.started_daemons = 0
    mock_result.started_review_daemons = 0
    mock_result.repositories = (
        MagicMock(
            full_name="owner/repo-a",
            repo_id="owner-repo-a",
            repo_path=Path("/tmp/repos/owner/repo-a"),
            error=None,
        ),
    )

    with patch(
        "backend.api.cli_takeover.create_github_client",
        return_value=auth_client,
    ), patch(
        "backend.api.cli_takeover.select_repositories_interactive",
        side_effect=AssertionError("should not be called in non-interactive mode"),
    ), patch(
        "backend.api.cli_takeover.execute_takeover",
        return_value=mock_result,
    ) as mock_execute:
        exit_code = main(
            [
                "takeover",
                "--repos",
                "owner/repo-a",
                "--clone-root",
                "/tmp/repos",
                "--no-start",
            ]
        )

    assert exit_code == 0
    mock_execute.assert_called_once()
    captured = capsys.readouterr()
    assert "Takeover complete" in captured.out
