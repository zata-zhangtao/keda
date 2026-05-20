"""Sync standard GitHub labels for agent-runner workflow."""

from __future__ import annotations

import logging

from backend.core.shared.interfaces.agent_runner import IGitHubClient
from backend.core.shared.models.agent_runner import LabelConfig

_logger = logging.getLogger(__name__)


def sync_labels(
    *,
    labels_config: LabelConfig,
    github_client: IGitHubClient,
) -> None:
    """Create or update standard labels in the target repository.

    Args:
        labels_config: Label names to use.
        github_client: Client for interacting with GitHub.
    """
    github_client.sync_labels(labels_config)
    _logger.info("Labels synchronized.")
