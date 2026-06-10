"""Command-line interface for issue-agent-runner.

NOTE: This argparse-based parser is still the execution backend for
``backend.api.cli_typer``. When adding or changing CLI options, defaults, or
argument structure, keep ``cli_typer.py`` in sync so the actual ``iar`` entry
point and its help text stay consistent.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console

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
from backend.core.use_cases.run_agent_deliberation import (
    DeliberationRequest,
    create_default_session_id,
    run_agent_deliberation,
)
from backend.core.use_cases.interactive_decision import run_interactive_decision
from backend.core.use_cases.run_agent_repositories_once import (
    run_agent_repositories_once,
)
from backend.core.use_cases.review_daemon import run_review_daemon
from backend.core.use_cases.review_once import review_once
from backend.core.use_cases.sync_labels import sync_labels
from backend.core.use_cases.worktree_cleanup import (
    WorktreeCleanupRequest,
    WorktreeCleanupResult,
    WorktreeCleanupStatus,
    cleanup_iar_worktrees,
)
from backend.core.shared.models.agent_deliberation import DeliberationSession
from backend.core.shared.models.agent_runner import LabelConfig
from backend.engines.agent_runner.factory import (
    build_deliberation_config_from_settings,
    create_console_store,
    create_content_generator,
    create_event_sink,
    create_github_client,
    create_planner_runner,
    create_process_runner,
    create_transcript_runner,
    get_agent_runner_settings,
    logger,
    resolve_issue_from_prd_target,
    resolve_repository_targets,
    write_deliberation_outputs,
)
from backend.engines.agent_runner.live_terminal import create_output_view
from backend.engines.agent_runner.repository_local import (
    RepositoryInitOptions,
    detect_git_repository_root,
    initialize_repository_local_config,
)
from backend.engines.agent_runner.worktree_cli import (
    build_worktree_manager,
)

console = Console()
error_console = Console(stderr=True)

_MAX_CLI_ERROR_STREAM_CHARS = 12000


if TYPE_CHECKING:
    from backend.core.shared.interfaces.agent_runner import (
        IGitHubClient,
        IProcessRunner,
    )
    from backend.core.shared.models.agent_runner import (
        LabelConfig,
        RepositoryRunContext,
    )


def _format_command_for_cli(command: object) -> str:
    """Format a failed command for CLI diagnostics."""
    if isinstance(command, str):
        return command
    if isinstance(command, (list, tuple)):
        return shlex.join(str(command_part) for command_part in command)
    return str(command)


def _decode_cli_error_stream(stream_value: object) -> str:
    """Decode captured subprocess output for CLI diagnostics."""
    if stream_value is None:
        return ""
    if isinstance(stream_value, bytes):
        return stream_value.decode("utf-8", errors="replace")
    return str(stream_value)


def _truncate_cli_error_stream(stream_text: str) -> str:
    """Limit very large captured command output in CLI diagnostics."""
    if len(stream_text) <= _MAX_CLI_ERROR_STREAM_CHARS:
        return stream_text
    omitted_char_count = len(stream_text) - _MAX_CLI_ERROR_STREAM_CHARS
    return (
        stream_text[:_MAX_CLI_ERROR_STREAM_CHARS]
        + f"\n... truncated {omitted_char_count} chars ..."
    )


def _format_cli_exception(exc: BaseException) -> str:
    """Format an exception with subprocess stdout/stderr when available."""
    if not isinstance(exc, subprocess.CalledProcessError):
        return str(exc)

    lines = [
        "Command failed.",
        f"Command: {_format_command_for_cli(exc.cmd)}",
        f"Exit code: {exc.returncode}",
    ]
    stdout_text = _truncate_cli_error_stream(_decode_cli_error_stream(exc.output))
    stderr_text = _truncate_cli_error_stream(_decode_cli_error_stream(exc.stderr))
    if stdout_text:
        lines.extend(["", "stdout:", stdout_text.rstrip()])
    if stderr_text:
        lines.extend(["", "stderr:", stderr_text.rstrip()])
    if not stdout_text and not stderr_text:
        lines.append("No stdout or stderr was captured.")
    return "\n".join(lines)


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


def _ensure_gh_auth_or_prompt(
    repo_path: Path, process_runner: "IProcessRunner"
) -> None:
    """Check gh auth status and exit with a friendly message if not authenticated."""
    if os.environ.get("IAR_SKIP_GH_AUTH_CHECK") == "1":
        return
    github_client = create_github_client(repo_path, process_runner)
    auth_status = github_client.check_auth_status()
    if auth_status.authenticated:
        return
    error_console.print("[red]GitHub CLI 认证失败。[/]")
    if auth_status.failure_reason:
        error_console.print(f"[red]{auth_status.failure_reason}[/]")
    error_console.print("[yellow]请运行: gh auth login -h github.com[/]")
    raise SystemExit(1)


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


def add_all_repositories_option(parser: argparse.ArgumentParser) -> None:
    """Allow explicit multi-repository selection for configured repositories."""
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_repositories",
        help="Process all enabled configured repositories.",
    )


def _print_worktree_cleanup_result(cleanup_result: WorktreeCleanupResult) -> None:
    """Print a concise branch cleanup summary."""
    if not cleanup_result.branches:
        console.print("[green]No local iAR issue branches found.[/]")
        return

    for branch_result in cleanup_result.branches:
        worktree_suffix = (
            f" ({branch_result.worktree_path})" if branch_result.worktree_path else ""
        )
        if branch_result.status is WorktreeCleanupStatus.WOULD_DELETE:
            console.print(
                f"[yellow]Would delete:[/] {branch_result.branch}{worktree_suffix} - "
                f"{branch_result.reason}"
            )
        elif branch_result.status is WorktreeCleanupStatus.DELETED:
            console.print(
                f"[green]Deleted:[/] {branch_result.branch}{worktree_suffix} - "
                f"{branch_result.reason}"
            )
        elif branch_result.status is WorktreeCleanupStatus.FAILED:
            console.print(
                f"[red]Failed:[/] {branch_result.branch}{worktree_suffix} - "
                f"{branch_result.reason}"
            )
        else:
            console.print(
                f"[dim]Skipped:[/] {branch_result.branch}{worktree_suffix} - "
                f"{branch_result.reason}"
            )

    console.print(
        "Cleanup summary: "
        f"deleted={cleanup_result.deleted_count}, "
        f"would_delete={cleanup_result.would_delete_count}, "
        f"skipped={cleanup_result.skipped_count}, "
        f"failed={cleanup_result.failed_count}"
    )


def _resolve_cli_repository_targets(
    *,
    parsed: argparse.Namespace,
    runner_settings: Any,
    repo_id: str | None,
    repo_override: str | None,
) -> list["RepositoryRunContext"]:
    """Resolve repository targets for parsed CLI selectors."""
    return resolve_repository_targets(
        runner_settings,
        repo_id=repo_id,
        repo_path_override=repo_override,
        all_repositories=getattr(parsed, "all_repositories", False),
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(prog="iar")
    parser.add_argument("--repo", default=None, help="Target repository path.")
    parser.add_argument(
        "--repo-id", default=None, help="Target configured repository ID."
    )
    parser.add_argument(
        "--config",
        help="Deprecated: config is loaded from config.toml and env vars.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="Create repository-local .iar.toml config."
    )
    init_parser.add_argument("--dry-run", action="store_true")
    init_parser.add_argument("--force", action="store_true")
    init_parser.add_argument("--id", dest="repository_id")
    init_parser.add_argument("--display-name")
    init_parser.add_argument("--remote")
    init_parser.add_argument("--base-branch")

    labels_parser = subparsers.add_parser("labels", help="Manage GitHub labels.")
    labels_subparsers = labels_parser.add_subparsers(
        dest="labels_command", required=True
    )
    labels_sync_parser = labels_subparsers.add_parser(
        "sync", help="Sync standard labels to the repository."
    )
    add_common_options(labels_sync_parser)
    add_all_repositories_option(labels_sync_parser)

    issue_parser = subparsers.add_parser(
        "issue", help="Create and manage GitHub Issues."
    )
    issue_subparsers = issue_parser.add_subparsers(dest="issue_command", required=True)
    issue_create_parser = issue_subparsers.add_parser(
        "create", help="Create a GitHub Issue from a PRD file."
    )
    issue_create_parser.set_defaults(command="issue create")
    issue_create_parser.add_argument("prd_path")
    issue_create_parser.add_argument(
        "--type", choices=("feature", "refactor", "bug"), default="feature"
    )
    issue_create_parser.add_argument("--title")
    issue_create_parser.add_argument(
        "--ready",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Add the ready label so a runner can pick the Issue up.",
    )
    issue_create_parser.add_argument(
        "--agent",
        choices=("auto", "codex", "claude", "kimi", "none"),
        default="auto",
        help="Optional agent routing label to add to the Issue.",
    )
    issue_create_parser.add_argument(
        "--publish-prd",
        action="store_true",
        help="Commit and push only the target PRD before adding the ready label.",
    )
    issue_create_parser.add_argument("--force", action="store_true")
    issue_create_parser.add_argument(
        "--group",
        default="",
        help="Task group name (materialised as task-group/<name> label).",
    )
    issue_create_parser.add_argument(
        "--depends-on",
        action="append",
        type=int,
        default=[],
        help="Upstream Issue number this Issue depends on (repeatable).",
    )
    issue_create_parser.add_argument(
        "--depends-on-group",
        action="append",
        type=str,
        default=[],
        help="Upstream group label this Issue depends on (repeatable).",
    )
    add_common_options(issue_create_parser)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument(
        "--agent", choices=("auto", "codex", "claude", "kimi"), default="auto"
    )
    run_parser.add_argument("--max-issues", type=int)
    add_common_options(run_parser)
    add_all_repositories_option(run_parser)

    daemon_parser = subparsers.add_parser("daemon")
    daemon_parser.add_argument("--interval", type=int, default=None)
    daemon_parser.add_argument(
        "--agent", choices=("auto", "codex", "claude", "kimi"), default="auto"
    )
    daemon_parser.add_argument("--max-issues", type=int)
    add_common_options(daemon_parser)
    add_all_repositories_option(daemon_parser)

    review_parser = subparsers.add_parser("review")
    review_parser.add_argument("--dry-run", action="store_true")
    review_parser.add_argument(
        "--agent", choices=("auto", "codex", "claude", "kimi"), default="auto"
    )
    review_parser.add_argument("--max-issues", type=int)
    add_common_options(review_parser)
    add_all_repositories_option(review_parser)

    review_daemon_parser = subparsers.add_parser("review-daemon")
    review_daemon_parser.add_argument("--interval", type=int, default=None)
    review_daemon_parser.add_argument(
        "--agent", choices=("auto", "codex", "claude", "kimi"), default="auto"
    )
    review_daemon_parser.add_argument("--max-issues", type=int)
    add_common_options(review_daemon_parser)
    add_all_repositories_option(review_daemon_parser)

    recover_parser = subparsers.add_parser(
        "recover",
        help="Resume a failed publish operation for an Issue.",
    )
    recover_parser.add_argument(
        "--issue",
        type=int,
        required=True,
        help="Issue number to recover publish for.",
    )
    recover_parser.add_argument(
        "--branch",
        default=None,
        help="Explicitly confirm the current branch name.",
    )
    add_common_options(recover_parser)

    blocked_continue_parser = subparsers.add_parser(
        "blocked-continue",
        help="Resume a blocked Issue after resolving forbidden paths.",
    )
    blocked_continue_parser.add_argument(
        "--issue",
        type=int,
        required=True,
        help="Issue number to continue.",
    )
    blocked_continue_parser.add_argument(
        "--agent",
        choices=("auto", "codex", "claude", "kimi"),
        default="auto",
        help="Agent runner to use.",
    )
    add_common_options(blocked_continue_parser)

    ask_parser = subparsers.add_parser(
        "ask", help="Ask the agent runner to decide the next safe action."
    )
    ask_parser.add_argument("prompt", help="Natural language request.")
    ask_parser.add_argument(
        "--agent",
        choices=("auto", "codex", "claude", "kimi"),
        default="auto",
        help="Planner agent to use.",
    )
    ask_parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Only generate plan without executing.",
    )
    ask_parser.add_argument(
        "--execute",
        action="store_true",
        help="Allow execution after confirmation.",
    )
    ask_parser.add_argument(
        "--yes",
        action="store_true",
        help="Auto-confirm non-interactive execution.",
    )
    ask_parser.add_argument(
        "--output",
        default=None,
        help="Output directory for decision audit.",
    )
    add_common_options(ask_parser)

    deliberate_parser = subparsers.add_parser(
        "deliberate", help="Run a multi-agent deliberation session."
    )
    deliberate_parser.add_argument(
        "prompt", help="The requirement or question to deliberate."
    )
    deliberate_parser.add_argument(
        "--agents",
        default="architect,skeptic,implementer",
        help="Comma-separated participant profile IDs.",
    )
    deliberate_parser.add_argument(
        "--rounds", type=int, default=None, help="Number of discussion rounds."
    )
    deliberate_parser.add_argument(
        "--synthesizer", default=None, help="Agent to run synthesis."
    )
    deliberate_parser.add_argument(
        "--output",
        default=None,
        help="Output directory for deliberation files.",
    )
    deliberate_parser.add_argument(
        "--session-id", default=None, help="Optional session ID for reproducibility."
    )
    add_common_options(deliberate_parser)

    worktree_parser = subparsers.add_parser(
        "worktree",
        help="Manage iAR-owned Git worktrees for the current repository.",
    )
    worktree_subparsers = worktree_parser.add_subparsers(
        dest="worktree_command", required=True
    )
    worktree_create_parser = worktree_subparsers.add_parser(
        "create", help="Create a worktree at .iar-worktrees/<branch>."
    )
    worktree_create_parser.add_argument(
        "--branch", required=True, help="Branch name to create."
    )
    worktree_create_parser.add_argument(
        "--base-branch", required=True, help="Existing branch to fork from."
    )
    worktree_path_parser = worktree_subparsers.add_parser(
        "path", help="Print the absolute worktree path for a branch."
    )
    worktree_path_parser.add_argument(
        "--branch", required=True, help="Branch name to resolve."
    )
    worktree_remove_parser = worktree_subparsers.add_parser(
        "remove", help="Remove a worktree and prune Git metadata."
    )
    worktree_remove_parser.add_argument(
        "--branch", required=True, help="Branch name whose worktree to remove."
    )
    worktree_cleanup_parser = worktree_subparsers.add_parser(
        "cleanup",
        help="Delete stale local issue branches whose Issue is closed.",
    )
    worktree_cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview cleanup without deleting anything.",
    )
    worktree_cleanup_parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete eligible branches and worktrees.",
    )
    worktree_cleanup_parser.add_argument(
        "--force",
        action="store_true",
        help="Also delete dirty or unmerged eligible branches.",
    )
    return parser


def _resolve_run_trigger(command_kind: str) -> str:
    """解析运行记录的 trigger 来源。

    管理终端托管的子进程带有 ``IAR_CONSOLE=1`` 环境标记，记为
    ``console_*``；否则记为 ``cli_*``。

    Args:
        command_kind: ``"run"`` 或 ``"daemon"``。
    """
    prefix = "console" if os.environ.get("IAR_CONSOLE") == "1" else "cli"
    return f"{prefix}_{command_kind}"


def _create_run_history_store_or_none():
    """创建运行历史存储；初始化失败时降级为 None（不阻断 CLI）。"""
    try:
        return create_console_store()
    except Exception as exc:  # noqa: BLE001 - history is a side channel.
        logger.warning("Run history store unavailable: %s", exc)
        return None


def _run_parsed_command(parsed: argparse.Namespace) -> int:
    """Run a command after CLI arguments have been parsed."""
    if parsed.config:
        logger.warning(
            "The --config flag is deprecated. Use config.toml or env vars instead."
        )

    repo_id: str | None = getattr(parsed, "repo_id", None)
    repo_override: str | None = getattr(parsed, "repo", None)

    if repo_id is not None and repo_override is not None:
        logger.error("--repo and --repo-id are mutually exclusive.")
        return 1

    process_runner = create_process_runner()

    if parsed.command == "init":
        if repo_id is not None or repo_override is not None:
            logger.error(
                "iar init uses the current Git repository; omit --repo/--repo-id."
            )
            return 1
        try:
            init_result = initialize_repository_local_config(
                RepositoryInitOptions(
                    cwd=Path.cwd(),
                    repo_id_override=parsed.repository_id,
                    display_name_override=parsed.display_name,
                    remote_override=parsed.remote,
                    base_branch_override=parsed.base_branch,
                    dry_run=parsed.dry_run,
                    force=parsed.force,
                ),
                process_runner,
            )
        except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
            logger.error("iar init failed: %s", exc)
            return 1
        if parsed.dry_run:
            print(init_result.config_text, end="")
            return 0

        logger.info("Wrote IAR local config: %s", init_result.config_path)
        console.print(f"[green]Wrote IAR local config:[/] {init_result.config_path}")
        try:
            github_client = create_github_client(
                init_result.repo_root_path, process_runner
            )
            sync_labels(labels_config=LabelConfig(), github_client=github_client)
            logger.info("Labels synced for: %s", init_result.repo_root_path)
            console.print(f"[green]Labels synced for:[/] {init_result.repo_root_path}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Label sync failed (labels may already exist): %s", exc)
            error_console.print(f"[yellow]Label sync failed:[/] {exc}")
        return 0

    if parsed.command == "worktree":
        try:
            repo_root_path = detect_git_repository_root(Path.cwd(), process_runner)
        except ValueError as exc:
            logger.error("iar worktree failed: %s", exc)
            return 1
        manager = build_worktree_manager(repo_root_path, process_runner)
        if parsed.worktree_command == "create":
            manager.create(branch=parsed.branch, base_branch=parsed.base_branch)
            return 0
        if parsed.worktree_command == "path":
            print(str(manager.worktree_path(parsed.branch)))
            return 0
        if parsed.worktree_command == "remove":
            manager.remove(branch=parsed.branch)
            return 0
        if parsed.worktree_command == "cleanup":
            runner_settings = get_agent_runner_settings()
            contexts = resolve_repository_targets(
                runner_settings,
                fallback_path=str(repo_root_path),
            )
            if len(contexts) != 1:
                logger.error("iar worktree cleanup requires exactly one repository.")
                return 1
            context = contexts[0]
            _ensure_gh_auth_or_prompt(context.repo_path, process_runner)
            github_client = create_github_client(context.repo_path, process_runner)
            cleanup_request = WorktreeCleanupRequest(
                repo_path=context.repo_path,
                remote=context.config.git.remote,
                base_branch=context.config.git.base_branch,
                dry_run=parsed.dry_run or not parsed.yes,
                force=parsed.force,
                managed_worktree_root_path=manager.worktree_root,
            )
            cleanup_result = cleanup_iar_worktrees(
                cleanup_request,
                github_client=github_client,
                process_runner=process_runner,
            )
            _print_worktree_cleanup_result(cleanup_result)
            return 1 if cleanup_result.failed_count else 0
        logger.error("iar worktree: unknown subcommand %r", parsed.worktree_command)
        return 1

    runner_settings = get_agent_runner_settings()

    def github_client_factory(repo_path: Path) -> "IGitHubClient":
        return create_github_client(repo_path, process_runner)

    try:
        if parsed.command == "labels":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            if contexts:
                _ensure_gh_auth_or_prompt(contexts[0].repo_path, process_runner)
            for context in contexts:
                github_client = github_client_factory(context.repo_path)
                sync_labels(
                    labels_config=context.config.labels, github_client=github_client
                )
            logger.info("Labels are ready.")
            return 0

        if parsed.command == "issue create":
            context = resolve_issue_from_prd_target(
                runner_settings,
                repo_id=repo_id,
                repo_path_override=repo_override,
                cwd=Path.cwd(),
            )
            _ensure_gh_auth_or_prompt(context.repo_path, process_runner)
            github_client = create_github_client(context.repo_path, process_runner)
            _, relative_prd_path = resolve_prd_paths(
                context.repo_path, Path(parsed.prd_path)
            )
            gc_config = context.config.generated_content
            content_generator = None
            if gc_config.enabled and gc_config.issue_from_prd.enabled:
                if gc_config.issue_from_prd.mode == "agent":
                    content_generator = create_content_generator(process_runner)
            # 当不显式 --publish-prd 时，先把 queue_ready 压成 False，
            # 避免 Issue 还没发布就已经 ready，runner 在 worktree 里读到过时 PRD。
            # 交互式 prompt 在 push 成功后再补 ready。
            queue_ready_for_request = parsed.ready if parsed.publish_prd else False
            issue_url = create_issue_from_prd(
                request=IssueFromPrdRequest(
                    repo_path=context.repo_path,
                    prd_path=Path(parsed.prd_path),
                    issue_type=parsed.type,
                    title_override=parsed.title,
                    queue_ready=queue_ready_for_request,
                    issue_agent=parsed.agent,
                    labels_config=context.config.labels,
                    force=parsed.force,
                    publish_prd=parsed.publish_prd,
                    git_remote=context.config.git.remote,
                    git_base_branch=context.config.git.base_branch,
                    generated_content_config=gc_config,
                    group=getattr(parsed, "group", "") or "",
                    depends_on=tuple(getattr(parsed, "depends_on", []) or []),
                    depends_on_group=tuple(
                        getattr(parsed, "depends_on_group", []) or []
                    ),
                    parse_evidence_format_with_agent=context.config.validation.parse_evidence_format_with_agent,
                ),
                github_client=github_client,
                process_runner=process_runner,
                content_generator=content_generator,
            )
            published = False
            if not parsed.publish_prd:
                published = _prompt_and_publish_prd_if_needed(
                    repo_path=context.repo_path,
                    relative_prd_path=relative_prd_path,
                    issue_url=issue_url,
                    queue_ready=parsed.ready,
                    git_remote=context.config.git.remote,
                    labels_config=context.config.labels,
                    github_client=github_client,
                    process_runner=process_runner,
                )
            if not parsed.ready or (
                parsed.ready and not parsed.publish_prd and not published
            ):
                logger.info(
                    "Issue created without '%s' label. "
                    "Use --ready if you want a runner to pick it up.",
                    context.config.labels.ready,
                )
            logger.info("Created GitHub Issue: %s", issue_url)
            console.print(f"[green]Created GitHub Issue:[/] {issue_url}")
            return 0

        if parsed.command == "run":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            if contexts:
                _ensure_gh_auth_or_prompt(contexts[0].repo_path, process_runner)
            content_generator = create_content_generator(process_runner)
            return run_agent_repositories_once(
                contexts=contexts,
                dry_run=parsed.dry_run,
                agent=parsed.agent,
                max_issues=parsed.max_issues or runner_settings.runner.max_issues,
                process_runner=process_runner,
                github_client_factory=github_client_factory,
                content_generator=content_generator,
                run_history_store=_create_run_history_store_or_none(),
                run_trigger=_resolve_run_trigger("run"),
            )

        if parsed.command == "daemon":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            if contexts:
                _ensure_gh_auth_or_prompt(contexts[0].repo_path, process_runner)
            interval = (
                parsed.interval
                if parsed.interval is not None
                else runner_settings.daemon.run_interval_seconds
            )
            run_agent_daemon(
                contexts=contexts,
                interval=interval,
                agent=parsed.agent,
                max_issues=parsed.max_issues or runner_settings.runner.max_issues,
                process_runner=process_runner,
                github_client_factory=github_client_factory,
                run_history_store=_create_run_history_store_or_none(),
                run_trigger=_resolve_run_trigger("daemon"),
            )
            return 0

        if parsed.command == "review":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            if contexts:
                _ensure_gh_auth_or_prompt(contexts[0].repo_path, process_runner)
            aggregated_exit_code = 0
            for context in contexts:
                github_client = github_client_factory(context.repo_path)
                try:
                    repo_exit_code = review_once(
                        repo_path=context.repo_path,
                        config=context.config,
                        dry_run=parsed.dry_run,
                        agent=parsed.agent,
                        max_issues=parsed.max_issues
                        or runner_settings.runner.max_issues,
                        github_client=github_client,
                        process_runner=process_runner,
                    )
                    if repo_exit_code != 0:
                        aggregated_exit_code = 1
                except Exception as exc:  # noqa: BLE001
                    aggregated_exit_code = 1
                    logger.error(
                        "Repository '%s' review_once failed: %s",
                        context.repo_id,
                        exc,
                    )
            return aggregated_exit_code

        if parsed.command == "review-daemon":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            if contexts:
                _ensure_gh_auth_or_prompt(contexts[0].repo_path, process_runner)
            interval = (
                parsed.interval
                if parsed.interval is not None
                else runner_settings.daemon.review_interval_seconds
            )
            run_review_daemon(
                contexts=contexts,
                interval=interval,
                agent=parsed.agent,
                max_issues=parsed.max_issues or runner_settings.runner.max_issues,
                process_runner=process_runner,
                github_client_factory=github_client_factory,
            )
            return 0

        if parsed.command == "recover":
            from backend.core.use_cases.recover_publish import (
                PublishRecoveryError,
                PublishRecoveryRequest,
                recover_publish_issue,
            )

            contexts = resolve_repository_targets(
                runner_settings,
                repo_id=repo_id,
                repo_path_override=repo_override,
            )
            if len(contexts) != 1:
                logger.error(
                    "recover requires exactly one target repository. "
                    "Use --repo or --repo-id to specify."
                )
                return 1
            context = contexts[0]
            github_client = create_github_client(context.repo_path, process_runner)
            request = PublishRecoveryRequest(
                issue_number=parsed.issue,
                expected_branch=parsed.branch,
            )
            try:
                result = recover_publish_issue(
                    request=request,
                    repo_path=context.repo_path,
                    config=context.config,
                    github_client=github_client,
                    process_runner=process_runner,
                )
                logger.info(
                    "Publish recovered for Issue #%d: %s",
                    result.issue_number,
                    result.pr_url,
                )
                console.print(
                    f"[green]Publish recovered for Issue "
                    f"#{result.issue_number}:[/] {result.pr_url}"
                )
                return 0
            except PublishRecoveryError as exc:
                logger.error(
                    "Publish recovery failed (category=%s): %s",
                    exc.failure_category,
                    exc,
                )
                return 1

        if parsed.command == "blocked-continue":
            from backend.core.use_cases.blocked_continue import (
                BlockedContinueError,
                blocked_continue_issue,
            )

            contexts = resolve_repository_targets(
                runner_settings,
                repo_id=repo_id,
                repo_path_override=repo_override,
            )
            if len(contexts) != 1:
                logger.error(
                    "blocked-continue requires exactly one target repository. "
                    "Use --repo or --repo-id to specify."
                )
                return 1
            context = contexts[0]
            github_client = create_github_client(context.repo_path, process_runner)
            try:
                claimed = blocked_continue_issue(
                    issue_number=parsed.issue,
                    repo_path=context.repo_path,
                    config=context.config,
                    agent=parsed.agent,
                    github_client=github_client,
                    process_runner=process_runner,
                )
                if claimed:
                    console.print(
                        f"[green]Issue #{parsed.issue} resumed successfully.[/]"
                    )
                    return 0
                console.print(
                    f"[yellow]Issue #{parsed.issue} was claimed by another runner.[/]"
                )
                return 0
            except BlockedContinueError as exc:
                logger.error("blocked-continue failed: %s", exc)
                error_console.print(f"[red]blocked-continue failed:[/] {exc}")
                return 1

        if parsed.command == "ask":
            contexts = _resolve_cli_repository_targets(
                parsed=parsed,
                runner_settings=runner_settings,
                repo_id=repo_id,
                repo_override=repo_override,
            )
            if len(contexts) != 1:
                logger.error(
                    "ask requires exactly one target repository. "
                    "Use --repo or --repo-id to specify."
                )
                return 1
            context = contexts[0]
            _ensure_gh_auth_or_prompt(context.repo_path, process_runner)
            github_client = create_github_client(context.repo_path, process_runner)
            planner_runner = create_planner_runner(process_runner)
            content_generator = create_content_generator(process_runner)
            agent = parsed.agent
            if agent == "auto":
                agent = context.config.interactive_decision.default_agent
            output_dir = None
            if parsed.output:
                output_dir = Path(parsed.output)
            deliberation_config = build_deliberation_config_from_settings(
                runner_settings
            )
            transcript_runner = create_transcript_runner(process_runner)
            output_view = create_output_view()
            event_sink = create_event_sink(
                Path(context.config.interactive_decision.default_output_dir),
                output_view,
            )
            return run_interactive_decision(
                user_prompt=parsed.prompt,
                context=context,
                config=context.config.interactive_decision,
                agent=agent,
                plan_only=parsed.plan_only,
                execute=parsed.execute,
                auto_confirm=parsed.yes,
                output_dir=output_dir,
                planner_runner=planner_runner,
                github_client=github_client,
                process_runner=process_runner,
                content_generator=content_generator,
                github_client_factory=github_client_factory,
                deliberation_deps={
                    "config": deliberation_config,
                    "transcript_runner": transcript_runner,
                    "event_sink": event_sink,
                    "output_view": output_view,
                },
            )

        if parsed.command == "deliberate":
            deliberation_settings = runner_settings.deliberation
            output_dir = parsed.output or deliberation_settings.default_output_dir
            rounds = (
                parsed.rounds
                if parsed.rounds is not None
                else deliberation_settings.default_rounds
            )
            synthesizer = (
                parsed.synthesizer or deliberation_settings.default_synthesizer
            )
            agents = tuple(a.strip() for a in parsed.agents.split(",") if a.strip())
            session_id = parsed.session_id or create_default_session_id()
            output_path = Path(output_dir) / session_id
            request = DeliberationRequest(
                prompt=parsed.prompt,
                agents=agents,
                rounds=rounds,
                synthesizer=synthesizer,
                output_dir=str(output_path),
                session_id=session_id,
            )
            deliberation_config = build_deliberation_config_from_settings(
                runner_settings
            )
            transcript_runner = create_transcript_runner(process_runner)
            output_path.mkdir(parents=True, exist_ok=True)
            output_view = create_output_view()
            event_sink = create_event_sink(output_path, output_view)
            result = run_agent_deliberation(
                request=request,
                config=deliberation_config,
                transcript_runner=transcript_runner,
                event_sink=event_sink,
                target_repo_path=Path.cwd(),
                output_view=output_view,
            )
            selected_profile_ids = tuple(
                dict.fromkeys(
                    profile_id
                    for outputs in result.agent_outputs.values()
                    for profile_id in outputs
                )
            )
            profiles_by_id = {
                profile.profile_id: profile for profile in deliberation_config.profiles
            }
            session_profiles = tuple(
                profiles_by_id[profile_id]
                for profile_id in selected_profile_ids
                if profile_id in profiles_by_id
            )
            session = DeliberationSession(
                session_id=result.session_id,
                prompt=result.prompt,
                profiles=session_profiles,
                rounds=request.rounds,
                synthesizer=request.synthesizer,
                output_dir=output_path,
                started_at=result.started_at,
                finished_at=result.finished_at,
            )
            write_deliberation_outputs(result, session, output_path)
            console.print(f"\n[green]Deliberation complete:[/] {output_path}")
            return 0
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
        error_detail = _format_cli_exception(exc)
        logger.error("iar failed:\n%s", error_detail)
        error_console.print("[red]iar failed:[/]")
        error_console.print(error_detail, markup=False)
        return 1
    return 1


def main(argv: list[str] | None = None) -> int:
    """Run the Typer-powered CLI."""
    from backend.api.cli_typer import main as typer_main

    return typer_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
