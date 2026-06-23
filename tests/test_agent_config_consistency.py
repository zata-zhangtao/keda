"""Ensure agent labels, settings, and command builders stay in sync."""

from __future__ import annotations

from pathlib import Path

from backend.core.shared.models.agent_runner import (
    AppConfig as CoreAppConfig,
    LabelConfig as CoreLabelConfig,
    PostPrSupervisorConfig,
    PrePrReviewConfig,
)
from backend.core.use_cases.run_agent_once import _AGENT_COMMAND_BUILDERS
from backend.engines.agent_runner.factory import build_app_config
from backend.infrastructure.config import settings as settings_module
from backend.infrastructure.config.settings import (
    AgentRunnerDeliberationSettings,
    AgentRunnerLabelSettings,
    AgentRunnerPostPrSupervisorSettings,
    AgentRunnerPrePrReviewSettings,
    AgentRunnerSettings,
)
from backend.infrastructure.github_client import LabelConfig as InfraLabelConfig

import pytest


def test_agent_runner_reads_root_config_toml() -> None:
    """Agent runner settings should load the repository root config.toml."""
    repository_root = Path(__file__).resolve().parents[1]
    app_config = build_app_config()

    assert settings_module._PROJECT_ROOT_PATH == repository_root
    assert settings_module._find_config_toml() == repository_root / "config.toml"
    assert app_config.runner.default_agent == "claude"
    assert app_config.runner.recovery_retry_delay_seconds == 30


def test_settings_and_core_agent_labels_are_identical() -> None:
    """AgentRunnerLabelSettings must aggregate the same keys as Core LabelConfig."""
    assert AgentRunnerLabelSettings().agent_labels == CoreLabelConfig().agent_labels


def test_infra_label_config_matches_core_label_config() -> None:
    """github_client.py LabelConfig must stay in sync with core LabelConfig."""
    assert InfraLabelConfig().agent_labels == CoreLabelConfig().agent_labels


def test_every_non_default_agent_has_a_command_builder() -> None:
    """Every agent in LabelConfig (except the codex fallback) must be runnable."""
    core_labels = CoreLabelConfig().agent_labels
    supported = set(_AGENT_COMMAND_BUILDERS) | {"codex"}
    assert (
        set(core_labels) <= supported
    ), f"Missing command builders for {set(core_labels) - supported}"


def test_label_config_includes_supervising() -> None:
    """All label configs must include the supervising label."""
    assert CoreLabelConfig().supervising == "agent/supervising"
    assert AgentRunnerLabelSettings().supervising == "agent/supervising"
    assert InfraLabelConfig().supervising == "agent/supervising"


def test_label_config_includes_waiting() -> None:
    """All label configs must include the waiting label."""
    assert CoreLabelConfig().waiting == "agent/waiting"
    assert AgentRunnerLabelSettings().waiting == "agent/waiting"
    assert InfraLabelConfig().waiting == "agent/waiting"


def test_label_config_includes_group_prefix() -> None:
    """All label configs must include the group prefix."""
    assert CoreLabelConfig().group_prefix == "task-group/"
    assert AgentRunnerLabelSettings().group_prefix == "task-group/"
    assert InfraLabelConfig().group_prefix == "task-group/"


def test_factory_build_app_config_maps_waiting_and_group_prefix() -> None:
    """Factory must map waiting and group_prefix labels through to AppConfig."""
    from backend.engines.agent_runner.factory import build_app_config

    app_config = build_app_config()
    assert app_config.labels.waiting == "agent/waiting"
    assert app_config.labels.group_prefix == "task-group/"


def test_app_config_has_review_and_supervisor_settings() -> None:
    """AppConfig must aggregate pre-PR review and post-PR supervisor configs."""
    app_config = CoreAppConfig()
    assert isinstance(app_config.pre_pr_review, PrePrReviewConfig)
    assert isinstance(app_config.post_pr_supervisor, PostPrSupervisorConfig)


def test_settings_review_and_supervisor_match_core() -> None:
    """Infrastructure settings must map to core review/supervisor configs."""
    pre_push = AgentRunnerPrePrReviewSettings()
    post_sup = AgentRunnerPostPrSupervisorSettings()
    core_pre = PrePrReviewConfig()
    core_post = PostPrSupervisorConfig()

    assert pre_push.enabled == core_pre.enabled
    assert pre_push.review_agent == core_pre.review_agent
    assert pre_push.allow_same_agent == core_pre.allow_same_agent
    assert pre_push.max_attempts == core_pre.max_attempts
    assert pre_push.timeout_seconds == core_pre.timeout_seconds
    assert (
        pre_push.commit_request_reminder_attempts
        == core_pre.commit_request_reminder_attempts
    )
    assert list(pre_push.review_prompt_template) == list(
        core_pre.review_prompt_template
    )

    assert post_sup.enabled == core_post.enabled
    assert post_sup.supervisor_agent == core_post.supervisor_agent
    assert post_sup.max_repair_attempts == core_post.max_repair_attempts


