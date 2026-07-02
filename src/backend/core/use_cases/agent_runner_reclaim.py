"""Reclaim Issues stuck at ``agent/running`` after their runner process died.

硬中断(SIGKILL / 崩溃 / 关机)不会把 Issue 从 ``agent/running`` 退回,而 daemon
只认领 ``agent/ready``,于是任务会永久卡死、无人接手。本模块在 daemon 每轮开头
扫描 running Issue,**仅当** claim 标记的 host 是本机、且记录的 PID 已不存活时,
才 best-effort CAS 退回 ``agent/ready``——让正常流程(复用 worktree 上的已提交进度
+ L2 的 RV 复跑缓存)接着推进,而不是从零重来。

保守判定是刻意的:误收一个**还活着**的运行,比让一个死任务多卡一会儿危险得多。
因此本模块绝不触碰别的机器(host 不符)或仍存活的进程(PID 还在)的 running Issue;
拿不到 claim 标记时同样跳过。跨机器孤儿留待"同机重启 daemon"或后续的心跳/TTL 方案。
"""

from __future__ import annotations

import logging
import os
import re
import socket
from typing import Callable

from backend.core.shared.interfaces.agent_runner import IGitHubClient
from backend.core.shared.models.agent_runner import AppConfig
from backend.core.use_cases.agent_runner_workflow import (
    transition_issue_workflow_state,
)

_logger = logging.getLogger(__name__)

_CLAIM_MARKER_PATTERN = re.compile(
    r'<!--\s*iar:claim\s+host="(?P<host>[^"]*)"\s+pid="(?P<pid>\d+)"\s*-->'
)


def format_claim_marker(host: str, pid: int) -> str:
    """Hidden marker recording which host/PID owns an ``agent/running`` claim.

    Appended to the "Agent Runner Claimed" comment so a later daemon pass can
    tell whether the owning process is still alive (see
    :func:`reclaim_stale_running_issues`).
    """
    return f'<!-- iar:claim host="{host}" pid="{pid}" -->'


def parse_claim_marker(comment_body: str) -> tuple[str, int] | None:
    """Extract ``(host, pid)`` from the last claim marker in ``comment_body``."""
    last_match = None
    for last_match in _CLAIM_MARKER_PATTERN.finditer(comment_body):
        pass
    if last_match is None:
        return None
    return last_match.group("host"), int(last_match.group("pid"))


def is_pid_alive(pid: int) -> bool:
    """Return whether ``pid`` is a live process on this host.

    ``os.kill(pid, 0)`` sends no signal — it only probes existence. A
    ``PermissionError`` means the PID exists but is owned by another user, so
    it is still alive and must not be reclaimed. Any uncertainty errs toward
    "alive" so a live run is never disturbed.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _latest_claim(
    issue_number: int, github_client: IGitHubClient
) -> tuple[str, int] | None:
    """Return the most recent ``(host, pid)`` claim marker for an Issue."""
    for comment_body in reversed(github_client.list_issue_comments(issue_number)):
        parsed = parse_claim_marker(comment_body)
        if parsed is not None:
            return parsed
    return None


def reclaim_stale_running_issues(
    *,
    config: AppConfig,
    github_client: IGitHubClient,
    limit: int = 50,
    host: str | None = None,
    pid_alive: Callable[[int], bool] = is_pid_alive,
) -> list[int]:
    """Flip abandoned ``agent/running`` Issues back to ``agent/ready``.

    Only reclaims an Issue whose latest claim marker names *this* host and a
    PID that is no longer alive — the conservative rule that never disturbs a
    live run or another machine's work. Best-effort: a failure on one Issue is
    logged and skipped so the daemon keeps polling.

    Args:
        config: Per-repository application config (provides workflow labels).
        github_client: GitHub client for the target repository.
        limit: Maximum running Issues to inspect in one pass.
        host: Override for this machine's hostname (tests). Defaults to
            ``socket.gethostname()``.
        pid_alive: Liveness probe; injectable for tests.

    Returns:
        The Issue numbers that were reclaimed to ``agent/ready``.
    """
    this_host = host if host is not None else socket.gethostname()
    running_issues = github_client.list_issues_by_label(config.labels.running, limit)
    reclaimed: list[int] = []
    for issue in running_issues:
        if issue.state.upper() != "OPEN":
            continue
        claim = _latest_claim(issue.number, github_client)
        if claim is None:
            # 拿不到归属信息,无法证明它死了,保守跳过。
            continue
        claim_host, claim_pid = claim
        if claim_host != this_host or pid_alive(claim_pid):
            continue
        try:
            if _reclaim_one(issue.number, claim_pid, config, github_client, this_host):
                reclaimed.append(issue.number)
        except Exception as exc:  # noqa: BLE001 - one bad Issue must not stop the sweep.
            _logger.error("Failed to reclaim stale Issue #%d: %s", issue.number, exc)
    return reclaimed


def _reclaim_one(
    issue_number: int,
    dead_pid: int,
    config: AppConfig,
    github_client: IGitHubClient,
    this_host: str,
) -> bool:
    """CAS a single Issue from ``agent/running`` to ``agent/ready``.

    Mirrors :func:`claim_blocked_issue` 的 read-check-write-reread 模式:写回前
    再确认仍是 running,写回后确认已变 ready,二次确认失败则放弃(返回 False)。
    """
    issue_before = github_client.get_issue(issue_number)
    if config.labels.running not in issue_before.labels:
        return False
    transition_issue_workflow_state(
        github_client, issue_number, config, config.labels.ready
    )
    issue_after = github_client.get_issue(issue_number)
    if config.labels.ready not in issue_after.labels:
        return False
    github_client.comment_issue(
        issue_number,
        "## Stale Run Reclaimed\n\n"
        f"- Host: `{this_host}`\n"
        f"- Dead PID: `{dead_pid}`\n\n"
        "The owning runner process is no longer alive, so this Issue was "
        f"returned to `{config.labels.ready}` for re-pickup. Committed progress "
        "and the Realistic Validation re-execution cache are reused, so work "
        "does not restart from scratch.",
    )
    _logger.info(
        "Reclaimed stale Issue #%d (dead pid %d on %s) to %s.",
        issue_number,
        dead_pid,
        this_host,
        config.labels.ready,
    )
    return True
