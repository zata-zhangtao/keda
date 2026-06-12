"""Agent runner run-history side-channel recording."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from backend.core.shared.interfaces.runner_console import (
    IRunHistoryStore,
    RunRecord,
)
from backend.core.shared.models.agent_runner import IssueSummary

_logger = logging.getLogger(__name__)

__all__ = ["append_run_record"]


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
