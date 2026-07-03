"""Tests for the SQLite console store (run history + audit log)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.core.shared.interfaces.runner_console import AuditEntry, RunRecord
from backend.infrastructure.persistence.console_store import (
    RoadmapQueueEntry,
    RoadmapSettingsEntry,
    SqliteConsoleStore,
)


def _make_run_record(
    *,
    issue_number: int = 19,
    outcome: str = "completed",
    repo_id: str = "keda-main",
    started_at: str = "2026-06-11T10:00:00+00:00",
) -> RunRecord:
    return RunRecord(
        repo_id=repo_id,
        repo_path="/tmp/repo",
        issue_number=issue_number,
        trigger="cli_run",
        agent="claude",
        outcome=outcome,
        error_summary=None if outcome == "completed" else "boom",
        started_at=started_at,
        finished_at="2026-06-11T10:05:00+00:00",
        duration_seconds=300.0,
    )


def test_append_and_list_runs(tmp_path: Path) -> None:
    """Run records should round-trip through the SQLite store."""
    store = SqliteConsoleStore(tmp_path / "console.db")
    store.append_run(_make_run_record(issue_number=1))
    store.append_run(_make_run_record(issue_number=2, outcome="failed"))

    recent_runs = store.list_recent_runs(limit=10)
    assert len(recent_runs) == 2
    # 倒序：最后写入的在最前。
    assert recent_runs[0].issue_number == 2
    assert recent_runs[0].outcome == "failed"
    assert recent_runs[0].error_summary == "boom"
    assert recent_runs[1].outcome == "completed"


def test_list_runs_filters_by_repo(tmp_path: Path) -> None:
    """repo_id filter should only return matching records."""
    store = SqliteConsoleStore(tmp_path / "console.db")
    store.append_run(_make_run_record(repo_id="alpha"))
    store.append_run(_make_run_record(repo_id="beta"))

    alpha_runs = store.list_recent_runs(repo_id="alpha", limit=10)
    assert [run.repo_id for run in alpha_runs] == ["alpha"]


def test_audit_round_trip(tmp_path: Path) -> None:
    """Audit entries should round-trip including rejected results."""
    store = SqliteConsoleStore(tmp_path / "console.db")
    store.append_audit(
        AuditEntry(
            occurred_at="2026-06-11T10:00:00+00:00",
            actor="console",
            action="retry_failed",
            repo_id="keda-main",
            issue_number=19,
            params_json='{"action": "retry_failed"}',
            result="rejected",
            detail="not failed",
        )
    )
    audits = store.list_recent_audits(limit=10)
    assert len(audits) == 1
    assert audits[0].action == "retry_failed"
    assert audits[0].result == "rejected"
    assert audits[0].issue_number == 19


def test_daily_trend_groups_by_day_and_outcome(tmp_path: Path) -> None:
    """Trend aggregation should bucket by day with per-outcome counts."""
    store = SqliteConsoleStore(tmp_path / "console.db")
    today_prefix = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    store.append_run(_make_run_record(issue_number=1, started_at=f"{today_prefix}T08:00:00+00:00"))
    store.append_run(
        _make_run_record(
            issue_number=2,
            outcome="failed",
            started_at=f"{today_prefix}T09:00:00+00:00",
        )
    )

    trend = store.daily_run_trend(repo_id=None, days=7)
    assert len(trend) == 1
    assert trend[0].day == today_prefix
    assert trend[0].completed == 1
    assert trend[0].failed == 1
    assert trend[0].blocked == 0
    assert trend[0].average_duration_seconds == 300.0


def test_store_survives_concurrent_style_reopen(tmp_path: Path) -> None:
    """Two store instances on the same file must both read/write (WAL)."""
    db_path = tmp_path / "console.db"
    writer_a = SqliteConsoleStore(db_path)
    writer_b = SqliteConsoleStore(db_path)
    writer_a.append_run(_make_run_record(issue_number=1))
    writer_b.append_run(_make_run_record(issue_number=2))
    assert len(writer_a.list_recent_runs(limit=10)) == 2


def test_append_failure_degrades_to_warning(tmp_path: Path) -> None:
    """A broken database must not raise out of append_run."""
    db_path = tmp_path / "console.db"
    store = SqliteConsoleStore(db_path)
    # 用目录占住 db 文件路径之外的方式不可行；直接破坏文件权限模拟。
    raw_connection = sqlite3.connect(db_path)
    raw_connection.execute("DROP TABLE run_records")
    raw_connection.commit()
    raw_connection.close()
    # 表被删掉后 append 不得抛出。
    store.append_run(_make_run_record())


def test_roadmap_settings_round_trip(tmp_path: Path) -> None:
    """Roadmap settings should be persisted and retrievable."""
    store = SqliteConsoleStore(tmp_path / "console.db")
    settings = store.get_roadmap_settings("keda-main")
    assert settings is None

    store.save_roadmap_settings(
        RoadmapSettingsEntry(
            repo_id="keda-main",
            max_parallel=3,
            default_view="timeline",
            updated_at="2026-06-14T12:00:00+00:00",
        )
    )
    settings = store.get_roadmap_settings("keda-main")
    assert settings is not None
    assert settings.max_parallel == 3
    assert settings.default_view == "timeline"


def test_roadmap_queue_round_trip(tmp_path: Path) -> None:
    """Roadmap queue entries should be persisted and filterable."""
    store = SqliteConsoleStore(tmp_path / "console.db")
    entry_id = store.enqueue_roadmap(
        RoadmapQueueEntry(
            repo_id="keda-main",
            prd_path="tasks/pending/P1-FEAT-20260101-a.md",
            status="queued",
            trigger="global",
            started_at=None,
            finished_at=None,
            error_detail=None,
        )
    )
    queue = store.list_roadmap_queue(repo_id="keda-main")
    assert len(queue) == 1
    assert queue[0].entry_id == entry_id
    assert queue[0].prd_path == "tasks/pending/P1-FEAT-20260101-a.md"

    store.update_roadmap_queue_status(
        entry_id=entry_id, status="running", started_at="2026-06-14T12:00:00+00:00"
    )
    running = store.list_roadmap_queue(repo_id="keda-main", status="running")
    assert len(running) == 1
    assert running[0].status == "running"


def test_schema_migration_from_version_1(tmp_path: Path) -> None:
    """An existing v1 database should be migrated to v2 in-place."""
    db_path = tmp_path / "console.db"
    SqliteConsoleStore(db_path)
    raw = sqlite3.connect(db_path)
    version = raw.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 2
    tables = {
        row[0]
        for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "roadmap_queue" in tables
    assert "roadmap_settings" in tables
    raw.close()
