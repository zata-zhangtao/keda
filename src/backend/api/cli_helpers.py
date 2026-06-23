"""Shared CLI helper utilities used by ``backend.api.cli``.

These helpers are thin presentation-layer adapters (auth checks, output
formatting, target resolution) that do not themselves invoke long-running
business logic. Keeping them in a dedicated module lets the main dispatch
file stay focused on command wiring.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.api.cli_console import console, error_console
from backend.core.use_cases.worktree_cleanup import (
    WorktreeCleanupResult,
    WorktreeCleanupStatus,
)
from backend.engines.agent_runner.factory import (
    create_console_store,
    create_github_client,
    find_repository_match_for_path,
    load_fresh_agent_runner_settings,
    logger,
    resolve_repository_targets,
)
from backend.engines.agent_runner.repository_local import (
    IARRepositoryNotInitializedError,
    detect_git_repository_root,
    require_iar_repository_initialized,
)

if TYPE_CHECKING:
    from backend.core.shared.interfaces.agent_runner import IProcessRunner
    from backend.core.shared.models.agent_runner import RepositoryRunContext


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


@dataclasses.dataclass(frozen=True)
class _DefaultDaemonTarget:
    """Result of inferring a daemon target from cwd."""

    repo_id: str | None
    error: str


def _resolve_default_daemon_target() -> _DefaultDaemonTarget:
    """Infer the daemon target repository from the current working directory.

    Returns:
        _DefaultDaemonTarget: when ``repo_id`` is set, use that repository;
        when ``error`` is set, fail early with the error message. This function
        no longer falls back to ``--all``; callers must explicitly request all
        enabled registry entries.
    """
    try:
        cwd_git_root = detect_git_repository_root(Path.cwd())
    except ValueError:
        return _DefaultDaemonTarget(
            repo_id=None,
            error=(
                "Current directory is not a Git repository. "
                "Run from an initialized iAR repository, or use --all to target all enabled registry entries."
            ),
        )
    settings = load_fresh_agent_runner_settings()
    match = find_repository_match_for_path(settings, cwd_git_root)
    if match.is_unique_enabled:
        assert match.matched_repo_id is not None  # noqa: S101
        try:
            require_iar_repository_initialized(cwd_git_root)
        except IARRepositoryNotInitializedError:
            return _DefaultDaemonTarget(
                repo_id=None,
                error=(
                    f"Repository '{match.matched_repo_id}' is not initialized. "
                    "Run 'iar init' in the repository root, or use --all to target all enabled registry entries."
                ),
            )
        return _DefaultDaemonTarget(repo_id=match.matched_repo_id, error="")
    if match.is_disabled:
        assert match.disabled_repo_id is not None  # noqa: S101
        return _DefaultDaemonTarget(
            repo_id=None,
            error=(
                f"Repository '{match.disabled_repo_id}' is disabled. "
                "Use --repo-id to target it explicitly, or enable it in config.toml."
            ),
        )
    if match.is_ambiguous:
        candidates = ", ".join(repo_id for repo_id, _ in match.enabled_candidates)
        return _DefaultDaemonTarget(
            repo_id=None,
            error=(
                f"Current directory matches multiple enabled repositories: {candidates}. "
                "Use --repo-id to target one, or --all to target all."
            ),
        )
    return _DefaultDaemonTarget(
        repo_id=None,
        error=(
            "Current directory is not an enabled iAR registry target. "
            "Use --repo-id to target a registered repository, or --all to target all enabled registry entries."
        ),
    )


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


def _handle_not_initialized_error(exc: IARRepositoryNotInitializedError) -> int:
    """Print a friendly error and suggest running `iar init`."""
    error_console.print("[red]Repository is not initialized for iar.[/]")
    error_console.print(f"Expected local config: {exc.config_path}", soft_wrap=True)
    error_console.print("Run the following command from the repository root:")
    error_console.print("  iar init")
    return 1
