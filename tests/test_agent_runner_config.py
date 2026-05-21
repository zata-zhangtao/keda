"""Tests for Agent Runner multi-repository configuration loading and merging."""

from __future__ import annotations

from pathlib import Path

from backend.core.shared.models.agent_runner import AppConfig, GitConfig, RunnerConfig
from backend.engines.agent_runner.factory import (
    build_app_config_from_settings,
    merge_repository_config,
    resolve_issue_from_prd_target,
    resolve_repository_targets,
)
from backend.infrastructure.config.settings import (
    AgentRunnerGitSettings,
    AgentRunnerRepositorySettings,
    AgentRunnerRunnerSettings,
    AgentRunnerSettings,
)


def _make_settings(**kwargs) -> AgentRunnerSettings:
    return AgentRunnerSettings(**kwargs)


def test_build_app_config_from_settings_structure() -> None:
    """build_app_config_from_settings should return a fully populated AppConfig."""
    settings = AgentRunnerSettings()
    app_config = build_app_config_from_settings(settings)
    assert isinstance(app_config, AppConfig)
    assert app_config.git.base_branch is not None
    assert app_config.git.remote is not None
    assert app_config.runner.max_issues >= 1


def test_merge_repository_config_overrides_git() -> None:
    """Repository-level git settings should override global defaults."""
    global_config = AppConfig(git=GitConfig(base_branch="main", remote="origin"))
    repo_settings = AgentRunnerRepositorySettings(
        path="/tmp/repo",
        git=AgentRunnerGitSettings(base_branch="develop", remote="upstream"),
    )
    merged = merge_repository_config(global_config, repo_settings)
    assert merged.git.base_branch == "develop"
    assert merged.git.remote == "upstream"


def test_merge_repository_config_inherits_unset_fields() -> None:
    """Repository settings should inherit global values for unoverridden fields."""
    global_config = AppConfig(runner=RunnerConfig(max_issues=5, default_agent="claude"))
    repo_settings = AgentRunnerRepositorySettings(
        path="/tmp/repo",
        runner=AgentRunnerRunnerSettings(max_issues=3),
    )
    merged = merge_repository_config(global_config, repo_settings)
    assert merged.runner.max_issues == 3
    assert merged.runner.default_agent == "claude"


def test_resolve_repository_targets_ad_hoc_repo() -> None:
    """--repo should return a single ad-hoc context with global config."""
    settings = _make_settings()
    contexts = resolve_repository_targets(
        settings, repo_id=None, repo_path_override="/tmp/repo"
    )
    assert len(contexts) == 1
    assert contexts[0].repo_id == "ad-hoc"
    assert contexts[0].repo_path == Path("/tmp/repo").resolve()


def test_resolve_repository_targets_by_repo_id() -> None:
    """--repo-id should return a single configured repository context."""
    settings = _make_settings(
        repositories={
            "keda": AgentRunnerRepositorySettings(
                path="/Users/zata/code/keda",
                display_name="Keda",
                git=AgentRunnerGitSettings(base_branch="develop"),
            )
        }
    )
    contexts = resolve_repository_targets(settings, repo_id="keda")
    assert len(contexts) == 1
    assert contexts[0].repo_id == "keda"
    assert contexts[0].display_name == "Keda"
    assert contexts[0].config.git.base_branch == "develop"


def test_resolve_repository_targets_all_enabled() -> None:
    """No selector should return all enabled configured repositories."""
    settings = _make_settings(
        repositories={
            "keda": AgentRunnerRepositorySettings(
                path="/tmp/keda", enabled=True, display_name="Keda"
            ),
            "backend": AgentRunnerRepositorySettings(
                path="/tmp/backend", enabled=False, display_name="Backend"
            ),
        }
    )
    contexts = resolve_repository_targets(settings)
    assert len(contexts) == 1
    assert contexts[0].repo_id == "keda"


def test_resolve_repository_targets_fallback_when_empty() -> None:
    """No selector and no configured repos should fallback to cwd."""
    settings = _make_settings()
    contexts = resolve_repository_targets(settings, fallback_path=".")
    assert len(contexts) == 1
    assert contexts[0].repo_id == "fallback"


def test_resolve_repository_targets_mutual_exclusion() -> None:
    """--repo and --repo-id together should raise an error."""
    settings = _make_settings()
    try:
        resolve_repository_targets(
            settings, repo_id="keda", repo_path_override="/tmp/repo"
        )
        raise AssertionError("Expected ValueError")
    except ValueError as exc:
        assert "mutually exclusive" in str(exc)


def test_resolve_repository_targets_disabled_repo() -> None:
    """Selecting a disabled repo should raise an error."""
    settings = _make_settings(
        repositories={
            "keda": AgentRunnerRepositorySettings(path="/tmp/keda", enabled=False)
        }
    )
    try:
        resolve_repository_targets(settings, repo_id="keda")
        raise AssertionError("Expected ValueError")
    except ValueError as exc:
        assert "disabled" in str(exc)


def test_resolve_issue_from_prd_matches_cwd() -> None:
    """When cwd matches a configured repo path, merged config should apply."""
    cwd = Path("/tmp/keda")
    settings = _make_settings(
        repositories={
            "keda": AgentRunnerRepositorySettings(
                path="/tmp/keda",
                git=AgentRunnerGitSettings(base_branch="develop"),
            )
        }
    )
    context = resolve_issue_from_prd_target(settings, cwd=cwd)
    assert context.repo_id == "keda"
    assert context.config.git.base_branch == "develop"


def test_resolve_issue_from_prd_fallback() -> None:
    """When cwd does not match any configured repo, global config should apply."""
    cwd = Path("/tmp/other")
    settings = _make_settings(
        repositories={
            "keda": AgentRunnerRepositorySettings(path="/tmp/keda"),
        }
    )
    context = resolve_issue_from_prd_target(settings, cwd=cwd)
    assert context.repo_id == "fallback"
    assert context.config.git.base_branch == "main"
