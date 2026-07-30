"""Shared builders and fake-runner helpers for agent runner tests.

Extracted from the historical ``tests/test_run_agent.py`` monolith so the
per-module agent runner test files can reuse one definition instead of
copying fixtures around.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PostPrSupervisorConfig,
    PrePrReviewConfig,
    RunnerConfig,
    WorktreeConfig,
)
from backend.infrastructure.process_runner import CommandFailedError


def make_ready_issue() -> IssueSummary:
    """Return the canonical ``agent/ready`` Issue #123 used by runner tests."""
    return IssueSummary(
        number=123,
        title="Example",
        url="https://github.com/example/repo/issues/123",
        body="Example body",
        labels=("agent/ready", "agent/codex"),
    )


def make_prd_issue(
    prd_path: str = "tasks/pending/example.md",
) -> IssueSummary:
    """Return a ready Issue whose body carries a backtick-quoted PRD anchor."""
    return IssueSummary(
        number=123,
        title="Example",
        url="https://github.com/example/repo/issues/123",
        body=f"PRD path: `{prd_path}`",
        labels=("agent/ready", "agent/codex"),
    )


def config_with_review_disabled(
    worktree_path: Path | None = None,
    *verification_commands: str,
    max_recovery_attempts: int = 2,
    recovery_retry_delay_seconds: int = 0,
    agent_fallback_order: tuple[str, ...] = (),
) -> AppConfig:
    """Return a config with pre-PR review and post-PR supervisor disabled.

    Cross-agent fallback is disabled by default so that tests of the single-agent
    recovery loop are not affected by the global default fallback chain.
    """
    commands = verification_commands or ("just test",)
    worktree_cfg = (
        WorktreeConfig(path_command=f"echo {worktree_path}") if worktree_path else WorktreeConfig()
    )
    return AppConfig(
        runner=RunnerConfig(
            max_recovery_attempts=max_recovery_attempts,
            recovery_retry_delay_seconds=recovery_retry_delay_seconds,
            verification_commands=commands,
            agent_fallback_order=agent_fallback_order,
        ),
        worktree=worktree_cfg,
        pre_pr_review=PrePrReviewConfig(enabled=False),
        post_pr_supervisor=PostPrSupervisorConfig(enabled=False),
    )


def worktree_path_response(
    worktree_path: Path,
) -> tuple[tuple[str, ...], CommandResult]:
    """Return the ``worktree.path_command`` argv and its canned stdout result."""
    command = ("echo", str(worktree_path))
    return command, CommandResult(
        command=command,
        return_code=0,
        stdout=f"{worktree_path}\n",
        stderr="",
    )


def git_remote_command() -> tuple[str, ...]:
    """Return the argv the runner uses to probe configured git remotes."""
    return ("git", "remote")


def git_remote_result(*remote_names: str) -> CommandResult:
    """Return a ``git remote`` result listing the given remote names."""
    command = git_remote_command()
    return CommandResult(
        command=command,
        return_code=0,
        stdout="".join(f"{remote_name}\n" for remote_name in remote_names),
        stderr="",
    )


def write_commit_request(worktree_path: Path, commit_message: str) -> None:
    """Write the agent's ``.agent-runner/commit-request.json`` proxy request."""
    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        f'{{"commit_message": "{commit_message}"}}\n',
        encoding="utf-8",
    )


def write_complete_prd(worktree_path: Path, relative_path: str = "tasks/example.md") -> None:
    """Write a PRD whose Acceptance Checklist is fully checked."""
    prd_path = worktree_path / relative_path
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        "\n".join(
            [
                "# PRD: Example",
                "",
                "## 7. Acceptance Checklist",
                "",
                "- [x] item 1",
                "- [x] item 2",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_incomplete_prd(worktree_path: Path, relative_path: str = "tasks/example.md") -> None:
    """Write a PRD whose Acceptance Checklist still has an unchecked item."""
    prd_path = worktree_path / relative_path
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        "\n".join(
            [
                "# PRD: Example",
                "",
                "## 7. Acceptance Checklist",
                "",
                "- [x] item 1",
                "- [ ] item 2",
                "",
            ]
        ),
        encoding="utf-8",
    )


def is_bash_wrapped_verification_call(
    command: Sequence[str], expected_inner: tuple[str, ...]
) -> bool:
    """Match either the legacy flat argv or the ``bash -lc <cmd>`` wrap.

    ``run_verification`` wraps each command in ``bash -lc`` so that shell
    metacharacters (command substitution, globs, pipes, env var
    interpolation) are honored. Custom ``FakeProcessRunner`` subclasses in
    the agent runner tests register responses keyed on the inner command
    tuple (e.g. ``("just", "test")``); they need a stable way to recognize
    the wrap so that pre- and post-wrap test assertions keep matching.
    """
    if tuple(command) == expected_inner:
        return True
    if len(command) == 3 and command[0] == "bash" and command[1] == "-lc":
        import shlex as _shlex

        try:
            return tuple(_shlex.split(command[2])) == expected_inner
        except ValueError:
            return False
    return False


def init_bare_git_repo(path: Path) -> Path:
    """Create a bare repository at ``path`` to act as a test remote."""
    subprocess.run(["git", "init", "--bare", str(path)], check=True, capture_output=True)
    return path


def init_git_repo(path: Path) -> Path:
    """Create a repository at ``path`` on ``main`` with a deterministic identity."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "branch", "-M", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
    )
    return path


def create_commit(path: Path, message: str) -> str:
    """Commit staged changes (empty commits allowed) and return the new HEAD sha."""
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", message],
        check=True,
        capture_output=True,
    )
    sha_result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return sha_result.stdout.strip()


def transient_command_error() -> CommandFailedError:
    """Return an agent failure whose output matches the transient-retry predicate."""
    return CommandFailedError(
        1,
        ["claude", "--dangerously-skip-permissions", "-p", "PROMPT"],
        output=("[agent error] API Error: The socket connection was closed unexpectedly."),
        stderr="",
    )
