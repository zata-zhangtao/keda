"""Reclaim Issues stuck at ``agent/running`` after their runner process died.

硬中断(SIGKILL / 崩溃 / 关机)不会把 Issue 从 ``agent/running`` 退回,而 daemon
只认领 ``agent/ready``,于是任务会永久卡死、无人接手。本模块在 daemon 每轮开头
扫描 running Issue,**当满足以下条件之一**时 best-effort CAS 退回 ``agent/ready``:

- claim 标记的 host 是本机、记录的 PID 已不存活(原行为,保守)
- ``ttl_seconds`` 非 ``None``、claim marker 含 ``started_at`` 字段、
  且 ``now - started_at >= ttl_seconds``(TTL 路径,处理"daemon 自己
  持锁但已卡死"的场景——daemon PID 永远 alive,只有靠 TTL 才能让下一轮接走)

其他保守规则不变:不触碰跨机器 claim、不触碰关闭 issue、best-effort 处理
单 issue 失败不影响扫整体扫描。

TTL 路径的额外保护:claim marker 没有 ``started_at`` 字段时(老 marker 格式),
**仍走"PID 必须死"判定**,不启用 TTL 路径,避免对老 claim 误判。
"""

from __future__ import annotations

import logging
import os
import re
import socket
from datetime import datetime, timezone
from typing import Callable

from backend.core.shared.interfaces.agent_runner import IGitHubClient
from backend.core.shared.models.agent_runner import AppConfig
from backend.core.use_cases.agent_runner_workflow import (
    transition_issue_workflow_state,
)

_logger = logging.getLogger(__name__)

# 兼容老 marker:`host` 必填,`pid`/`started_at` 可选;后者仅在 TTL 路径下使用。
_CLAIM_MARKER_PATTERN = re.compile(
    r'<!--\s*iar:claim\s+host="(?P<host>[^"]*)"'
    r'(?:\s+pid="(?P<pid>\d+)")?'
    r'(?:\s+started_at="(?P<started_at>[^"]*)")?'
    r"\s*-->"
)


def format_claim_marker(
    host: str,
    pid: int,
    *,
    started_at: datetime | None = None,
) -> str:
    """Hidden marker recording which host/PID owns an ``agent/running`` claim.

    Appended to the "Agent Runner Claimed" comment so a later daemon pass can
    tell whether the owning process is still alive (see
    :func:`reclaim_stale_running_issues`). ``started_at`` 字段可选;传入时写入
    comment 供 TTL reclaim 路径使用,不传则保持向后兼容的纯 ``host/pid`` marker。
    """
    if started_at is None:
        return f'<!-- iar:claim host="{host}" pid="{pid}" -->'
    iso_started_at = started_at.astimezone(timezone.utc).isoformat()
    return f'<!-- iar:claim host="{host}" pid="{pid}" ' f'started_at="{iso_started_at}" -->'


def parse_claim_marker(comment_body: str) -> tuple[str, int] | None:
    """Extract ``(host, pid)`` from the last claim marker in ``comment_body``.

    向后兼容的 2-tuple 包装;若需读取 ``started_at`` 字段,使用
    :func:`parse_claim_marker_body`。
    """
    full = parse_claim_marker_body(comment_body)
    if full is None:
        return None
    return full[0], full[1]


def parse_claim_marker_body(
    comment_body: str,
) -> tuple[str, int, datetime | None] | None:
    """Extract ``(host, pid, started_at)`` from the last claim marker.

    ``started_at`` 为 ``None`` 时表示该 marker 不含时间戳,TTL reclaim 路径会
    跳过这种 issue。``pid`` 缺省时按"无法证明死亡"返回 ``None``。
    """
    last_match = None
    for last_match in _CLAIM_MARKER_PATTERN.finditer(comment_body):
        pass
    if last_match is None:
        return None
    host = last_match.group("host")
    pid_raw = last_match.group("pid")
    started_raw = last_match.group("started_at")
    if not host or pid_raw is None:
        return None
    try:
        pid = int(pid_raw)
    except ValueError:
        return None
    started_at: datetime | None = None
    if started_raw:
        try:
            started_at = datetime.fromisoformat(started_raw)
        except ValueError:
            started_at = None
    return host, pid, started_at


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


