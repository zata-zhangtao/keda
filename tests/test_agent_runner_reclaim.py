"""Tests for stale ``agent/running`` reclaim (L1 hard-kill recovery)."""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime, timedelta, timezone

from backend.core.shared.models.agent_runner import AppConfig
from backend.core.use_cases.agent_runner_reclaim import (
    format_claim_marker,
    is_pid_alive,
    parse_claim_marker,
    parse_claim_marker_body,
    reclaim_stale_running_issues,
)
from tests.conftest import FakeGitHubClient


def _seed_running_issue(
    github_client: FakeGitHubClient,
    config: AppConfig,
    *,
    issue_number: int,
    marker: str | None,
) -> None:
    """Seed an ``agent/running`` Issue, optionally with a claim marker comment."""
    github_client.edit_issue_labels(issue_number, add=[config.labels.running])
    if marker is not None:
        github_client.comment_issue(issue_number, f"## Agent Runner Claimed\n\n{marker}")
    github_client.set_list_issues_by_label_result([github_client.get_issue(issue_number)])


def test_claim_marker_round_trip() -> None:
    """A formatted claim marker parses back to the same host and pid."""
    marker = format_claim_marker("host-a", 4242)
    assert parse_claim_marker(f"## Claimed\n{marker}") == ("host-a", 4242)


def test_parse_claim_marker_picks_last() -> None:
    """When a comment carries several markers, the last one wins."""
    body = format_claim_marker("host-a", 1) + "\n" + format_claim_marker("host-b", 2)
    assert parse_claim_marker(body) == ("host-b", 2)


def test_parse_claim_marker_absent_returns_none() -> None:
    assert parse_claim_marker("no marker here") is None


def test_is_pid_alive_distinguishes_live_and_dead() -> None:
    """The current process is alive; an unused high PID is not."""
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(2**31 - 1) is False
    assert is_pid_alive(0) is False


def test_reclaim_flips_dead_local_run_to_ready() -> None:
    """A same-host, dead-PID run is returned to ready with an explanatory note."""
    config = AppConfig()
    github = FakeGitHubClient()
    _seed_running_issue(github, config, issue_number=5, marker=format_claim_marker("host-a", 4242))

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: False,
    )

    assert reclaimed == [5]
    labels = github.get_issue(5).labels
    assert config.labels.ready in labels
    assert config.labels.running not in labels
    assert any("Stale Run Reclaimed" in body for body in github.list_issue_comments(5))


def test_reclaim_skips_live_pid() -> None:
    """A run whose PID is still alive must never be reclaimed."""
    config = AppConfig()
    github = FakeGitHubClient()
    _seed_running_issue(github, config, issue_number=5, marker=format_claim_marker("host-a", 4242))

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: True,
    )

    assert reclaimed == []
    assert config.labels.running in github.get_issue(5).labels


def test_reclaim_skips_other_host() -> None:
    """A run claimed by a different machine is left untouched."""
    config = AppConfig()
    github = FakeGitHubClient()
    _seed_running_issue(github, config, issue_number=5, marker=format_claim_marker("host-b", 4242))

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: False,
    )

    assert reclaimed == []
    assert config.labels.running in github.get_issue(5).labels


def test_reclaim_skips_issue_without_claim_marker() -> None:
    """Without a claim marker we cannot prove death, so we skip conservatively."""
    config = AppConfig()
    github = FakeGitHubClient()
    _seed_running_issue(github, config, issue_number=5, marker=None)

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: False,
    )

    assert reclaimed == []
    assert config.labels.running in github.get_issue(5).labels


def test_reclaim_skips_closed_issue() -> None:
    """A closed Issue is never resurrected, even with a dead local claim."""
    config = AppConfig()
    github = FakeGitHubClient()
    github.edit_issue_labels(5, add=[config.labels.running])
    github.comment_issue(5, format_claim_marker("host-a", 4242))
    closed = dataclasses.replace(github.get_issue(5), state="CLOSED")
    github.set_list_issues_by_label_result([closed])

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: False,
    )

    assert reclaimed == []


# ---------------------------------------------------------------------------
# TTL reclaim path — issue stuck on a still-alive PID (e.g. daemon itself).
# ---------------------------------------------------------------------------


def test_format_claim_marker_with_started_at_round_trips() -> None:
    """started_at 字段可往返解析。"""
    started = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
    marker = format_claim_marker("host-a", 4242, started_at=started)
    assert parse_claim_marker_body(f"## Claimed\n{marker}") == (
        "host-a",
        4242,
        started,
    )


