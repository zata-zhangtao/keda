"""Tests for Agent Runner multi-repository configuration loading and merging."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import AppConfig, GitConfig, RunnerConfig
from backend.core.shared.models.agent_deliberation import DeliberationConfig
from backend.engines.agent_runner.factory import (
    build_app_config_from_settings,
    merge_repository_config,
    resolve_issue_from_prd_target,
    resolve_repository_targets,
)
from backend.engines.agent_runner.repository_local import (
    IARRepositoryNotInitializedError,
    require_iar_repository_initialized,
)
from backend.infrastructure.config import settings as settings_module
from backend.infrastructure.config.settings import (
    AgentRunnerDeliberationProfileSettings,
    AgentRunnerDeliberationSettings,
    AgentRunnerGeneratedContentSettings,
    AgentRunnerGeneratedContentTargetSettings,
    AgentRunnerGitSettings,
    AgentRunnerLabelSettings,
    AgentRunnerRepositorySettings,
    AgentRunnerRunnerSettings,
    AgentRunnerSettings,
)


def _run_git(repo_path: Path, *git_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *git_args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _init_git_repository(tmp_path: Path, name: str) -> Path:
    repo_path = tmp_path / name
    repo_path.mkdir()
    _run_git(repo_path, "init")
    _run_git(repo_path, "checkout", "-b", "main")
    return repo_path


def _write_local_iar_config(repo_path: Path, content: str) -> None:
    (repo_path / ".iar.toml").write_text(content, encoding="utf-8")


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
    # Registry repositories are loaded from a separate config path; keep those
    # isolated from the developer's ~/.iar/config.toml as well.
    monkeypatch.setattr(
        settings_module,
        "_load_registry_toml_section_data",
        lambda _section_name: {},
    )


def test_build_app_config_from_settings_structure() -> None:
    """build_app_config_from_settings should return a fully populated AppConfig."""
    settings = AgentRunnerSettings()
    app_config = build_app_config_from_settings(settings)
    assert isinstance(app_config, AppConfig)
    assert app_config.git.base_branch is not None
    assert app_config.git.remote is not None
    assert app_config.runner.max_issues >= 1


def test_agent_runner_daemon_settings_defaults() -> None:
    """Daemon polling intervals should default to 120 seconds."""
    settings = AgentRunnerSettings()
    assert settings.daemon.review_interval_seconds == 120
    assert settings.daemon.run_interval_seconds == 120


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


def test_runner_settings_escalation_ladder_defaults() -> None:
    """Escalation-ladder settings default to a conservative fallback chain."""
    settings = AgentRunnerRunnerSettings()
    assert settings.agent_fallback_order == ["claude", "kimi", "codex"]
    assert settings.max_agent_switches == 2
    assert settings.transient_retry_attempts == 2
    assert settings.transient_retry_delay_seconds == 10


def test_max_concurrent_issues_default_and_factory_mapping() -> None:
    """max_concurrent_issues defaults to 1 and maps into the domain RunnerConfig."""
    assert AgentRunnerRunnerSettings().max_concurrent_issues == 1
    assert RunnerConfig().max_concurrent_issues == 1

    # Build the domain config from settings; mutate the nested field directly to
    # avoid pydantic-settings sources (repo config.toml) shadowing an init kwarg.
    settings = AgentRunnerSettings()
    settings.runner.max_concurrent_issues = 4
    app_config = build_app_config_from_settings(settings)
    assert app_config.runner.max_concurrent_issues == 4


def test_merge_repository_config_inherits_max_concurrent_issues() -> None:
    """Repo override keeps global max_concurrent_issues when unset."""
    global_config = AppConfig(runner=RunnerConfig(max_concurrent_issues=6))
    repo_settings = AgentRunnerRepositorySettings(
        path="/tmp/repo",
        runner=AgentRunnerRunnerSettings(max_issues=2),
    )
    merged = merge_repository_config(global_config, repo_settings)
    assert merged.runner.max_concurrent_issues == 6


def test_iar_toml_renders_max_concurrent_issues_comment() -> None:
    """`iar init` output documents max_concurrent_issues above the field."""
    from backend.engines.agent_runner.repository_local import (
        _IAR_FIELD_COMMENTS,
        settings_to_toml_string,
    )
    from backend.infrastructure.config.settings import AgentRunnerLocalSettings

    settings = AgentRunnerLocalSettings(runner=AgentRunnerRunnerSettings())
    rendered = settings_to_toml_string(settings)

    assert "runner.max_concurrent_issues" in _IAR_FIELD_COMMENTS
    assert "max_concurrent_issues = 1" in rendered
    comment = _IAR_FIELD_COMMENTS["runner.max_concurrent_issues"]
    assert any(comment in line for line in rendered.splitlines())


def test_merge_repository_config_overrides_agent_fallback_order() -> None:
    """Repository-level fallback order overrides global runner config."""
    global_config = AppConfig(runner=RunnerConfig())
    repo_settings = AgentRunnerRepositorySettings(
        path="/tmp/repo",
        runner=AgentRunnerRunnerSettings(agent_fallback_order=["claude", "codex"]),
    )
    merged = merge_repository_config(global_config, repo_settings)
    assert list(merged.runner.agent_fallback_order) == ["claude", "codex"]
    # Unset escalation fields inherit defaults.
    assert merged.runner.max_agent_switches == 2


def test_merge_repository_config_overrides_labels() -> None:
    """Repository-level label overrides should map correctly to LabelConfig."""
    from backend.core.shared.models.agent_runner import LabelConfig

    global_config = AppConfig(labels=LabelConfig(ready="global/ready"))
    repo_settings = AgentRunnerRepositorySettings(
        path="/tmp/repo",
        labels=AgentRunnerLabelSettings(
            ready="repo/ready",
            codex="repo/codex",
            waiting="repo/waiting",
            group_prefix="repo-group/",
        ),
    )
    merged = merge_repository_config(global_config, repo_settings)
    assert merged.labels.ready == "repo/ready"
    assert merged.labels.agent_labels["codex"] == "repo/codex"
    assert merged.labels.agent_labels["claude"] == "agent/claude"
    assert merged.labels.waiting == "repo/waiting"
    assert merged.labels.group_prefix == "repo-group/"


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


def test_resolve_repository_targets_ad_hoc_repo(tmp_path: Path) -> None:
    """--repo should return a single context for the explicit Git repository."""
    repo_path = _init_git_repository(tmp_path, "repo")
    settings = _make_settings()
    contexts = resolve_repository_targets(
        settings, repo_id=None, repo_path_override=str(repo_path)
    )
    assert len(contexts) == 1
    assert contexts[0].repo_id == "repo"
    assert contexts[0].repo_path == repo_path.resolve()


def test_resolve_repository_targets_repo_path_loads_local_config(
    tmp_path: Path,
) -> None:
    """--repo should merge the target repository's .iar.toml."""
    repo_path = _init_git_repository(tmp_path, "target")
    _write_local_iar_config(
        repo_path,
        """
[agent_runner.repository]
id = "target-local"
display_name = "Target Local"

[agent_runner.git]
remote = "upstream"
base_branch = "develop"
""",
    )
    settings = _make_settings()
    contexts = resolve_repository_targets(settings, repo_path_override=str(repo_path))
    assert len(contexts) == 1
    assert contexts[0].repo_id == "target-local"
    assert contexts[0].display_name == "Target Local"
    assert contexts[0].config.git.remote == "upstream"
    assert contexts[0].config.git.base_branch == "develop"