def test_pre_pr_review_prompt_template_normalizes_string() -> None:
    """Settings layer must coerce a string override into a list."""
    from backend.infrastructure.config.settings import (
        AgentRunnerPrePrReviewSettings,
    )

    settings = AgentRunnerPrePrReviewSettings(review_prompt_template="just one line")
    assert settings.review_prompt_template == ["just one line"]

    settings_empty = AgentRunnerPrePrReviewSettings(review_prompt_template="")
    assert settings_empty.review_prompt_template == []


def test_factory_build_app_config_maps_supervising() -> None:
    """Factory must map the supervising label through to AppConfig."""
    app_config = build_app_config()
    assert app_config.labels.supervising == "agent/supervising"
    assert app_config.pre_pr_review.enabled is True
    assert app_config.pre_pr_review.timeout_seconds == 1800
    assert app_config.post_pr_supervisor.enabled is True


def test_deliberation_profiles_reference_runnable_agents() -> None:
    """Default deliberation profiles must reference agents with command builders."""

    deliberation = AgentRunnerDeliberationSettings()
    supported = set(_AGENT_COMMAND_BUILDERS) | {"codex"}
    for profile_id, profile in deliberation.profiles.items():
        assert (
            profile.agent in supported
        ), f"Deliberation profile '{profile_id}' references unrunnable agent '{profile.agent}'"


def test_app_config_has_deliberation() -> None:
    """AppConfig must expose deliberation config used by iar deliberate."""
    from backend.core.shared.models.agent_deliberation import DeliberationConfig
    from backend.core.shared.models.agent_runner import AppConfig

    app_config = AppConfig()
    assert isinstance(app_config.deliberation, DeliberationConfig)
    profile_ids = {p.profile_id for p in app_config.deliberation.profiles}
    assert profile_ids == {"architect", "skeptic", "implementer"}


def test_default_planner_agent_has_command_builder() -> None:
    """Default planner agent must have a supported command builder."""
    from backend.engines.agent_runner.factory import _build_planner_command

    settings = AgentRunnerSettings()
    default_agent = settings.interactive_decision.default_agent
    # Must not raise for the default agent
    command = _build_planner_command(default_agent, "test prompt", Path("/tmp"))
    assert isinstance(command, list)
    assert len(command) > 0
    assert command[0] == default_agent


def test_planner_command_builders_for_supported_agents() -> None:
    """All supported agents can build planner commands."""
    from backend.engines.agent_runner.factory import _build_planner_command

    for agent_name in ("claude", "codex", "kimi"):
        command = _build_planner_command(agent_name, "test prompt", Path("/tmp"))
        assert isinstance(command, list)
        assert len(command) > 0
        assert command[0] == agent_name


def test_unknown_planner_agent_fails_fast() -> None:
    """Unsupported agents must raise ValueError for planner command builder."""
    from backend.engines.agent_runner.factory import _build_planner_command

    with pytest.raises(ValueError, match="does not have a command builder"):
        _build_planner_command("unknown-agent", "test prompt", Path("/tmp"))


def test_interactive_decision_settings_have_sane_defaults() -> None:
    """Interactive decision settings should have safe defaults."""
    from backend.infrastructure.config.settings import (
        AgentRunnerInteractiveDecisionSettings,
    )

    ids = AgentRunnerInteractiveDecisionSettings()
    assert ids.enabled is True
    assert ids.default_agent == "claude"
    assert ids.planner_timeout_seconds > 0
    assert ids.allow_execute_yes is True


def test_content_generation_command_defaults_to_claude() -> None:
    """内容生成命令构造器：codex 需显式指定，其余（含已解析的 auto / 未识别值）都走 claude。"""
    from backend.engines.agent_runner.factory import _build_content_generation_command

    for agent_name in ("auto", "claude", "gpt-unknown"):
        command = _build_content_generation_command(agent_name, "prompt", Path("/tmp"))
        assert command[0] == "claude", f"{agent_name} should build the claude command"
        assert "--dangerously-skip-permissions" in command

    codex_command = _build_content_generation_command("codex", "prompt", Path("/tmp"))
    assert codex_command[0] == "codex"
    assert "exec" in codex_command

    kimi_command = _build_content_generation_command("kimi", "prompt", Path("/tmp"))
    assert kimi_command[0] == "kimi"
