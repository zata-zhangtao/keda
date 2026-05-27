"""Git utilities and verification for the agent runner."""

from __future__ import annotations

import shlex
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner
from backend.core.shared.models.agent_runner import AppConfig, CommandResult

__all__ = [
    "get_current_branch",
    "get_head_sha",
    "has_changes",
    "list_changed_paths",
    "list_git_remotes",
    "run_verification",
]


def get_head_sha(worktree_path: Path, process_runner: IProcessRunner) -> str:
    """Return the full SHA of the current HEAD commit."""
    result = process_runner.run(["git", "rev-parse", "HEAD"], cwd=worktree_path)
    return result.stdout.strip()


def get_current_branch(worktree_path: Path, process_runner: IProcessRunner) -> str:
    """Return the current branch name for a worktree."""
    result = process_runner.run(["git", "branch", "--show-current"], cwd=worktree_path)
    return result.stdout.strip()


def run_verification(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> list[CommandResult]:
    """Run configured verification commands.

    验证命令按配置顺序串行执行，第一个失败即短路停止，
    避免在已知失败的情况下继续执行后续耗时命令。
    """
    verification_results: list[CommandResult] = []
    for command in config.runner.verification_commands:
        result = process_runner.run(
            shlex.split(command),
            cwd=worktree_path,
            check=False,
        )
        verification_results.append(result)
        # 短路：第一个验证失败就停止，节省后续验证时间
        if result.return_code != 0:
            break
    return verification_results


def has_changes(worktree_path: Path, process_runner: IProcessRunner) -> bool:
    """Return whether the worktree has uncommitted changes."""
    result = process_runner.run(["git", "status", "--porcelain"], cwd=worktree_path)
    return bool(result.stdout.strip())


def list_changed_paths(
    worktree_path: Path, process_runner: IProcessRunner
) -> list[str]:
    """List changed paths in a worktree."""
    status_result = process_runner.run(
        ["git", "status", "--porcelain"], cwd=worktree_path
    )
    changed_paths: list[str] = []
    for status_line in status_result.stdout.splitlines():
        if not status_line:
            continue
        raw_path_text = status_line[3:]
        if " -> " in raw_path_text:
            changed_paths.extend(raw_path_text.split(" -> ", maxsplit=1))
        else:
            changed_paths.append(raw_path_text)
    return changed_paths


def list_git_remotes(worktree_path: Path, process_runner: IProcessRunner) -> list[str]:
    """Return configured Git remote names for the worktree."""
    remote_result = process_runner.run(["git", "remote"], cwd=worktree_path)
    remote_names = []
    for remote_line in remote_result.stdout.splitlines():
        remote_name = remote_line.strip()
        if remote_name and remote_name not in remote_names:
            remote_names.append(remote_name)
    return remote_names
