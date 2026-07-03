"""管理终端运行历史与审计的本地 SQLite 存储。

设计要点：

- 使用 stdlib ``sqlite3`` 而非 SQLAlchemy/alembic：CLI 直跑 ``iar run``
  也要写运行记录，不能要求 PostgreSQL 常驻；本地单文件零依赖。
- WAL + busy_timeout 容忍多个 runner 进程并发收尾写库。
- 通过 ``PRAGMA user_version`` 做就地迁移（当前版本 2）。
- 任何写入失败都不允许向上抛出阻断 runner 主流程，降级为日志警告。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)


# 以下数据类与 core/shared/interfaces/runner_console.py 中的同名类型
# 结构一致（鸭子类型实现端口），infrastructure 层禁止导入 core。


@dataclass(frozen=True)
class RunRecord:
    """一次 Issue 处理的运行结果（与 core 同构）。"""

    repo_id: str
    repo_path: str
    issue_number: int
    trigger: str
    agent: str
    outcome: str
    error_summary: str | None
    started_at: str
    finished_at: str
    duration_seconds: float


@dataclass(frozen=True)
class AttemptRecord:
    """一次 agent execution attempt 的本地记录（与 core 同构）。"""

    repo_id: str
    issue_number: int
    agent: str
    attempt_number: int
    failure_type: str
    recovered: bool
    detail: str
    started_at: str
    finished_at: str
    duration_seconds: float


@dataclass(frozen=True)
class AuditEntry:
    """一次管理终端写操作的审计条目（与 core 同构）。"""

    occurred_at: str
    actor: str
    action: str
    repo_id: str | None
    issue_number: int | None
    params_json: str
    result: str
    detail: str | None


@dataclass(frozen=True)
class DailyRunTrendEntry:
    """运行历史按天聚合的一个数据点（与 core 同构）。"""

    day: str
    completed: int
    failed: int
    blocked: int
    average_duration_seconds: float | None


_SCHEMA_VERSION = 3

_CREATE_RUN_RECORDS = """
CREATE TABLE IF NOT EXISTS run_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    agent TEXT NOT NULL,
    outcome TEXT NOT NULL,
    error_summary TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL
)
"""

_CREATE_ATTEMPT_RECORDS = """
CREATE TABLE IF NOT EXISTS attempt_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    agent TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    failure_type TEXT NOT NULL,
    recovered INTEGER NOT NULL,
    detail TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_AUDIT_LOGS = """
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    repo_id TEXT,
    issue_number INTEGER,
    params_json TEXT NOT NULL,
    result TEXT NOT NULL,
    detail TEXT
)
"""

_CREATE_ROADMAP_QUEUE = """
CREATE TABLE IF NOT EXISTS roadmap_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL,
    prd_path TEXT NOT NULL,
    status TEXT NOT NULL,
    trigger TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_detail TEXT
)
"""

