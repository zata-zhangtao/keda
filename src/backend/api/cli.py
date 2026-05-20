"""Command-line interface for issue-agent-runner."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from backend.core.use_cases.create_issue_from_prd import create_issue_from_prd
from backend.core.use_cases.run_agent_daemon import run_agent_daemon
from backend.core.use_cases.run_agent_once import run_once
from backend.core.use_cases.sync_labels import sync_labels
from backend.engines.agent_runner.factory import (
    build_app_config,
    create_github_client,
    create_process_runner,
)

_logger = logging.getLogger(__name__)


def add_common_options(parser: argparse.ArgumentParser) -> None:
    """Allow global options before or after the effective subcommand."""
    parser.add_argument("--repo", default=argparse.SUPPRESS, help="Target repository path.")
    parser.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="Deprecated: config is loaded from config.toml and env vars.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(prog="iar")
    parser.add_argument("--repo", default=".", help="Target repository path.")
    parser.add_argument(
        "--config",
        help="Deprecated: config is loaded from config.toml and env vars.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    labels_parser = subparsers.add_parser("labels", help="Manage GitHub labels.")
    labels_subparsers = labels_parser.add_subparsers(dest="labels_command", required=True)
    labels_sync_parser = labels_subparsers.add_parser("sync", help="Sync standard labels to the repository.")
    add_common_options(labels_sync_parser)

    issue_parser = subparsers.add_parser("issue-from-prd")
    issue_parser.add_argument("prd_path")
    issue_parser.add_argument("--type", choices=("feature", "refactor", "bug"), default="feature")
    issue_parser.add_argument("--title")
    issue_parser.add_argument(
        "--ready",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Add the ready label so a runner can pick the Issue up.",
    )
    issue_parser.add_argument(
        "--agent",
        choices=("auto", "codex", "claude", "none"),
        default="auto",
        help="Optional agent routing label to add to the Issue.",
    )
    issue_parser.add_argument("--force", action="store_true")
    add_common_options(issue_parser)

    run_parser = subparsers.add_parser("run-once")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--agent", choices=("auto", "codex", "claude"), default="auto")
    run_parser.add_argument("--max-issues", type=int)
    add_common_options(run_parser)

    daemon_parser = subparsers.add_parser("daemon")
    daemon_parser.add_argument("--interval", type=int, default=600)
    daemon_parser.add_argument("--agent", choices=("auto", "codex", "claude"), default="auto")
    daemon_parser.add_argument("--max-issues", type=int)
    add_common_options(daemon_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    parsed = build_parser().parse_args(argv)
    repo_path = Path(parsed.repo).resolve()
    config = build_app_config()
    process_runner = create_process_runner()

    if parsed.config:
        _logger.warning("The --config flag is deprecated. Use config.toml or env vars instead.")

    try:
        if parsed.command == "labels":
            github_client = create_github_client(repo_path, process_runner)
            sync_labels(labels_config=config.labels, github_client=github_client)
            _logger.info("Labels are ready.")
            return 0
        if parsed.command == "issue-from-prd":
            github_client = create_github_client(repo_path, process_runner)
            issue_url = create_issue_from_prd(
                repo_path=repo_path,
                prd_path=Path(parsed.prd_path),
                issue_type=parsed.type,
                title_override=parsed.title,
                queue_ready=parsed.ready,
                issue_agent=parsed.agent,
                labels_config=config.labels,
                force=parsed.force,
                github_client=github_client,
            )
            _logger.info("Created GitHub Issue: %s", issue_url)
            return 0
        if parsed.command == "run-once":
            github_client = create_github_client(repo_path, process_runner)
            return run_once(
                repo_path=repo_path,
                config=config,
                dry_run=parsed.dry_run,
                agent=parsed.agent,
                max_issues=parsed.max_issues or config.runner.max_issues,
                github_client=github_client,
                process_runner=process_runner,
            )
        if parsed.command == "daemon":
            github_client = create_github_client(repo_path, process_runner)
            run_agent_daemon(
                repo_path=repo_path,
                config=config,
                interval=parsed.interval,
                agent=parsed.agent,
                max_issues=parsed.max_issues or config.runner.max_issues,
                github_client=github_client,
                process_runner=process_runner,
            )
            return 0
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
        _logger.error("iar failed: %s", exc)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
