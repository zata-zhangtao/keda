"""Agent runner rework helpers."""

from __future__ import annotations

from backend.core.shared.models.agent_runner import IssueSummary


def build_missing_worktree_comment(
    *,
    issue: IssueSummary,
    pr_branch: str,
    expected_path: str,
) -> str:
    """Build an actionable blocked comment when a rework worktree is missing."""
    return "\n".join(
        [
            "## Agent Runner Rework Blocked",
            "",
            f"Pending rework for Issue #{issue.number} cannot run because the "
            f"worktree for branch `{pr_branch}` is missing.",
            "",
            f"- Expected worktree path: `{expected_path}`",
            "- PR branch will not be repaired until the worktree is restored.",
            "",
            "To recover:",
            f"1. Create or restore the worktree for branch `{pr_branch}`.",
            "2. Ensure the branch HEAD matches the pending rework marker.",
            "3. Re-run `iar run` to pick up the rework marker.",
        ]
    )
