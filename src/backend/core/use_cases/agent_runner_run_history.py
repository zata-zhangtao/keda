"""Agent runner run-history side-channel recording."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.core.shared.interfaces.runner_console import (
    AttemptRecord,
    IRunHistoryStore,
    RunRecord,
)
from backend.core.shared.models.agent_runner import (
    AttemptResult,
    FailureType,
    IssueSummary,
)

_logger = logging.getLogger(__name__)

#: 实时 attempt 历史评论最多渲染的行数，避免评论体随反复 claim 无限增长。
ATTEMPT_HISTORY_MAX_ROWS = 50

__all__ = [
    "ATTEMPT_HISTORY_MAX_ROWS",
    "IssueAttemptTrail",
    "append_run_record",
    "load_issue_attempt_trail",
]


def append_run_record(
    *,
    run_history_store: IRunHistoryStore | None,
    repo_id: str,
    repo_path: Path,
    issue: IssueSummary,
    trigger: str,
    agent: str,
    outcome: str,
    error_summary: str | None,
    started_at: "datetime",
) -> None:
    """旁路写入一条运行记录；任何失败都不阻断 runner。

    Args:
        run_history_store: 运行历史存储；为 ``None`` 时直接跳过。
        repo_id: 目标仓库标识。
        repo_path: 目标仓库路径。
        issue: 本次运行处理的 Issue。
        trigger: 触发来源（如 ``cli_run``）。
        agent: 实际使用的 AI agent 名称。
        outcome: 运行结果摘要标识。
        error_summary: 失败时的错误摘要；成功时为 ``None``。
        started_at: 运行开始时间（UTC）。
    """
    if run_history_store is None:
        return
    finished_at = datetime.now(timezone.utc)
    try:
        run_history_store.append_run(
            RunRecord(
                repo_id=repo_id,
                repo_path=str(repo_path),
                issue_number=issue.number,
                trigger=trigger,
                agent=agent,
                outcome=outcome,
                error_summary=error_summary,
                started_at=started_at.isoformat(timespec="seconds"),
                finished_at=finished_at.isoformat(timespec="seconds"),
                duration_seconds=(finished_at - started_at).total_seconds(),
            )
        )
    except Exception as record_exc:  # noqa: BLE001 - side channel only.
        _logger.warning(
            "Failed to record run history for Issue #%d: %s",
            issue.number,
            record_exc,
        )


@dataclass(frozen=True)
class IssueAttemptTrail:
    """某个 Issue 已落库的 attempt 轨迹（渲染视图）。

    Attributes:
        attempts: 按时间正序排列的 attempt；无存储或读取失败时为空列表。
        older_omitted: 是否因超出 ``ATTEMPT_HISTORY_MAX_ROWS`` 而丢弃了更早的记录。
    """

    attempts: list[AttemptResult]
    older_omitted: bool


def load_issue_attempt_trail(
    *,
    run_history_store: IRunHistoryStore | None,
    repo_id: str,
    issue_number: int,
) -> IssueAttemptTrail:
    """读取某个 Issue 跨 agent、跨 claim 的完整 attempt 轨迹。

    runner 内存中的 attempt 列表在跨 agent fallback 和重新 claim 时都会从 1
    重新开始，因此只能代表"本轮"；存储侧按 Issue 累积全部尝试，是实时评论的
    渲染源。任何读取失败都降级为空轨迹，由调用方回退到内存列表。

    Args:
        run_history_store: 运行历史存储；为 ``None`` 时返回空轨迹。
        repo_id: 目标仓库标识。
        issue_number: 目标 Issue 编号。

    Returns:
        IssueAttemptTrail: 轨迹与截断状态。
    """
    if run_history_store is None:
        return IssueAttemptTrail(attempts=[], older_omitted=False)
    try:
        stored_attempts = run_history_store.list_issue_attempts(
            repo_id=repo_id,
            issue_number=issue_number,
            limit=ATTEMPT_HISTORY_MAX_ROWS + 1,
        )
    except Exception as trail_exc:  # noqa: BLE001 - side channel only.
        _logger.warning(
            "Failed to load attempt trail for Issue #%d: %s",
            issue_number,
            trail_exc,
        )
        return IssueAttemptTrail(attempts=[], older_omitted=False)
    rendered_attempts = [
        _stored_attempt_to_result(stored_attempt)
        for stored_attempt in stored_attempts[-ATTEMPT_HISTORY_MAX_ROWS:]
    ]
    return IssueAttemptTrail(
        attempts=[attempt for attempt in rendered_attempts if attempt is not None],
        older_omitted=len(stored_attempts) > ATTEMPT_HISTORY_MAX_ROWS,
    )


def _stored_attempt_to_result(attempt_record: AttemptRecord) -> AttemptResult | None:
    """把一条已落库的 attempt 记录还原为可渲染的 :class:`AttemptResult`。

    ``failure_type`` 落库时写的是枚举字面值；若历史行的取值已不在当前
    :class:`FailureType` 中（例如枚举更名后读到旧库），跳过该行并返回
    ``None``，而不是让整条轨迹渲染失败。
    """
    try:
        failure_type = FailureType(attempt_record.failure_type)
    except ValueError:
        _logger.warning(
            "Skipping stored attempt with unknown failure_type %r for Issue #%d.",
            attempt_record.failure_type,
            attempt_record.issue_number,
        )
        return None
    return AttemptResult(
        attempt_number=attempt_record.attempt_number,
        failure_type=failure_type,
        recovered=attempt_record.recovered,
        detail=attempt_record.detail,
        agent=attempt_record.agent,
        started_at=attempt_record.started_at,
        finished_at=attempt_record.finished_at,
        duration_seconds=attempt_record.duration_seconds,
    )
