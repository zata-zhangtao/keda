"""Agent Runner infrastructure adapter and factory.

This module bridges the ``core/`` business layer with ``infrastructure/``
implementations by instantiating concrete clients/runners and converting
pydantic-settings configuration into the frozen dataclasses expected by use
cases.
"""

from __future__ import annotations

from pathlib import Path

from backend.core.shared.models.agent_runner import (
    AppConfig,
    GitConfig,
    LabelConfig,
    RunnerConfig,
    SafetyConfig,
    WorktreeConfig,
)
from backend.infrastructure.config.settings import config
from backend.infrastructure.github_client import GitHubCliClient
from backend.infrastructure.process_runner import SubprocessRunner


def build_app_config() -> AppConfig:
    """Convert pydantic-settings ``AgentRunnerSettings`` to frozen ``AppConfig``."""
    agent_runner_settings = config.agent_runner
    label_settings = agent_runner_settings.labels
    git_settings = agent_runner_settings.git
    worktree_settings = agent_runner_settings.worktree
    runner_settings = agent_runner_settings.runner
    safety_settings = agent_runner_settings.safety

    return AppConfig(
        labels=LabelConfig(
            ready=label_settings.ready,
            running=label_settings.running,
            review=label_settings.review,
            failed=label_settings.failed,
            blocked=label_settings.blocked,
            codex=label_settings.codex,
            claude=label_settings.claude,
        ),
        git=GitConfig(
            remote=git_settings.remote,
            base_branch=git_settings.base_branch,
        ),
        worktree=WorktreeConfig(
            create_command=worktree_settings.create_command,
            reuse_command=worktree_settings.reuse_command,
            path_command=worktree_settings.path_command,
        ),
        runner=RunnerConfig(
            max_issues=runner_settings.max_issues,
            default_agent=runner_settings.default_agent,
            verification_commands=tuple(runner_settings.verification_commands),
        ),
        safety=SafetyConfig(
            auto_merge=safety_settings.auto_merge,
            forbidden_path_patterns=tuple(safety_settings.forbidden_path_patterns),
        ),
    )


def create_process_runner() -> SubprocessRunner:
    """Create a new subprocess runner instance."""
    return SubprocessRunner()


def create_github_client(
    repo_path: Path, process_runner: SubprocessRunner | None = None
) -> GitHubCliClient:
    """Create a new GitHub CLI client instance."""
    return GitHubCliClient(repo_path, process_runner)
