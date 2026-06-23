"""JSON-backed loop state store.

Stores the registered loops in ``~/.iar/loop-state.json`` so that the schedule
metadata is operator-scoped (not repository-scoped) and easy to inspect. The
file is read once and cached in memory; mutations re-serialize the full file
atomically via ``os.replace`` so concurrent readers never observe a partial
write.

All filesystem I/O is explicit ``encoding="utf-8"`` and the file is written
with restrictive permissions (``0o600``) because the file is operator-scoped
and may contain personal scheduling metadata.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.shared.interfaces.loop_scheduler import ILoopStateStore
from backend.core.shared.models.loop import (
    LoopSchedule,
    LoopScheduleKind,
    LoopTask,
)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def resolve_loop_state_path() -> Path:
    """Return the canonical location of the loop state file."""
    return Path.home() / ".iar" / "loop-state.json"


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _task_from_dict(payload: dict[str, Any]) -> LoopTask:
    """Decode a single task entry from the on-disk JSON format."""
    schedule_payload = payload.get("schedule", {})
    schedule_kind = LoopScheduleKind(schedule_payload.get("kind", "cron"))
    schedule = LoopSchedule(
        kind=schedule_kind,
        expression=str(schedule_payload.get("expression", "")),
    )
    recipe_path_text = payload.get("recipe_path", "")
    recipe_path = Path(recipe_path_text) if recipe_path_text else Path()
    labels_payload = payload.get("labels") or ()
    return LoopTask(
        id=str(payload.get("id", "")),
        recipe_path=recipe_path,
        repo_id=str(payload.get("repo_id", "")),
        schedule=schedule,
        enabled=bool(payload.get("enabled", True)),
        created_at=str(payload.get("created_at", "")),
        last_fire_at=payload.get("last_fire_at"),
        next_fire_at=payload.get("next_fire_at"),
        fire_count=int(payload.get("fire_count", 0)),
        last_error=payload.get("last_error"),
        priority=str(payload.get("priority", "P2")),
        slug=payload.get("slug"),
        issue_type=str(payload.get("issue_type", "feature")),
        agent=str(payload.get("agent", "auto")),
        labels=tuple(str(label) for label in labels_payload),
        publish_prd=bool(payload.get("publish_prd", True)),
        queue_ready=bool(payload.get("queue_ready", True)),
        run_now=bool(payload.get("run_now", False)),
        pre_command=payload.get("pre_command"),
        timezone_name=payload.get("timezone_name"),
    )


def _task_to_dict(task: LoopTask) -> dict[str, Any]:
    """Serialize a loop task into the on-disk JSON format."""
    return {
        "id": task.id,
        "recipe_path": str(task.recipe_path),
        "repo_id": task.repo_id,
        "schedule": {
            "kind": task.schedule.kind.value,
            "expression": task.schedule.expression,
        },
        "enabled": task.enabled,
        "created_at": task.created_at,
        "last_fire_at": task.last_fire_at,
        "next_fire_at": task.next_fire_at,
        "fire_count": task.fire_count,
        "last_error": task.last_error,
        "priority": task.priority,
        "slug": task.slug,
        "issue_type": task.issue_type,
        "agent": task.agent,
        "labels": list(task.labels),
        "publish_prd": task.publish_prd,
        "queue_ready": task.queue_ready,
        "run_now": task.run_now,
        "pre_command": task.pre_command,
        "timezone_name": task.timezone_name,
    }


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


class JsonLoopStateStore(ILoopStateStore):
    """Persist loops to a JSON file under ``~/.iar/loop-state.json``.

    The store keeps an in-memory cache that is lazily populated on first
    access and refreshed by :meth:`load`. Concurrent writers are tolerated
    only at the granularity of full-file replacement; the MVP warns about
    this in the user-facing guide.
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self._state_path: Path = (state_path or resolve_loop_state_path()).resolve()
        self._tasks_by_id: dict[str, LoopTask] = {}
        self._loaded: bool = False

    @property
    def state_path(self) -> Path:
        """Return the on-disk path this store writes to."""
        return self._state_path

    def load(self) -> None:
        """Refresh the in-memory cache from disk.

        Missing files are treated as an empty state. Files with malformed
        JSON raise ``ValueError`` so the CLI surfaces the corruption rather
        than silently discarding the user's loops.
        """
        if not self._state_path.exists():
            self._tasks_by_id = {}
            self._loaded = True
            return
        with open(self._state_path, encoding="utf-8") as state_file:
            payload = json.load(state_file)
        if not isinstance(payload, dict):
            raise ValueError(
                f"Loop state file {self._state_path} is not a JSON object."
            )
        raw_tasks = payload.get("tasks", [])
        if not isinstance(raw_tasks, list):
            raise ValueError(
                f"Loop state file {self._state_path} has non-list 'tasks'."
            )
        tasks: dict[str, LoopTask] = {}
        for raw_task in raw_tasks:
            if not isinstance(raw_task, dict):
                continue
            try:
                task = _task_from_dict(raw_task)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Loop state file {self._state_path} contains an invalid "
                    f"task entry: {exc}"
                ) from exc
            if task.id:
                tasks[task.id] = task
        self._tasks_by_id = tasks
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def list_tasks(self) -> list[LoopTask]:
        """Return all registered tasks sorted by id for stable output."""
        self._ensure_loaded()
        return [self._tasks_by_id[task_id] for task_id in sorted(self._tasks_by_id)]

    def get_task(self, loop_id: str) -> LoopTask | None:
        """Return the task with the given id or ``None`` if missing."""
        self._ensure_loaded()
        return self._tasks_by_id.get(loop_id)

    def upsert_task(self, task: LoopTask) -> None:
        """Insert or replace the task entry and persist to disk."""
        self._ensure_loaded()
        self._tasks_by_id[task.id] = task
        self._flush()

    def delete_task(self, loop_id: str) -> bool:
        """Remove a task by id, returning whether anything was removed."""
        self._ensure_loaded()
        removed = self._tasks_by_id.pop(loop_id, None)
        if removed is None:
            return False
        self._flush()
        return True

    def _flush(self) -> None:
        """Write the in-memory cache to disk atomically."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        ordered_tasks = [
            self._tasks_by_id[task_id] for task_id in sorted(self._tasks_by_id)
        ]
        payload = {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tasks": [_task_to_dict(task) for task in ordered_tasks],
        }
        tmp_path = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as tmp_file:
            json.dump(payload, tmp_file, indent=2, ensure_ascii=False)
            tmp_file.write("\n")
        os.replace(tmp_path, self._state_path)
        try:
            os.chmod(self._state_path, 0o600)
        except OSError:
            # Best effort — some platforms / filesystems do not support chmod.
            pass
