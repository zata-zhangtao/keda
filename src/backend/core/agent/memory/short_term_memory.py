"""短期记忆写业务规则。"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.core.agent.memory.protocols import (
    IShortTermMemoryStore,
    ShortTermAttempt,
    ShortTermContextPayload,
)
from backend.core.shared.models.agent_runner import (
    AttemptResult,
    IssueSummary,
    MemoryConfig,
)

_logger = logging.getLogger(__name__)


def save_short_term_memory(
    repo_id: str,
    issue: IssueSummary,
    attempt_result: AttemptResult,
    worktree_path: Path,
    memory_config: MemoryConfig,
    *,
    final_solution: str | None = None,
    key_files: tuple[str, ...] = (),
    summary: str | None = None,
    store: IShortTermMemoryStore,
) -> Path | None:
    """Update the per-Issue short-term memory after a recovery attempt.

    Args:
        repo_id: Stable per-repository identifier.
        issue: Issue being processed.
        attempt_result: The attempt that just finished.
        worktree_path: Worktree whose ``memory_config.base_dir`` should be
            used for persistence.
        memory_config: Effective memory configuration.
        final_solution: Optional final-solution snippet to record.
        key_files: Optional list of touched file paths to record.
        summary: Optional short summary line.
        store: Caller-injected short-term store.

    Returns:
        The path to the persisted ``context.json`` file, or ``None`` when
        memory persistence is disabled.
    """
    if not memory_config.enabled:
        return None
    if not worktree_path:
        return None
    existing = store.load(repo_id, issue.number) or ShortTermContextPayload(
        repo_id=repo_id,
        issue_number=issue.number,
        issue_title=issue.title,
        issue_url=issue.url,
    )
    existing.issue_title = issue.title or existing.issue_title
    existing.issue_url = issue.url or existing.issue_url
    if summary is not None:
        existing.summary = summary
    elif attempt_result.detail and not existing.summary:
        existing.summary = attempt_result.detail
    existing.attempts.append(
        ShortTermAttempt(
            attempt_number=attempt_result.attempt_number,
            failure_type=attempt_result.failure_type.value,
            detail=attempt_result.detail,
            recovered=attempt_result.recovered,
        )
    )
    if final_solution is not None:
        existing.final_solution = final_solution
    if key_files:
        merged = list(existing.key_files)
        for path in key_files:
            if path and path not in merged:
                merged.append(path)
        existing.key_files = tuple(merged)
    try:
        path = store.save(repo_id, issue.number, existing)
    except OSError as exc:
        _logger.warning(
            "Failed to persist short-term memory for Issue #%d: %s",
            issue.number,
            exc,
        )
        return None
    return path


__all__ = ["save_short_term_memory"]
