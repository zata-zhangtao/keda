"""管理终端的完成度统计（实时 GitHub 口径 + 本地历史趋势）。

实时口径：

- 对每个 workflow label 以 ``state="all"`` 查询 Issue 并按编号去重。
- ``completed``：closed 且不含 failed/blocked label。
- ``failed`` / ``blocked``：含对应 label（无论 open/closed）。
- ``open_in_pipeline``：open 且不含 failed/blocked label。
- ``completion_rate = completed / total_tracked``，分母为 0 时为 ``None``。

历史趋势直接委托 ``IRunHistoryStore.daily_run_trend``（SQLite 按天聚合）。
GitHub 仍是 workflow 状态唯一事实来源；SQLite 只反映 runner 处理留痕。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient
from backend.core.shared.interfaces.runner_console import (
    DailyRunTrendEntry,
    IRunHistoryStore,
)
from backend.core.shared.models.agent_runner import (
    IssueSummary,
    RepositoryRunContext,
)

_logger = logging.getLogger(__name__)

#: 单 label 查询上限；命中上限时标记 truncated，避免静默截断。
_PER_LABEL_QUERY_LIMIT = 200


@dataclass(frozen=True)
class RepositoryCompletionStats:
    """单个仓库的实时完成度统计。"""

    repo_id: str
    display_name: str
    total_tracked: int
    completed: int
    failed: int
    blocked: int
    open_in_pipeline: int
    completion_rate: float | None
    truncated: bool
    error: str | None = None


def _workflow_labels(context: RepositoryRunContext) -> tuple[str, ...]:
    labels = context.config.labels
    return (
        labels.ready,
        labels.running,
        labels.supervising,
        labels.review,
        labels.failed,
        labels.blocked,
    )


def _is_closed(issue: IssueSummary) -> bool:
    return issue.state.upper() == "CLOSED"


def build_completion_stats(
    *,
    context: RepositoryRunContext,
    github_client: IGitHubClient,
) -> RepositoryCompletionStats:
    """统计一个仓库被 agent workflow 跟踪过的 Issue 完成度。

    Args:
        context: 仓库运行上下文。
        github_client: 该仓库的 GitHub 客户端。

    Returns:
        RepositoryCompletionStats: 实时完成度统计；GitHub 查询失败时
        返回带 ``error`` 的空统计，不让单仓库失败拖死整个统计页。
    """
    tracked_issues: dict[int, IssueSummary] = {}
    truncated = False
    try:
        for workflow_label in _workflow_labels(context):
            labeled_issues = github_client.list_issues_by_label(
                workflow_label, _PER_LABEL_QUERY_LIMIT, state="all"
            )
            if len(labeled_issues) >= _PER_LABEL_QUERY_LIMIT:
                truncated = True
            for issue in labeled_issues:
                tracked_issues[issue.number] = issue
    except Exception as exc:  # noqa: BLE001 - isolate per-repo stats failures.
        _logger.warning("Completion stats unavailable for '%s': %s", context.repo_id, exc)
        return RepositoryCompletionStats(
            repo_id=context.repo_id,
            display_name=context.display_name,
            total_tracked=0,
            completed=0,
            failed=0,
            blocked=0,
            open_in_pipeline=0,
            completion_rate=None,
            truncated=False,
            error=str(exc),
        )

    failed_label = context.config.labels.failed
    blocked_label = context.config.labels.blocked
    completed_count = 0
    failed_count = 0
    blocked_count = 0
    open_in_pipeline_count = 0
    for issue in tracked_issues.values():
        has_failed = failed_label in issue.labels
        has_blocked = blocked_label in issue.labels
        if has_failed:
            failed_count += 1
        if has_blocked:
            blocked_count += 1
        if _is_closed(issue):
            if not has_failed and not has_blocked:
                completed_count += 1
        elif not has_failed and not has_blocked:
            open_in_pipeline_count += 1

    total_tracked = len(tracked_issues)
    completion_rate = completed_count / total_tracked if total_tracked > 0 else None
    return RepositoryCompletionStats(
        repo_id=context.repo_id,
        display_name=context.display_name,
        total_tracked=total_tracked,
        completed=completed_count,
        failed=failed_count,
        blocked=blocked_count,
        open_in_pipeline=open_in_pipeline_count,
        completion_rate=completion_rate,
        truncated=truncated,
    )


def build_completion_stats_overview(
    *,
    contexts: list[RepositoryRunContext],
    github_client_factory: Callable[[Path], IGitHubClient],
) -> list[RepositoryCompletionStats]:
    """对全部仓库构建实时完成度统计。"""
    return [
        build_completion_stats(
            context=context,
            github_client=github_client_factory(context.repo_path),
        )
        for context in contexts
    ]


def build_run_history_trend(
    *,
    store: IRunHistoryStore,
    repo_id: str | None,
    days: int,
) -> list[DailyRunTrendEntry]:
    """读取本地运行历史的按天趋势。

    Args:
        store: 运行历史存储端口。
        repo_id: 仓库过滤；``None`` 表示全部仓库。
        days: 回看天数（1-365 之间截断）。
    """
    bounded_days = min(max(days, 1), 365)
    return store.daily_run_trend(repo_id=repo_id, days=bounded_days)