def reclaim_stale_running_issues(
    *,
    config: AppConfig,
    github_client: IGitHubClient,
    limit: int = 50,
    host: str | None = None,
    pid_alive: Callable[[int], bool] = is_pid_alive,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> list[int]:
    """Flip abandoned ``agent/running`` Issues back to ``agent/ready``.

    仅当满足以下**任一**条件时,best-effort CAS 退回 ``agent/ready``:

    - claim 标记的 host 是本机、记录的 PID 已不存活(原行为,保守)
    - ``ttl_seconds`` 非 ``None``,claim marker 含 ``started_at`` 且距
      ``now`` 超过 TTL;此时即便 PID 仍存活也视为 stale。

    Args:
        config: Per-repository application config (provides workflow labels).
        github_client: GitHub client for the target repository.
        limit: Maximum running Issues to inspect in one pass.
        host: Override for this machine's hostname (tests). Defaults to
            ``socket.gethostname()``.
        pid_alive: Liveness probe; injectable for tests.
        ttl_seconds: 启用 TTL reclaim 的秒数阈值。``None`` 表示禁用 TTL 路径
            (走原"PID 死了才 reclaim"逻辑)。daemon 调用点暂时传 ``None`` 保持
            现有行为,留给将来打开 TTL 留口子。
        now: 当前时间(测试可注入);默认 ``datetime.now(timezone.utc)``。

    Returns:
        The Issue numbers that were reclaimed to ``agent/ready``.
    """
    this_host = host if host is not None else socket.gethostname()
    effective_now = now if now is not None else datetime.now(timezone.utc)
    running_issues = github_client.list_issues_by_label(config.labels.running, limit)
    reclaimed: list[int] = []
    for issue in running_issues:
        if issue.state.upper() != "OPEN":
            continue
        claim = _latest_claim_full(issue.number, github_client)
        if claim is None:
            # 拿不到归属信息,无法证明它死了,保守跳过。
            continue
        claim_host, claim_pid, claim_started_at = claim
        if claim_host != this_host:
            continue
        reclaim_reason = _classify_reclaim(
            claim_pid=claim_pid,
            claim_started_at=claim_started_at,
            effective_now=effective_now,
            ttl_seconds=ttl_seconds,
            pid_alive=pid_alive,
        )
        if reclaim_reason is None:
            continue
        try:
            if _reclaim_one(
                issue_number=issue.number,
                claim_pid=claim_pid,
                config=config,
                github_client=github_client,
                this_host=this_host,
                reclaim_reason=reclaim_reason,
                claim_started_at=claim_started_at,
            ):
                reclaimed.append(issue.number)
        except Exception as exc:  # noqa: BLE001 - one bad Issue must not stop the sweep.
            _logger.error("Failed to reclaim stale Issue #%d: %s", issue.number, exc)
    return reclaimed


def _latest_claim_full(
    issue_number: int,
    github_client: IGitHubClient,
) -> tuple[str, int, datetime | None] | None:
    """Return the most recent ``(host, pid, started_at)`` claim marker."""
    for comment_body in reversed(github_client.list_issue_comments(issue_number)):
        parsed = parse_claim_marker_body(comment_body)
        if parsed is not None:
            return parsed
    return None


def _classify_reclaim(
    *,
    claim_pid: int,
    claim_started_at: datetime | None,
    effective_now: datetime,
    ttl_seconds: int | None,
    pid_alive: Callable[[int], bool],
) -> str | None:
    """判断 issue 是否应该被 reclaim,返回 reclaim 原因字符串,否则 ``None``。

    - 原行为路径(PID 死了):返回 ``"dead_pid"``
    - TTL 路径(PID 还活但 claim 太久):返回 ``"ttl_expired"``
    """
    if not pid_alive(claim_pid):
        return "dead_pid"
    if ttl_seconds is not None and claim_started_at is not None:
        age_seconds = (effective_now - claim_started_at).total_seconds()
        if age_seconds >= ttl_seconds:
            return "ttl_expired"
    return None


def _reclaim_one(
    *,
    issue_number: int,
    claim_pid: int,
    config: AppConfig,
    github_client: IGitHubClient,
    this_host: str,
    reclaim_reason: str,
    claim_started_at: datetime | None,
) -> bool:
    """CAS a single Issue from ``agent/running`` to ``agent/ready``。

    Mirrors :func:`claim_blocked_issue` 的 read-check-write-reread 模式:写回前
    再确认仍是 running,写回后确认已变 ready,二次确认失败则放弃(返回 False)。
    """
    issue_before = github_client.get_issue(issue_number)
    if config.labels.running not in issue_before.labels:
        return False
    transition_issue_workflow_state(github_client, issue_number, config, config.labels.ready)
    issue_after = github_client.get_issue(issue_number)
    if config.labels.ready not in issue_after.labels:
        return False

    if reclaim_reason == "ttl_expired":
        reason_text = (
            "The owning runner process is still alive, but its claim marker "
            f"is older than the TTL threshold (started_at=`{claim_started_at}`). "
            "Returning to ready so the next daemon pass can re-pick it up."
        )
    else:
        reason_text = (
            "The owning runner process is no longer alive, so this Issue was "
            f"returned to `{config.labels.ready}` for re-pickup. Committed "
            "progress and the Realistic Validation re-execution cache are "
            "reused, so work does not restart from scratch."
        )

    github_client.comment_issue(
        issue_number,
        "## Stale Run Reclaimed\n\n"
        f"- Host: `{this_host}`\n"
        f"- Claim PID: `{claim_pid}`\n"
        f"- Reason: `{reclaim_reason}`\n\n"
        f"{reason_text}",
    )
    _logger.info(
        "Reclaimed stale Issue #%d (pid %d on %s, reason=%s) to %s.",
        issue_number,
        claim_pid,
        this_host,
        reclaim_reason,
        config.labels.ready,
    )
    return True
