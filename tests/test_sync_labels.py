"""Tests for label synchronization."""

from __future__ import annotations

from backend.core.shared.models.agent_runner import LabelConfig
from backend.core.use_cases.sync_labels import sync_labels
from tests.conftest import FakeGitHubClient


def test_sync_labels_calls_client() -> None:
    """sync_labels should delegate to the GitHub client."""
    fake_client = FakeGitHubClient()
    labels_config = LabelConfig()
    sync_labels(labels_config=labels_config, github_client=fake_client)

    sync_calls = [c for c in fake_client.calls if c["method"] == "sync_labels"]
    assert len(sync_calls) == 1
    assert sync_calls[0]["labels"] == labels_config


def test_sync_labels_includes_supervising() -> None:
    """LabelConfig must include the supervising label."""
    labels_config = LabelConfig()
    assert labels_config.supervising == "agent/supervising"


def test_sync_labels_includes_rework_prd() -> None:
    """LabelConfig must expose the rework-prd trigger label for Issue->PRD."""
    labels_config = LabelConfig()
    assert labels_config.rework_prd == "agent/rework-prd"


def test_sync_labels_includes_verifier_passed() -> None:
    """LabelConfig must expose the verifier-passed label so post-PR verdict can tag."""
    labels_config = LabelConfig()
    assert labels_config.verifier_passed == "validation/verifier-passed"