_CREATE_ROADMAP_SETTINGS = """
CREATE TABLE IF NOT EXISTS roadmap_settings (
    repo_id TEXT PRIMARY KEY,
    max_parallel INTEGER NOT NULL,
    default_view TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class SqliteConsoleStore:
    """``IRunHistoryStore`` 与 ``IRoadmapStore`` 端口的 SQLite 实现（鸭子类型）。"""

    def __init__(self, db_path: str | Path) -> None:
        """初始化存储并确保 schema 就绪。

        Args:
            db_path: SQLite 文件路径，支持 ``~`` 展开。
        """
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._migrate(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._db_path), timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.row_factory = sqlite3.Row
        return connection

    def _migrate(self, connection: sqlite3.Connection) -> None:
        current_version_row = connection.execute("PRAGMA user_version").fetchone()
        current_version = int(current_version_row[0])
        if current_version >= _SCHEMA_VERSION:
            return
        if current_version < 1:
            connection.execute(_CREATE_RUN_RECORDS)
            connection.execute(_CREATE_AUDIT_LOGS)
        if current_version < 2:
            connection.execute(_CREATE_ROADMAP_QUEUE)
            connection.execute(_CREATE_ROADMAP_SETTINGS)
        if current_version < 3:
            connection.execute(_CREATE_ATTEMPT_RECORDS)
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        connection.commit()

    def append_run(self, run_record: RunRecord) -> None:
        """追加运行记录；失败时降级为日志警告，不阻断 runner。"""
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO run_records "
                    "(repo_id, repo_path, issue_number, trigger, agent, outcome, "
                    " error_summary, started_at, finished_at, duration_seconds) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_record.repo_id,
                        run_record.repo_path,
                        run_record.issue_number,
                        run_record.trigger,
                        run_record.agent,
                        run_record.outcome,
                        run_record.error_summary,
                        run_record.started_at,
                        run_record.finished_at,
                        run_record.duration_seconds,
                    ),
                )
                connection.commit()
        except Exception as exc:  # noqa: BLE001 - side-channel must not break runs.
            _logger.warning("Failed to append run record to %s: %s", self._db_path, exc)

    def append_attempt(self, attempt_record: AttemptRecord) -> None:
        """追加 attempt 记录；失败时降级为日志警告，不阻断 runner。"""
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO attempt_records "
                    "(repo_id, issue_number, agent, attempt_number, failure_type, "
                    " recovered, detail, started_at, finished_at, duration_seconds) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        attempt_record.repo_id,
                        attempt_record.issue_number,
                        attempt_record.agent,
                        attempt_record.attempt_number,
                        attempt_record.failure_type,
                        int(attempt_record.recovered),
                        attempt_record.detail,
                        attempt_record.started_at,
                        attempt_record.finished_at,
                        attempt_record.duration_seconds,
                    ),
                )
                connection.commit()
        except Exception as exc:  # noqa: BLE001 - side-channel must not break runs.
            _logger.warning("Failed to append attempt record to %s: %s", self._db_path, exc)

    def append_audit(self, audit_entry: AuditEntry) -> None:
        """追加审计条目；失败时降级为日志警告。"""
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO audit_logs "
                    "(occurred_at, actor, action, repo_id, issue_number, "
                    " params_json, result, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        audit_entry.occurred_at,
                        audit_entry.actor,
                        audit_entry.action,
                        audit_entry.repo_id,
                        audit_entry.issue_number,
                        audit_entry.params_json,
                        audit_entry.result,
                        audit_entry.detail,
                    ),
                )
                connection.commit()
        except Exception as exc:  # noqa: BLE001 - side-channel must not break actions.
            _logger.warning("Failed to append audit entry to %s: %s", self._db_path, exc)

    def list_recent_runs(self, *, repo_id: str | None = None, limit: int = 100) -> list[RunRecord]:
        """倒序列出最近的运行记录。"""
        query = (
            "SELECT repo_id, repo_path, issue_number, trigger, agent, outcome, "
            "error_summary, started_at, finished_at, duration_seconds "
            "FROM run_records"
        )
        query_params: list[object] = []
        if repo_id is not None:
            query += " WHERE repo_id = ?"
            query_params.append(repo_id)
        query += " ORDER BY id DESC LIMIT ?"
        query_params.append(limit)
        with self._connect() as connection:
            record_rows = connection.execute(query, query_params).fetchall()
        return [
            RunRecord(
                repo_id=record_row["repo_id"],
                repo_path=record_row["repo_path"],
                issue_number=int(record_row["issue_number"]),
                trigger=record_row["trigger"],
                agent=record_row["agent"],
                outcome=record_row["outcome"],
                error_summary=record_row["error_summary"],
                started_at=record_row["started_at"],
                finished_at=record_row["finished_at"],
                duration_seconds=float(record_row["duration_seconds"]),
            )
            for record_row in record_rows
        ]

    def list_recent_audits(self, *, limit: int = 100) -> list[AuditEntry]:
        """倒序列出最近的审计条目。"""
        with self._connect() as connection:
            audit_rows = connection.execute(
                "SELECT occurred_at, actor, action, repo_id, issue_number, "
                "params_json, result, detail "
                "FROM audit_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            AuditEntry(
                occurred_at=audit_row["occurred_at"],
                actor=audit_row["actor"],
                action=audit_row["action"],
                repo_id=audit_row["repo_id"],
                issue_number=(
                    int(audit_row["issue_number"])
                    if audit_row["issue_number"] is not None
                    else None
                ),
                params_json=audit_row["params_json"],
                result=audit_row["result"],
                detail=audit_row["detail"],
            )
            for audit_row in audit_rows
        ]

    def daily_run_trend(self, *, repo_id: str | None, days: int) -> list[DailyRunTrendEntry]:
        """按天聚合最近 ``days`` 天的运行结果。"""
        query = (
            "SELECT date(started_at) AS day, "
            "SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END) AS completed, "
            "SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN outcome = 'blocked' THEN 1 ELSE 0 END) AS blocked, "
            "AVG(duration_seconds) AS average_duration_seconds "
            "FROM run_records "
            "WHERE date(started_at) >= date('now', ?)"
        )
        query_params: list[object] = [f"-{max(days, 1)} days"]
        if repo_id is not None:
            query += " AND repo_id = ?"
            query_params.append(repo_id)
        query += " GROUP BY day ORDER BY day ASC"
        with self._connect() as connection:
            trend_rows = connection.execute(query, query_params).fetchall()
        return [
            DailyRunTrendEntry(
                day=trend_row["day"],
                completed=int(trend_row["completed"] or 0),
                failed=int(trend_row["failed"] or 0),
                blocked=int(trend_row["blocked"] or 0),
                average_duration_seconds=(
                    float(trend_row["average_duration_seconds"])
                    if trend_row["average_duration_seconds"] is not None
                    else None
                ),
            )
            for trend_row in trend_rows
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # Roadmap queue / settings (IRoadmapStore duck-type implementation)
    # ─────────────────────────────────────────────────────────────────────────

    def get_roadmap_settings(self, repo_id: str) -> RoadmapSettingsEntry | None:
        """读取指定仓库的 roadmap 设置。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT repo_id, max_parallel, default_view, updated_at "
                "FROM roadmap_settings WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
        if row is None:
            return None
        return RoadmapSettingsEntry(
            repo_id=row["repo_id"],
            max_parallel=int(row["max_parallel"]),
            default_view=row["default_view"],
            updated_at=row["updated_at"],
        )

    def save_roadmap_settings(self, settings: RoadmapSettingsEntry) -> None:
        """保存或更新 roadmap 设置；失败时抛出异常。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO roadmap_settings (repo_id, max_parallel, default_view, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(repo_id) DO UPDATE SET "
                "max_parallel=excluded.max_parallel, default_view=excluded.default_view, updated_at=excluded.updated_at",
                (
                    settings.repo_id,
                    settings.max_parallel,
                    settings.default_view,
                    settings.updated_at,
                ),
            )
            connection.commit()

    def enqueue_roadmap(self, entry: RoadmapQueueEntry) -> int:
        """将 PRD 加入 roadmap 队列，返回自增 ID；失败时抛出异常。"""
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO roadmap_queue (repo_id, prd_path, status, trigger, started_at, finished_at, error_detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.repo_id,
                    entry.prd_path,
                    entry.status,
                    entry.trigger,
                    entry.started_at,
                    entry.finished_at,
                    entry.error_detail,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def list_roadmap_queue(
        self, *, repo_id: str | None = None, status: str | None = None
    ) -> list[RoadmapQueueEntry]:
        """列出 roadmap 队列条目。"""
        query = (
            "SELECT id, repo_id, prd_path, status, trigger, started_at, finished_at, error_detail "
            "FROM roadmap_queue"
        )
        conditions: list[str] = []
        params: list[object] = []
        if repo_id is not None:
            conditions.append("repo_id = ?")
            params.append(repo_id)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id ASC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            RoadmapQueueEntry(
                entry_id=int(row["id"]),
                repo_id=row["repo_id"],
                prd_path=row["prd_path"],
                status=row["status"],
                trigger=row["trigger"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                error_detail=row["error_detail"],
            )
            for row in rows
        ]

    def update_roadmap_queue_status(
        self,
        *,
        entry_id: int,
        status: str,
        started_at: str | None = None,
        finished_at: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """更新队列条目的状态；失败时抛出异常。"""
        with self._connect() as connection:
            connection.execute(
                "UPDATE roadmap_queue SET status = ?, started_at = ?, finished_at = ?, error_detail = ? "
                "WHERE id = ?",
                (status, started_at, finished_at, error_detail, entry_id),
            )
            connection.commit()

    def clear_roadmap_queue(self, *, repo_id: str | None = None) -> None:
        """清空 roadmap 队列；失败时抛出异常。"""
        with self._connect() as connection:
            if repo_id is None:
                connection.execute("DELETE FROM roadmap_queue")
            else:
                connection.execute("DELETE FROM roadmap_queue WHERE repo_id = ?", (repo_id,))
            connection.commit()


@dataclass(frozen=True)
class RoadmapQueueEntry:
    """roadmap 队列条目（与 core 侧同构，供 SQLite 实现使用）。"""

    repo_id: str
    prd_path: str
    status: str
    trigger: str
    started_at: str | None
    finished_at: str | None
    error_detail: str | None
    entry_id: int | None = None


@dataclass(frozen=True)
class RoadmapSettingsEntry:
    """roadmap 用户设置（与 core 侧同构，供 SQLite 实现使用）。"""

    repo_id: str
    max_parallel: int
    default_view: str
    updated_at: str


def summarize_params(params: dict) -> str:
    """将动作参数序列化为审计用 JSON 字符串。"""
    return json.dumps(params, ensure_ascii=False, sort_keys=True)