def test_resolve_repository_targets_local_config_disables_validation(
    tmp_path: Path,
) -> None:
    """A repository-local [agent_runner.validation] override must reach AppConfig.

    Regression guard: the local-settings repack used to drop the validation
    section, so `enabled = false` in .iar.toml was silently ignored and the
    evidence gate still fired.
    """
    repo_path = _init_git_repository(tmp_path, "target")
    _write_local_iar_config(
        repo_path,
        """
[agent_runner.repository]
id = "target-local"

[agent_runner.validation]
enabled = false
""",
    )
    settings = _make_settings()
    contexts = resolve_repository_targets(settings, repo_path_override=str(repo_path))
    assert len(contexts) == 1
    assert contexts[0].config.validation.enabled is False


def test_resolve_repository_targets_by_repo_id(tmp_path: Path) -> None:
    """--repo-id should return a single configured repository context."""
    repo_path = _init_git_repository(tmp_path, "keda")
    settings = _make_settings(
        repositories={
            "keda": AgentRunnerRepositorySettings(
                path=str(repo_path),
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


def test_resolve_repository_targets_by_repo_id_merges_local_config(
    tmp_path: Path,
) -> None:
    """--repo-id should use registry path and merge repository-local overrides."""
    repo_path = _init_git_repository(tmp_path, "keda")
    _write_local_iar_config(
        repo_path,
        """
[agent_runner.repository]
id = "local-keda"

[agent_runner.git]
base_branch = "local-main"
""",
    )
    settings = _make_settings(
        repositories={
            "keda": AgentRunnerRepositorySettings(
                path=str(repo_path),
                display_name="Keda",
                git=AgentRunnerGitSettings(base_branch="develop", remote="zata"),
            )
        }
    )
    contexts = resolve_repository_targets(settings, repo_id="keda")
    assert len(contexts) == 1
    assert contexts[0].repo_id == "keda"
    assert contexts[0].display_name == "Keda"
    assert contexts[0].config.git.remote == "zata"
    assert contexts[0].config.git.base_branch == "local-main"


def test_resolve_repository_targets_all_enabled_requires_all_selector(
    tmp_path: Path,
) -> None:
    """--all should return all enabled configured repositories."""
    keda_path = _init_git_repository(tmp_path, "keda")
    backend_path = _init_git_repository(tmp_path, "backend")
    settings = _make_settings(
        repositories={
            "keda": AgentRunnerRepositorySettings(
                path=str(keda_path), enabled=True, display_name="Keda"
            ),
            "backend": AgentRunnerRepositorySettings(
                path=str(backend_path), enabled=False, display_name="Backend"
            ),
        }
    )
    contexts = resolve_repository_targets(settings, all_repositories=True)
    assert len(contexts) == 1
    assert contexts[0].repo_id == "keda"


def test_resolve_repository_targets_no_selector_uses_current_git_repo(
    tmp_path: Path,
) -> None:
    """No selector should use the current Git repo instead of configured repos."""
    current_repo_path = _init_git_repository(tmp_path, "current")
    other_repo_path = _init_git_repository(tmp_path, "other")
    _write_local_iar_config(
        current_repo_path,
        """
[agent_runner.repository]
id = "current-local"
display_name = "Current Local"

[agent_runner.git]
base_branch = "current-main"
""",
    )
    settings = _make_settings(
        repositories={
            "other": AgentRunnerRepositorySettings(
                path=str(other_repo_path),
                enabled=True,
                git=AgentRunnerGitSettings(base_branch="other-main"),
            ),
        }
    )
    contexts = resolve_repository_targets(
        settings, fallback_path=str(current_repo_path)
    )
    assert len(contexts) == 1
    assert contexts[0].repo_id == "current-local"
    assert contexts[0].repo_path == current_repo_path.resolve()
    assert contexts[0].config.git.base_branch == "current-main"


def test_resolve_repository_targets_rejects_disabled_local_config(
    tmp_path: Path,
) -> None:
    """Repository-local disabled config should produce an actionable error."""
    repo_path = _init_git_repository(tmp_path, "disabled")
    _write_local_iar_config(
        repo_path,
        """
[agent_runner.repository]
id = "disabled"
enabled = false
""",
    )
    settings = _make_settings()
    with pytest.raises(ValueError, match="Repository-local config"):
        resolve_repository_targets(settings, fallback_path=str(repo_path))


def test_resolve_repository_targets_fallback_when_empty(tmp_path: Path) -> None:
    """No selector and no configured repos should use the current Git repo."""
    repo_path = _init_git_repository(tmp_path, "fallback")
    settings = _make_settings()
    contexts = resolve_repository_targets(settings, fallback_path=str(repo_path))
    assert len(contexts) == 1
    assert contexts[0].repo_id == "fallback"
    assert contexts[0].repo_path == repo_path.resolve()


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


def test_resolve_repository_targets_disabled_repo(tmp_path: Path) -> None:
    """Selecting a disabled repo should raise an error."""
    repo_path = _init_git_repository(tmp_path, "keda")
    settings = _make_settings(
        repositories={
            "keda": AgentRunnerRepositorySettings(path=str(repo_path), enabled=False)
        }
    )
    try:
        resolve_repository_targets(settings, repo_id="keda")
        raise AssertionError("Expected ValueError")
    except ValueError as exc:
        assert "disabled" in str(exc)


def test_resolve_issue_from_prd_uses_local_cwd_config(tmp_path: Path) -> None:
    """issue create no-selector should use the current repository local config."""
    cwd = _init_git_repository(tmp_path, "keda")
    _write_local_iar_config(
        cwd,
        """
[agent_runner.repository]
id = "cwd-keda"

[agent_runner.git]
base_branch = "local-develop"
""",
    )
    settings = _make_settings(
        repositories={
            "keda": AgentRunnerRepositorySettings(
                path=str(cwd),
                git=AgentRunnerGitSettings(base_branch="develop"),
            )
        }
    )
    context = resolve_issue_from_prd_target(settings, cwd=cwd)
    assert context.repo_id == "cwd-keda"
    assert context.config.git.base_branch == "local-develop"


def test_resolve_issue_from_prd_fallback(tmp_path: Path) -> None:
    """When cwd has no local config, global config should apply to current repo."""
    cwd = _init_git_repository(tmp_path, "other")
    configured_repo = _init_git_repository(tmp_path, "keda")
    settings = _make_settings(
        repositories={
            "keda": AgentRunnerRepositorySettings(path=str(configured_repo)),
        }
    )
    context = resolve_issue_from_prd_target(settings, cwd=cwd)
    assert context.repo_id == "other"
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


def test_require_iar_repository_initialized_accepts_valid_config(
    tmp_path: Path,
) -> None:
    """A valid .iar.toml with non-empty repository.id should pass."""
    repo_path = _init_git_repository(tmp_path, "initialized")
    _write_local_iar_config(
        repo_path,
        """
[agent_runner.repository]
id = "initialized"
""",
    )
    require_iar_repository_initialized(repo_path)


def test_require_iar_repository_initialized_rejects_missing_config(
    tmp_path: Path,
) -> None:
    """Missing .iar.toml should raise IARRepositoryNotInitializedError."""
    repo_path = _init_git_repository(tmp_path, "missing")
    with pytest.raises(IARRepositoryNotInitializedError) as exc_info:
        require_iar_repository_initialized(repo_path)
    assert str(repo_path / ".iar.toml") in str(exc_info.value)


def test_require_iar_repository_initialized_rejects_empty_repo_id(
    tmp_path: Path,
) -> None:
    """An empty repository.id should raise IARRepositoryNotInitializedError."""
    repo_path = _init_git_repository(tmp_path, "empty-id")
    _write_local_iar_config(
        repo_path,
        """
[agent_runner.repository]
id = ""
""",
    )
    with pytest.raises(IARRepositoryNotInitializedError) as exc_info:
        require_iar_repository_initialized(repo_path)
    assert str(repo_path / ".iar.toml") in str(exc_info.value)


def test_require_iar_repository_initialized_rejects_invalid_toml(
    tmp_path: Path,
) -> None:
    """Invalid TOML should raise IARRepositoryNotInitializedError."""
    repo_path = _init_git_repository(tmp_path, "invalid")
    _write_local_iar_config(repo_path, "this is not valid toml")
    with pytest.raises(IARRepositoryNotInitializedError) as exc_info:
        require_iar_repository_initialized(repo_path)
    assert str(repo_path / ".iar.toml") in str(exc_info.value)


def test_require_iar_repository_initialized_rejects_missing_agent_runner_section(
    tmp_path: Path,
) -> None:
    """TOML without [agent_runner] should raise IARRepositoryNotInitializedError."""
    repo_path = _init_git_repository(tmp_path, "no-section")
    _write_local_iar_config(repo_path, '[other]\nkey = "value"\n')
    with pytest.raises(IARRepositoryNotInitializedError) as exc_info:
        require_iar_repository_initialized(repo_path)
    assert str(repo_path / ".iar.toml") in str(exc_info.value)


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


def test_build_app_config_maps_validation_language_and_structured_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation language and structured-evidence flags reach the core config."""
    from backend.infrastructure.config.settings import AgentRunnerValidationSettings

    original_loader = settings_module._load_toml_section_data

    def load_empty_agent_runner(section_name: str) -> dict[str, object]:
        if section_name == "agent_runner":
            return {}
        return original_loader(section_name)

    monkeypatch.setattr(
        settings_module, "_load_toml_section_data", load_empty_agent_runner
    )

    settings = AgentRunnerSettings(
        validation=AgentRunnerValidationSettings(
            language="en-US", structured_evidence=False
        )
    )
    app_config = build_app_config_from_settings(settings)
    assert app_config.validation.language == "en-US"
    assert app_config.validation.structured_evidence is False


def test_merge_repository_config_overrides_validation_language(
    tmp_path: Path,
) -> None:
    """Repository-local .iar.toml can override validation language."""
    repo_path = _init_git_repository(tmp_path, "target")
    _write_local_iar_config(
        repo_path,
        """
[agent_runner.repository]
id = "target-local"

[agent_runner.validation]
language = "en-US"
structured_evidence = false
""",
    )
    settings = _make_settings()
    contexts = resolve_repository_targets(settings, repo_path_override=str(repo_path))
    assert len(contexts) == 1
    assert contexts[0].config.validation.language == "en-US"
    assert contexts[0].config.validation.structured_evidence is False


def test_build_app_config_from_settings_maps_deliberation() -> None:
    """build_app_config_from_settings should expose default deliberation profiles."""
    settings = AgentRunnerSettings()
    app_config = build_app_config_from_settings(settings)
    assert isinstance(app_config.deliberation, DeliberationConfig)
    profile_ids = {p.profile_id for p in app_config.deliberation.profiles}
    assert profile_ids == {"architect", "skeptic", "implementer"}


def test_merge_repository_config_overrides_deliberation() -> None:
    """Repository-level deliberation overrides should merge with global defaults."""
    global_config = AppConfig(
        deliberation=DeliberationConfig(default_rounds=2, default_synthesizer="claude")
    )
    repo_settings = AgentRunnerRepositorySettings(
        path="/tmp/repo",
        deliberation=AgentRunnerDeliberationSettings(
            default_rounds=5,
            profiles={
                **AgentRunnerDeliberationSettings().profiles,
                "reviewer": AgentRunnerDeliberationProfileSettings(
                    agent="claude",
                    role="reviewer",
                    behavior_prompt="Review the output.",
                ),
            },
        ),
    )
    merged = merge_repository_config(global_config, repo_settings)
    assert merged.deliberation.default_rounds == 5
    assert merged.deliberation.default_synthesizer == "claude"
    merged_profile_ids = {p.profile_id for p in merged.deliberation.profiles}
    assert merged_profile_ids == {"architect", "skeptic", "implementer", "reviewer"}


def test_merge_repository_config_inherits_deliberation_profiles() -> None:
    """Unoverridden deliberation profiles should inherit from global config."""
    from dataclasses import replace

    custom_profile = replace(DeliberationConfig().profiles[0], agent="custom-claude")
    global_config = AppConfig(
        deliberation=DeliberationConfig(profiles=(custom_profile,))
    )
    repo_settings = AgentRunnerRepositorySettings(
        path="/tmp/repo",
        deliberation=AgentRunnerDeliberationSettings(default_rounds=3),
    )
    merged = merge_repository_config(global_config, repo_settings)
    assert merged.deliberation.default_rounds == 3
    assert merged.deliberation.profiles[0].agent == "custom-claude"


def test_resolve_repository_targets_merges_local_deliberation(
    tmp_path: Path,
) -> None:
    """Repository-local .iar.toml can override deliberation settings."""
    repo_path = _init_git_repository(tmp_path, "target")
    _write_local_iar_config(
        repo_path,
        """
[agent_runner.repository]
id = "target-local"

[agent_runner.deliberation]
default_rounds = 7
default_synthesizer = "kimi"
""",
    )
    settings = _make_settings()
    contexts = resolve_repository_targets(settings, repo_path_override=str(repo_path))
    assert len(contexts) == 1
    assert contexts[0].config.deliberation.default_rounds == 7
    assert contexts[0].config.deliberation.default_synthesizer == "kimi"
    # Profiles fall back to global defaults when not overridden.
    profile_ids = {p.profile_id for p in contexts[0].config.deliberation.profiles}
    assert "architect" in profile_ids
