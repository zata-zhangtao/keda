"""Tests for Agent Runner multi-repository configuration loading and merging."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import AppConfig, GitConfig, RunnerConfig
from backend.engines.agent_runner.factory import (
    build_app_config_from_settings,
    merge_repository_config,
    resolve_issue_from_prd_target,
    resolve_repository_targets,
)
from backend.infrastructure.config import settings as settings_module
from backend.infrastructure.config.settings import (
    AgentRunnerGeneratedContentSettings,
    AgentRunnerGeneratedContentTargetSettings,
    AgentRunnerGitSettings,
    AgentRunnerLabelSettings,
    AgentRunnerRepositorySettings,
    AgentRunnerRunnerSettings,
    AgentRunnerSettings,
)


def _make_settings(**kwargs) -> AgentRunnerSettings:
    return AgentRunnerSettings(**kwargs)


@pytest.fixture(autouse=True)
def isolate_agent_runner_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep repository target tests independent from the developer's config.toml."""
    original_loader = settings_module._load_toml_section_data

    def load_toml_section_without_agent_repositories(
        section_name: str,
    ) -> dict[str, object]:
        section_data = original_loader(section_name)
        if section_name != "agent_runner":
            return section_data
        isolated_section_data = dict(section_data)
        isolated_section_data.pop("repositories", None)
        return isolated_section_data

    monkeypatch.setattr(
        settings_module,
        "_load_toml_section_data",
        load_toml_section_without_agent_repositories,
    )


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


def test_merge_repository_config_overrides_labels() -> None:
    """Repository-level label overrides should map correctly to LabelConfig."""
    from backend.core.shared.models.agent_runner import LabelConfig

    global_config = AppConfig(labels=LabelConfig(ready="global/ready"))
    repo_settings = AgentRunnerRepositorySettings(
        path="/tmp/repo",
        labels=AgentRunnerLabelSettings(ready="repo/ready", codex="repo/codex"),
    )
    merged = merge_repository_config(global_config, repo_settings)
    assert merged.labels.ready == "repo/ready"
    assert merged.labels.agent_labels["codex"] == "repo/codex"
    assert merged.labels.agent_labels["claude"] == "agent/claude"


def test_merge_repository_config_inherits_label_agent_labels() -> None:
    """Unoverridden agent labels should inherit from global config."""
    from backend.core.shared.models.agent_runner import LabelConfig

    global_config = AppConfig(
        labels=LabelConfig(
            agent_labels={"codex": "global/codex", "claude": "global/claude"}
        )
    )
    repo_settings = AgentRunnerRepositorySettings(
        path="/tmp/repo",
        labels=AgentRunnerLabelSettings(claude="repo/claude"),
    )
    merged = merge_repository_config(global_config, repo_settings)
    assert merged.labels.agent_labels["codex"] == "global/codex"
    assert merged.labels.agent_labels["claude"] == "repo/claude"


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


def test_build_generated_content_config_from_settings() -> None:
    """_build_generated_content_config should map settings to core config correctly."""
    from backend.engines.agent_runner.factory import _build_generated_content_config

    gc_settings = AgentRunnerGeneratedContentSettings(
        enabled=True,
        max_input_chars=15000,
        issue_from_prd=AgentRunnerGeneratedContentTargetSettings(
            enabled=True,
            mode="agent",
            title_template="{prd_title}",
        ),
    )
    gc_config = _build_generated_content_config(gc_settings)
    assert gc_config.enabled is True
    assert gc_config.max_input_chars == 15000
    assert gc_config.issue_from_prd.enabled is True
    assert gc_config.issue_from_prd.mode == "agent"
    assert gc_config.issue_from_prd.title_template == "{prd_title}"


def test_merge_repository_config_overrides_generated_content() -> None:
    """Repository-level generated_content settings should override global defaults."""
    from backend.engines.agent_runner.factory import merge_repository_config

    global_config = AppConfig(
        generated_content=AgentRunnerGeneratedContentSettings(
            enabled=False,
            issue_from_prd=AgentRunnerGeneratedContentTargetSettings(
                enabled=False, mode="template"
            ),
        )
    )
    repo_settings = AgentRunnerRepositorySettings(
        path="/tmp/repo",
        generated_content=AgentRunnerGeneratedContentSettings(
            enabled=True,
            issue_from_prd=AgentRunnerGeneratedContentTargetSettings(
                enabled=True, mode="agent"
            ),
        ),
    )
    merged = merge_repository_config(global_config, repo_settings)
    assert merged.generated_content.enabled is True
    assert merged.generated_content.issue_from_prd.enabled is True
    assert merged.generated_content.issue_from_prd.mode == "agent"


def test_merge_repository_config_inherits_generated_content() -> None:
    """Unoverridden generated_content fields should inherit from global config."""
    from backend.engines.agent_runner.factory import merge_repository_config

    global_config = AppConfig(
        generated_content=AgentRunnerGeneratedContentSettings(
            enabled=True,
            max_input_chars=10000,
            issue_from_prd=AgentRunnerGeneratedContentTargetSettings(
                enabled=True, mode="template", title_template="global"
            ),
        )
    )
    repo_settings = AgentRunnerRepositorySettings(
        path="/tmp/repo",
        generated_content=AgentRunnerGeneratedContentSettings(
            issue_from_prd=AgentRunnerGeneratedContentTargetSettings(
                title_template="repo"
            ),
        ),
    )
    merged = merge_repository_config(global_config, repo_settings)
    assert merged.generated_content.enabled is True
    assert merged.generated_content.max_input_chars == 10000
    assert merged.generated_content.issue_from_prd.title_template == "repo"
    assert merged.generated_content.issue_from_prd.mode == "template"
