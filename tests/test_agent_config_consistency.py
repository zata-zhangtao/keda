"""Ensure agent labels, settings, and command builders stay in sync."""

from __future__ import annotations

from pathlib import Path

from backend.core.shared.models.agent_runner import LabelConfig as CoreLabelConfig
from backend.core.use_cases.run_agent_once import _AGENT_COMMAND_BUILDERS
from backend.engines.agent_runner.factory import build_app_config
from backend.infrastructure.config import settings as settings_module
from backend.infrastructure.config.settings import AgentRunnerLabelSettings
from backend.infrastructure.github_client import LabelConfig as InfraLabelConfig


def test_agent_runner_reads_root_config_toml() -> None:
    """Agent runner settings should load the repository root config.toml."""
    repository_root = Path(__file__).resolve().parents[1]
    app_config = build_app_config()

    assert settings_module._PROJECT_ROOT_PATH == repository_root
    assert settings_module._TOML_CONFIG_FILE_PATH == repository_root / "config.toml"
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
