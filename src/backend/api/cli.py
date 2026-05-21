"""Command-line interface for issue-agent-runner."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from backend.core.use_cases.create_issue_from_prd import (
    IssueFromPrdRequest,
    PrdPublishContext,
    create_issue_from_prd,
    current_git_branch,
    parse_issue_number,
    publish_prd_file,
    resolve_prd_paths,
)
from backend.core.use_cases.run_agent_daemon import run_agent_daemon
from backend.core.use_cases.run_agent_repositories_once import (
    run_agent_repositories_once,
)
from backend.core.use_cases.sync_labels import sync_labels
from backend.engines.agent_runner.factory import (
    create_github_client,
    create_process_runner,
    get_agent_runner_settings,
    resolve_issue_from_prd_target,
    resolve_repository_targets,
)

if TYPE_CHECKING:
    from backend.core.shared.interfaces.agent_runner import (
        IGitHubClient,
        IProcessRunner,
    )
    from backend.core.shared.models.agent_runner import LabelConfig

_logger = logging.getLogger(__name__)


def _prompt_and_publish_prd_if_needed(
    *,
    repo_path: Path,
    relative_prd_path: Path,
    issue_url: str,
    queue_ready: bool,
    git_remote: str,
    labels_config: "LabelConfig",
    github_client: "IGitHubClient",
    process_runner: "IProcessRunner",
) -> bool:
    """Prompt user to commit and push PRD changes if working tree is dirty."""

    status_result = process_runner.run(["git", "status", "--porcelain"], cwd=repo_path)
    if not status_result.stdout.strip():
        return False

    prd_path_text = relative_prd_path.as_posix()
    print(f"\n检测到 PRD 文件有未提交的变更：{prd_path_text}")
    response = input("是否立即 commit 并 push 该变更？(y/N): ")
    if response.lower() not in ("y", "yes"):
        return False

    current_branch = current_git_branch(repo_path, process_runner)
    publish_context = PrdPublishContext(
        repo_path=repo_path,
        relative_prd_path=relative_prd_path,
        git_remote=git_remote,
        current_branch=current_branch,
    )
    publish_prd_file(publish_context, process_runner)
    if queue_ready:
        github_client.edit_issue_labels(
            parse_issue_number(issue_url),
            add=[labels_config.ready],
        )
    return True


def add_common_options(parser: argparse.ArgumentParser) -> None:
    """Allow global options before or after the effective subcommand."""
    parser.add_argument(
        "--repo", default=argparse.SUPPRESS, help="Target repository path."
    )
    parser.add_argument(
        "--repo-id", default=argparse.SUPPRESS, help="Target configured repository ID."
    )
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
        "--repo-id", default=None, help="Target configured repository ID."
    )
    parser.add_argument(
        "--config",
        help="Deprecated: config is loaded from config.toml and env vars.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    labels_parser = subparsers.add_parser("labels", help="Manage GitHub labels.")
    labels_subparsers = labels_parser.add_subparsers(
        dest="labels_command", required=True
    )
    labels_sync_parser = labels_subparsers.add_parser(
        "sync", help="Sync standard labels to the repository."
    )
    add_common_options(labels_sync_parser)

    issue_parser = subparsers.add_parser("issue-from-prd")
    issue_parser.add_argument("prd_path")
    issue_parser.add_argument(
        "--type", choices=("feature", "refactor", "bug"), default="feature"
    )
    issue_parser.add_argument("--title")
    issue_parser.add_argument(
        "--ready",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Add the ready label so a runner can pick the Issue up.",
    )
    issue_parser.add_argument(
        "--agent",
        choices=("auto", "codex", "claude", "kimi", "none"),
        default="auto",
        help="Optional agent routing label to add to the Issue.",
    )
    issue_parser.add_argument(
        "--publish-prd",
        action="store_true",
        help="Commit and push only the target PRD before adding the ready label.",
    )
    issue_parser.add_argument("--force", action="store_true")
    add_common_options(issue_parser)

    run_parser = subparsers.add_parser("run-once")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument(
        "--agent", choices=("auto", "codex", "claude", "kimi"), default="auto"
    )
    run_parser.add_argument("--max-issues", type=int)
    add_common_options(run_parser)

    daemon_parser = subparsers.add_parser("daemon")
    daemon_parser.add_argument("--interval", type=int, default=600)
    daemon_parser.add_argument(
        "--agent", choices=("auto", "codex", "claude", "kimi"), default="auto"
    )
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

    if parsed.config:
        _logger.warning(
            "The --config flag is deprecated. Use config.toml or env vars instead."
        )

    repo_id: str | None = getattr(parsed, "repo_id", None)
    repo_override: str | None = getattr(parsed, "repo", None)

    if repo_id is not None and repo_override is not None:
        _logger.error("--repo and --repo-id are mutually exclusive.")
        return 1

    process_runner = create_process_runner()
    runner_settings = get_agent_runner_settings()

    try:
        if parsed.command == "labels":
            contexts = resolve_repository_targets(
                runner_settings,
                repo_id=repo_id,
                repo_path_override=repo_override,
            )
            for context in contexts:
                github_client = create_github_client(context.repo_path, process_runner)
                sync_labels(
                    labels_config=context.config.labels, github_client=github_client
                )
            _logger.info("Labels are ready.")
            return 0

        if parsed.command == "issue-from-prd":
            context = resolve_issue_from_prd_target(
                runner_settings,
                repo_id=repo_id,
                repo_path_override=repo_override,
                cwd=Path.cwd(),
            )
            github_client = create_github_client(context.repo_path, process_runner)
            _, relative_prd_path = resolve_prd_paths(
                context.repo_path, Path(parsed.prd_path)
            )
            issue_url = create_issue_from_prd(
                request=IssueFromPrdRequest(
                    repo_path=context.repo_path,
                    prd_path=Path(parsed.prd_path),
                    issue_type=parsed.type,
                    title_override=parsed.title,
                    queue_ready=parsed.ready,
                    issue_agent=parsed.agent,
                    labels_config=context.config.labels,
                    force=parsed.force,
                    publish_prd=parsed.publish_prd,
                    git_remote=context.config.git.remote,
                    git_base_branch=context.config.git.base_branch,
                ),
                github_client=github_client,
                process_runner=process_runner,
            )
            if not parsed.publish_prd:
                _prompt_and_publish_prd_if_needed(
                    repo_path=context.repo_path,
                    relative_prd_path=relative_prd_path,
                    issue_url=issue_url,
                    queue_ready=parsed.ready,
                    git_remote=context.config.git.remote,
                    labels_config=context.config.labels,
                    github_client=github_client,
                    process_runner=process_runner,
                )
            if not parsed.ready:
                _logger.info(
                    "Issue created without '%s' label. "
                    "Use --ready if you want a runner to pick it up.",
                    context.config.labels.ready,
                )
            _logger.info("Created GitHub Issue: %s", issue_url)
            return 0

        if parsed.command == "run-once":
            contexts = resolve_repository_targets(
                runner_settings,
                repo_id=repo_id,
                repo_path_override=repo_override,
            )
            return run_agent_repositories_once(
                contexts=contexts,
                dry_run=parsed.dry_run,
                agent=parsed.agent,
                max_issues=parsed.max_issues or runner_settings.runner.max_issues,
                process_runner=process_runner,
                github_client_factory=lambda rp: create_github_client(
                    rp, process_runner
                ),
            )

        if parsed.command == "daemon":
            contexts = resolve_repository_targets(
                runner_settings,
                repo_id=repo_id,
                repo_path_override=repo_override,
            )
            run_agent_daemon(
                contexts=contexts,
                interval=parsed.interval,
                agent=parsed.agent,
                max_issues=parsed.max_issues or runner_settings.runner.max_issues,
                process_runner=process_runner,
                github_client_factory=lambda rp: create_github_client(
                    rp, process_runner
                ),
            )
            return 0
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
        _logger.error("iar failed: %s", exc)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
