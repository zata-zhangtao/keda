"""Tests for the agent runner daemon main loop, focused on TTL reclaim wiring."""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from backend.core.shared.models.agent_runner import (
    AppConfig,
    RepositoryRunContext,
)
from backend.core.use_cases.run_agent_daemon import run_agent_daemon
from backend.core.use_cases.agent_runner_reclaim import format_claim_marker
from tests.conftest import FakeGitHubClient


def _build_context(repo_id: str, repo_path: Path) -> RepositoryRunContext:
    """Build a minimal RepositoryRunContext for daemon tests."""
    return RepositoryRunContext(
        repo_id=repo_id,
        repo_path=repo_path,
        display_name=repo_id,
        config=AppConfig(),
    )


def test_daemon_reclaim_ttl_expired_calls_reclaim(monkeypatch, tmp_path: Path) -> None:
    """daemon TTL reclaim:old claim + live PID → reclaim 触发 + 进入 ready。

    通过 monkeypatch ``time.sleep`` 让 daemon 只跑一轮就跑出 while True。
    """
    from backend.core.use_cases import run_agent_daemon as daemon_module

    sleep_calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        sleep_calls["n"] += 1
        # 第一轮 reclaim 跑完后跳出 while 循环,防止无限循环。
        if sleep_calls["n"] >= 1:
            raise StopIteration

    monkeypatch.setattr(daemon_module.time, "sleep", fake_sleep)

    # 阻止后续 phase(prd_rework / run_once)做任何事:抛错让 daemon 记录后继续。
    # daemon 通过 ``from ... import`` 引入,得 monkeypatch daemon 模块本身的引用。

    def boom(*_args, **_kwargs):  # noqa: ANN001 - signature mirrors run_once
        raise RuntimeError("stop after reclaim for test")

    monkeypatch.setattr(daemon_module, "run_once", boom)
    monkeypatch.setattr(daemon_module, "process_prd_rework_issues", boom)

    config = AppConfig()
    github = FakeGitHubClient()
    started = datetime(2026, 7, 4, 8, 0, 0, tzinfo=timezone.utc)
    this_host = socket.gethostname()
    github.edit_issue_labels(7, add=[config.labels.running])
    github.comment_issue(
        7,
        f"## Agent Runner Claimed\n{format_claim_marker(this_host, os.getpid(), started_at=started)}",
    )
    github.set_list_issues_by_label_result([github.get_issue(7)])

    # daemon 自己 PID 仍 alive;TTL=3600s,now 比 started 晚 2h,触发 reclaim。
    with __import__("pytest").raises(StopIteration):
        run_agent_daemon(
            contexts=[_build_context("ttl-test", tmp_path)],
            interval=1,
            agent="claude",
            max_issues=1,
            process_runner=None,  # type: ignore[arg-type]  # 未触发
            github_client_factory=lambda _repo_path: github,
            reclaim_stale_running=True,
            reclaim_ttl_seconds=3600,
            # time.sleep 用全局时钟不感知 now;reclaim 内部用 datetime.now 直接读系统时间。
        )

    # DEBUG: 直接调用 reclaim 看是否真的能 reclaim 这条 issue。
    # from backend.core.use_cases.agent_runner_reclaim import (
    #     reclaim_stale_running_issues as _reclaim,
    # )

    # print("\n[DEBUG] post-daemon labels:", github.get_issue(7).labels)
    # print("[DEBUG] post-daemon reclaim direct call:", _reclaim(
    #     config=AppConfig(),
    #     github_client=github,
    #     host="host-a",
    #     pid_alive=lambda _pid: True,
    #     ttl_seconds=3600,
    # ))
    # print("[DEBUG] post-direct labels:", github.get_issue(7).labels)
    # print("[DEBUG] comments:", github.list_issue_comments(7))

    labels = github.get_issue(7).labels
    assert config.labels.ready in labels
    assert config.labels.running not in labels
    comment_bodies = github.list_issue_comments(7)
    assert any("ttl_expired" in body for body in comment_bodies)


def test_daemon_reclaim_disabled_keeps_live_pid_old_claim(monkeypatch, tmp_path: Path) -> None:
    """daemon reclaim_stale_running=False → 老 issue 不动(向后兼容)。"""
    from backend.core.use_cases import run_agent_daemon as daemon_module

    def fake_sleep(_seconds: float) -> None:
        raise StopIteration

    monkeypatch.setattr(daemon_module.time, "sleep", fake_sleep)

    def boom(*_args, **_kwargs):  # noqa: ANN001
        raise RuntimeError("stop")

    monkeypatch.setattr(daemon_module, "run_once", boom)
    monkeypatch.setattr(daemon_module, "process_prd_rework_issues", boom)

    config = AppConfig()
    github = FakeGitHubClient()
    started = datetime(2026, 7, 4, 8, 0, 0, tzinfo=timezone.utc)
    this_host = socket.gethostname()
    github.edit_issue_labels(7, add=[config.labels.running])
    github.comment_issue(
        7,
        f"## Claimed\n{format_claim_marker(this_host, os.getpid(), started_at=started)}",
    )
    github.set_list_issues_by_label_result([github.get_issue(7)])

    with __import__("pytest").raises(StopIteration):
        run_agent_daemon(
            contexts=[_build_context("ttl-test", tmp_path)],
            interval=1,
            agent="claude",
            max_issues=1,
            process_runner=None,  # type: ignore[arg-type]
            github_client_factory=lambda _repo_path: github,
            reclaim_stale_running=False,
            reclaim_ttl_seconds=3600,
        )

    assert config.labels.running in github.get_issue(7).labels
    assert config.labels.ready not in github.get_issue(7).labels
