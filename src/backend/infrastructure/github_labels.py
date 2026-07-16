"""Label syncing for the GitHub CLI client.

Holds the static label specifications and the
:func:`sync_labels` helper that creates or updates each label
through ``gh label create``. Extracted out of the main client so
:class:`backend.infrastructure.github_client.GitHubCliClient` stays
focused on connection lifecycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, Sequence

from backend.infrastructure.github_models import LabelConfig

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)


class _ClientProtocol(Protocol):
    """Duck-typed interface expected by :func:`sync_labels`."""

    repo_path: object

    def _run_with_retry(self, command: Sequence[str], *, cwd: object) -> object: ...


_LABEL_SPECS: list[tuple[str, str, str]] = [
    ("agent/ready", "0E8A16", "Issue is ready for a local AI runner to claim."),
    (
        "agent/running",
        "FBCA04",
        "Issue is currently being executed by a local AI runner.",
    ),
    (
        "agent/supervising",
        "C5DEF5",
        "PR exists and automatic post-PR supervisor is reviewing or reprocessing.",
    ),
    ("agent/review", "1D76DB", "AI runner opened work for human review."),
    ("agent/failed", "D73A4A", "AI runner failed and posted details."),
    ("agent/blocked", "000000", "AI runner needs human input."),
    (
        "agent/waiting",
        "FEF2C0",
        "Issue has unmet dependencies and is waiting for upstream closure.",
    ),
    (
        "agent/rework-prd",
        "D93F0B",
        "Request the AI runner to generate or rewrite this Issue's PRD.",
    ),
    (
        "agent/deliberate",
        "D4C5F9",
        "Issue needs multi-agent deliberation (Phase 0) before implementation.",
    ),
    (
        "validation/pending",
        "FBCA04",
        "Realistic Validation evidence awaits human sign-off on the PR.",
    ),
    (
        "validation/passed",
        "0E8A16",
        "A human verified the validation evidence and signed off.",
    ),
    (
        "validation/verifier-passed",
        "0E8A16",
        "Independent verifier agent approved this PR.",
    ),
    (
        "source/prd",
        "0052CC",
        "Issue has a canonical PRD tracked in the repository.",
    ),
    ("type/feature", "1D76DB", "User-facing feature or capability work."),
    ("type/refactor", "5319E7", "Internal refactor or structural improvement."),
    ("type/bug", "D73A4A", "Broken behavior or regression fix."),
    ("status/backlog", "BFDADC", "Tracked work that is not in progress yet."),
]


_AGENT_LABEL_META: dict[str, tuple[str, str]] = {
    "codex": ("5319E7", "Use Codex for local runner execution."),
    "claude": ("BFDADC", "Use Claude Code for local runner execution."),
    "kimi": ("FF6B6B", "Use Kimi for local runner execution."),
}


def sync_labels(client: _ClientProtocol, labels: LabelConfig) -> None:
    """Create or update standard labels."""
    label_specs = list(_LABEL_SPECS)
    for agent_name, label_text in labels.agent_labels.items():
        color, description = _AGENT_LABEL_META.get(
            agent_name, ("5319E7", f"Use {agent_name} for local runner execution.")
        )
        label_specs.append((f"agent/{agent_name}", color, description))
    configured_names = {
        "agent/ready": labels.ready,
        "agent/running": labels.running,
        "agent/supervising": labels.supervising,
        "agent/review": labels.review,
        "agent/failed": labels.failed,
        "agent/blocked": labels.blocked,
        "agent/waiting": labels.waiting,
        "agent/rework-prd": labels.rework_prd,
        "agent/deliberate": labels.deliberate,
        "validation/pending": labels.validation_pending,
        "validation/passed": labels.validation_passed,
        "validation/verifier-passed": labels.verifier_passed,
    }
    configured_names.update({f"agent/{k}": v for k, v in labels.agent_labels.items()})
    for label_name, color, description in label_specs:
        effective_name = configured_names.get(label_name, label_name)
        client._run_with_retry(
            [
                "gh",
                "label",
                "create",
                effective_name,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ],
            cwd=client.repo_path,
        )


__all__ = ["sync_labels"]