def test_format_claim_marker_without_started_at_yields_none_started() -> None:
    """不传 started_at 时,parse 返回 ``(host, pid, None)``(向后兼容)。"""
    marker = format_claim_marker("host-a", 4242)
    assert parse_claim_marker_body(f"## Claimed\n{marker}") == (
        "host-a",
        4242,
        None,
    )


def test_reclaim_ttl_expired_reclaims_live_pid() -> None:
    """TTL 路径:claim 已超时、daemon 自己仍存活(PID=os.getpid())→ reclaim。"""
    config = AppConfig()
    github = FakeGitHubClient()
    started = datetime(2026, 7, 4, 8, 0, 0, tzinfo=timezone.utc)
    _seed_running_issue(
        github,
        config,
        issue_number=5,
        marker=format_claim_marker("host-a", os.getpid(), started_at=started),
    )

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: True,  # PID alive:daemon 自己
        ttl_seconds=3600,
        now=started + timedelta(hours=2),
    )

    assert reclaimed == [5]
    labels = github.get_issue(5).labels
    assert config.labels.ready in labels
    assert config.labels.running not in labels
    comment_bodies = github.list_issue_comments(5)
    assert any("ttl_expired" in body for body in comment_bodies)


def test_reclaim_ttl_not_expired_keeps_live_pid() -> None:
    """TTL 路径下 claim 还没超阈值,即使 PID 活也保留。"""
    config = AppConfig()
    github = FakeGitHubClient()
    started = datetime(2026, 7, 4, 8, 0, 0, tzinfo=timezone.utc)
    _seed_running_issue(
        github,
        config,
        issue_number=5,
        marker=format_claim_marker("host-a", os.getpid(), started_at=started),
    )

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: True,
        ttl_seconds=3600,
        now=started + timedelta(minutes=5),  # 5 分钟 < 1 小时 TTL
    )

    assert reclaimed == []
    assert config.labels.running in github.get_issue(5).labels


def test_reclaim_ttl_skips_marker_without_started_at() -> None:
    """TTL=0 但 marker 是老格式(无 started_at)→ 仍按"PID 死了才 reclaim"。"""
    config = AppConfig()
    github = FakeGitHubClient()
    _seed_running_issue(
        github,
        config,
        issue_number=5,
        marker=format_claim_marker("host-a", os.getpid()),  # 没 started_at
    )

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: True,  # PID alive
        ttl_seconds=0,  # TTL 启用但 marker 老格式
        now=datetime(2026, 7, 4, 20, 0, 0, tzinfo=timezone.utc),
    )

    assert reclaimed == []
    assert config.labels.running in github.get_issue(5).labels


def test_reclaim_ttl_disabled_keeps_live_pid_even_when_started_at_old() -> None:
    """ttl_seconds=None(默认)→ 即使有 started_at,PID 活就不 reclaim。"""
    config = AppConfig()
    github = FakeGitHubClient()
    started = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
    _seed_running_issue(
        github,
        config,
        issue_number=5,
        marker=format_claim_marker("host-a", os.getpid(), started_at=started),
    )

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: True,
        ttl_seconds=None,  # 默认关闭
        now=datetime(2026, 7, 4, 20, 0, 0, tzinfo=timezone.utc),
    )

    assert reclaimed == []
    assert config.labels.running in github.get_issue(5).labels


def test_reclaim_ttl_still_reclaims_dead_pid() -> None:
    """TTL 路径不阻塞原 dead_pid 路径:PID 死了、started_at 任意 → reclaim。"""
    config = AppConfig()
    github = FakeGitHubClient()
    started = datetime(2026, 7, 4, 8, 0, 0, tzinfo=timezone.utc)
    _seed_running_issue(
        github,
        config,
        issue_number=5,
        marker=format_claim_marker("host-a", 999999, started_at=started),
    )

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: False,  # PID 已死
        ttl_seconds=3600,
        now=started + timedelta(seconds=10),  # TTL 还没到
    )

    assert reclaimed == [5]
    comment_bodies = github.list_issue_comments(5)
    assert any("dead_pid" in body for body in comment_bodies)


def test_parse_claim_marker_body_invalid_iso_falls_back_to_none() -> None:
    """started_at 解析失败时不影响 host/pid 解析,started_at 返回 None。"""
    bad = '<!-- iar:claim host="host-a" pid="4242" started_at="not-iso" -->'
    assert parse_claim_marker_body(f"## x\n{bad}") == ("host-a", 4242, None)
