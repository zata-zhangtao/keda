"""``iar worktree *`` handlers.

Extracted from :mod:`backend.api.cli`'s monolithic ``_run_parsed_command``
dispatcher.
"""

from __future__ import annotations

from pathlib import Path

from backend.api.cli_helpers import (
    _ensure_gh_auth_or_prompt,
    _print_worktree_cleanup_result,
)
from backend.api.cli_parsed_context import ParsedCommandContext
from backend.api import cli as _cli
from backend.core.use_cases.worktree_cleanup import (
    WorktreeCleanupRequest,
    cleanup_iar_worktrees,
)
from backend.core.use_cases.worktree_env import copy_missing_env_files
from backend.engines.agent_runner.repository_local import (
    IARRepositoryNotInitializedError,
    detect_git_repository_root,
    require_iar_repository_initialized,
)
from backend.engines.agent_runner.worktree_cli import build_worktree_manager


def run_worktree_command(ctx: ParsedCommandContext) -> int:
    """``iar worktree <create|path|remove|cleanup>``."""
    try:
        repo_root_path = detect_git_repository_root(Path.cwd(), ctx.process_runner)
        require_iar_repository_initialized(repo_root_path, ctx.process_runner)
    except ValueError as exc:
        _cli.logger.error("iar worktree failed: %s", exc)
        return 1
    except IARRepositoryNotInitializedError as exc:
        from backend.api.cli_helpers import _handle_not_initialized_error

        return _handle_not_initialized_error(exc)
    manager = build_worktree_manager(repo_root_path, ctx.process_runner)
    if ctx.parsed.worktree_command == "create":
        created_worktree_path = manager.create(
            branch=ctx.parsed.branch, base_branch=ctx.parsed.base_branch
        )
        copied_env_paths = copy_missing_env_files(repo_root_path, created_worktree_path)
        if copied_env_paths:
            _cli.logger.info(
                "Copied %d missing env file(s) into worktree %s",
                len(copied_env_paths),
                created_worktree_path,
            )
        return 0
    if ctx.parsed.worktree_command == "path":
        print(str(manager.worktree_path(ctx.parsed.branch)))
        return 0
    if ctx.parsed.worktree_command == "remove":
        manager.remove(branch=ctx.parsed.branch)
        return 0
    if ctx.parsed.worktree_command == "cleanup":
        contexts = _cli.resolve_repository_targets(
            ctx.runner_settings,
            fallback_path=str(repo_root_path),
        )
        if len(contexts) != 1:
            _cli.logger.error("iar worktree cleanup requires exactly one repository.")
            return 1
        run_context = contexts[0]
        _ensure_gh_auth_or_prompt(run_context.repo_path, ctx.process_runner)
        github_client = _cli.create_github_client(run_context.repo_path, ctx.process_runner)
        cleanup_request = WorktreeCleanupRequest(
            repo_path=run_context.repo_path,
            remote=run_context.config.git.remote,
            base_branch=run_context.config.git.base_branch,
            dry_run=ctx.parsed.dry_run or not ctx.parsed.yes,
            force=ctx.parsed.force,
            managed_worktree_root_path=manager.worktree_root,
        )
        cleanup_result = cleanup_iar_worktrees(
            cleanup_request,
            github_client=github_client,
            process_runner=ctx.process_runner,
        )
        _print_worktree_cleanup_result(cleanup_result)
        return 1 if cleanup_result.failed_count else 0
    _cli.logger.error("iar worktree: unknown subcommand %r", ctx.parsed.worktree_command)
    return 1


__all__ = ["run_worktree_command"]
