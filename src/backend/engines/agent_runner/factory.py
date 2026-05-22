"""Agent Runner infrastructure adapter and factory.

This module bridges the ``core/`` business layer with ``infrastructure/``
implementations by instantiating concrete clients/runners and converting
pydantic-settings configuration into the frozen dataclasses expected by use
cases.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from pydantic import BaseModel

from backend.core.shared.models.agent_runner import (
    AppConfig,
    GitConfig,
    LabelConfig,
    RepositoryRunContext,
    RunnerConfig,
    SafetyConfig,
    WorktreeConfig,
)
from backend.infrastructure.config.settings import (
    AgentRunnerLabelSettings,
    AgentRunnerRepositorySettings,
    AgentRunnerSettings,
    config,
)
from backend.infrastructure.github_client import GitHubCliClient
from backend.infrastructure.process_runner import SubprocessRunner


def build_app_config_from_settings(
    agent_runner_settings: AgentRunnerSettings,
) -> AppConfig:
    """Convert pydantic-settings ``AgentRunnerSettings`` to frozen ``AppConfig``."""
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
            agent_labels=label_settings.agent_labels,
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
            max_recovery_attempts=runner_settings.max_recovery_attempts,
            recovery_retry_delay_seconds=runner_settings.recovery_retry_delay_seconds,
            verification_commands=tuple(runner_settings.verification_commands),
        ),
        safety=SafetyConfig(
            auto_merge=safety_settings.auto_merge,
            forbidden_path_patterns=tuple(safety_settings.forbidden_path_patterns),
        ),
    )


def build_app_config() -> AppConfig:
    """Convert global pydantic-settings ``AgentRunnerSettings`` to frozen ``AppConfig``."""
    return build_app_config_from_settings(config.agent_runner)


def get_agent_runner_settings() -> AgentRunnerSettings:
    """Return the global ``AgentRunnerSettings`` instance."""
    return config.agent_runner


def get_agent_runner_status_data() -> dict:
    """Build the status response dict for the FastAPI status endpoint.

    Returns:
        A dictionary with ``daemon_mode``, ``config`` (global runner settings),
        and ``repositories`` (list of per-repository summaries).
    """
    agent_runner_settings = config.agent_runner
    app_config = build_app_config_from_settings(agent_runner_settings)
    repositories = []
    for repo_id in agent_runner_settings.repositories:
        repo = agent_runner_settings.repositories[repo_id]
        base_branch = app_config.git.base_branch
        remote = app_config.git.remote
        if repo.git is not None:
            base_branch = repo.git.base_branch or base_branch
            remote = repo.git.remote or remote
        repositories.append(
            {
                "repo_id": repo_id,
                "display_name": repo.display_name or repo_id,
                "enabled": repo.enabled,
                "base_branch": base_branch,
                "remote": remote,
            }
        )
    return {
        "daemon_mode": False,
        "config": {
            "max_issues": app_config.runner.max_issues,
            "default_agent": app_config.runner.default_agent,
            "max_recovery_attempts": app_config.runner.max_recovery_attempts,
            "recovery_retry_delay_seconds": (
                app_config.runner.recovery_retry_delay_seconds
            ),
            "ready_label": app_config.labels.ready,
            "running_label": app_config.labels.running,
            "review_label": app_config.labels.review,
            "failed_label": app_config.labels.failed,
            "base_branch": app_config.git.base_branch,
            "remote": app_config.git.remote,
            "auto_merge": app_config.safety.auto_merge,
            "forbidden_path_patterns": list(app_config.safety.forbidden_path_patterns),
        },
        "repositories": repositories,
    }


def _model_to_dict(model: object) -> dict:
    """Convert a pydantic BaseModel or dataclass to a plain dict."""
    if isinstance(model, BaseModel):
        return model.model_dump()
    return dataclasses.asdict(model)


def _pydantic_override_dict(override_model: BaseModel) -> dict:
    """Return only explicitly-set fields from a pydantic override model."""
    return {
        k: v
        for k, v in override_model.model_dump().items()
        if k in override_model.model_fields_set
    }


def _merge_optional_model(base_model, override_model):
    """Merge a pydantic override model into a base model, returning a new base instance."""
    if override_model is None:
        return base_model
    merged_data = {
        **_model_to_dict(base_model),
        **_pydantic_override_dict(override_model),
    }
    return type(base_model)(**merged_data)


def _merge_label_config(
    base_config: LabelConfig, override: AgentRunnerLabelSettings | None
) -> LabelConfig:
    """Merge repository-specific label overrides into a base ``LabelConfig``."""
    if override is None:
        return base_config
    override_data = _pydantic_override_dict(override)
    agent_labels = dict(base_config.agent_labels)
    for agent_key in ("codex", "claude", "kimi"):
        if agent_key in override_data:
            agent_labels[agent_key] = override_data[agent_key]
    return LabelConfig(
        ready=override_data.get("ready", base_config.ready),
        running=override_data.get("running", base_config.running),
        review=override_data.get("review", base_config.review),
        failed=override_data.get("failed", base_config.failed),
        blocked=override_data.get("blocked", base_config.blocked),
        agent_labels=agent_labels,
    )


def merge_repository_config(
    global_config: AppConfig, repo_settings: AgentRunnerRepositorySettings
) -> AppConfig:
    """Merge repository-specific overrides into a global ``AppConfig``.

    Args:
        global_config: The global application configuration.
        repo_settings: Repository-specific override settings.

    Returns:
        A new ``AppConfig`` with per-repository overrides applied.
    """
    labels = _merge_label_config(global_config.labels, repo_settings.labels)
    git = _merge_optional_model(global_config.git, repo_settings.git)
    worktree = _merge_optional_model(global_config.worktree, repo_settings.worktree)
    runner = _merge_optional_model(global_config.runner, repo_settings.runner)
    safety = _merge_optional_model(global_config.safety, repo_settings.safety)
    return AppConfig(
        labels=labels, git=git, worktree=worktree, runner=runner, safety=safety
    )


def resolve_repository_targets(
    settings: AgentRunnerSettings,
    *,
    repo_id: str | None = None,
    repo_path_override: str | None = None,
    fallback_path: str = ".",
) -> list[RepositoryRunContext]:
    """Resolve target repositories for consumer commands.

    Args:
        settings: Agent runner settings.
        repo_id: Optional configured repository ID selector.
        repo_path_override: Optional ad-hoc repository path selector.
        fallback_path: Path to use when no repositories are configured.

    Returns:
        List of repository run contexts.

    Raises:
        ValueError: If both ``repo_id`` and ``repo_path_override`` are provided,
            or if ``repo_id`` does not exist or is disabled.
    """
    if repo_path_override is not None and repo_id is not None:
        raise ValueError("--repo and --repo-id are mutually exclusive.")

    global_config = build_app_config_from_settings(settings)

    if repo_path_override is not None:
        path = Path(repo_path_override).resolve()
        return [
            RepositoryRunContext(
                repo_id="ad-hoc",
                display_name=str(path),
                repo_path=path,
                config=global_config,
            )
        ]

    if repo_id is not None:
        if repo_id not in settings.repositories:
            raise ValueError(f"Repository '{repo_id}' not found in config.")
        repo_settings = settings.repositories[repo_id]
        if not repo_settings.enabled:
            raise ValueError(f"Repository '{repo_id}' is disabled.")
        return [
            RepositoryRunContext(
                repo_id=repo_id,
                display_name=repo_settings.display_name or repo_id,
                repo_path=Path(repo_settings.path).resolve(),
                config=merge_repository_config(global_config, repo_settings),
            )
        ]

    enabled_repos = {
        rid: rcfg for rid, rcfg in settings.repositories.items() if rcfg.enabled
    }
    if enabled_repos:
        contexts: list[RepositoryRunContext] = []
        for rid, repo_settings in enabled_repos.items():
            contexts.append(
                RepositoryRunContext(
                    repo_id=rid,
                    display_name=repo_settings.display_name or rid,
                    repo_path=Path(repo_settings.path).resolve(),
                    config=merge_repository_config(global_config, repo_settings),
                )
            )
        return contexts

    path = Path(fallback_path).resolve()
    return [
        RepositoryRunContext(
            repo_id="fallback",
            display_name=str(path),
            repo_path=path,
            config=global_config,
        )
    ]


def resolve_issue_from_prd_target(
    settings: AgentRunnerSettings,
    *,
    repo_id: str | None = None,
    repo_path_override: str | None = None,
    cwd: Path,
) -> RepositoryRunContext:
    """Resolve the single target repository for ``issue-from-prd``.

    Defaults to the current working directory. If the current directory matches a
    configured repository path, that repository's merged config is applied.

    Args:
        settings: Agent runner settings.
        repo_id: Optional configured repository ID selector.
        repo_path_override: Optional ad-hoc repository path selector.
        cwd: Current working directory.

    Returns:
        A single repository run context.

    Raises:
        ValueError: If both ``repo_id`` and ``repo_path_override`` are provided,
            or if ``repo_id`` does not exist or is disabled.
    """
    if repo_path_override is not None and repo_id is not None:
        raise ValueError("--repo and --repo-id are mutually exclusive.")

    global_config = build_app_config_from_settings(settings)

    if repo_path_override is not None:
        path = Path(repo_path_override).resolve()
        return RepositoryRunContext(
            repo_id="ad-hoc",
            display_name=str(path),
            repo_path=path,
            config=global_config,
        )

    if repo_id is not None:
        if repo_id not in settings.repositories:
            raise ValueError(f"Repository '{repo_id}' not found in config.")
        repo_settings = settings.repositories[repo_id]
        if not repo_settings.enabled:
            raise ValueError(f"Repository '{repo_id}' is disabled.")
        return RepositoryRunContext(
            repo_id=repo_id,
            display_name=repo_settings.display_name or repo_id,
            repo_path=Path(repo_settings.path).resolve(),
            config=merge_repository_config(global_config, repo_settings),
        )

    cwd_resolved = cwd.resolve()
    for rid, repo_settings in settings.repositories.items():
        if not repo_settings.enabled:
            continue
        if Path(repo_settings.path).resolve() == cwd_resolved:
            return RepositoryRunContext(
                repo_id=rid,
                display_name=repo_settings.display_name or rid,
                repo_path=cwd_resolved,
                config=merge_repository_config(global_config, repo_settings),
            )

    return RepositoryRunContext(
        repo_id="fallback",
        display_name=str(cwd_resolved),
        repo_path=cwd_resolved,
        config=global_config,
    )


def create_process_runner() -> SubprocessRunner:
    """Create a new subprocess runner instance."""
    return SubprocessRunner()


def create_github_client(
    repo_path: Path, process_runner: SubprocessRunner | None = None
) -> GitHubCliClient:
    """Create a new GitHub CLI client instance."""
    return GitHubCliClient(repo_path, process_runner)
