"""Ensure agent labels, settings, and command builders stay in sync."""

from __future__ import annotations

from backend.core.shared.models.agent_runner import LabelConfig as CoreLabelConfig
from backend.core.use_cases.run_agent_once import _AGENT_COMMAND_BUILDERS
from backend.infrastructure.config.settings import AgentRunnerLabelSettings
from backend.infrastructure.github_client import LabelConfig as InfraLabelConfig


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
