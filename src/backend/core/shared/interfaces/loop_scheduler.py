"""Loop subsystem ports.

The loop use cases depend on these abstract ports so the core layer never
imports ``infrastructure``. The JSON-backed state store and the wall-clock
implementation live in ``backend.engines.agent_runner.persistence.loop_state_json``
and ``backend.engines.agent_runner.scheduler.loop_clock`` respectively.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from backend.core.shared.models.loop import LoopTask


class ILoopStateStore(ABC):
    """Persist and query registered loops.

    Implementations are responsible for atomic writes to durable storage.
    The default JSON implementation writes to ``~/.iar/loop-state.json``.
    """

    @abstractmethod
    def list_tasks(self) -> list[LoopTask]:
        """Return all registered tasks, regardless of enabled state."""

    @abstractmethod
    def get_task(self, loop_id: str) -> LoopTask | None:
        """Return the task with the given ``loop_id`` or ``None`` if missing."""

    @abstractmethod
    def upsert_task(self, task: LoopTask) -> None:
        """Insert or replace a task entry."""

    @abstractmethod
    def delete_task(self, loop_id: str) -> bool:
        """Remove a task by id, returning whether anything was removed."""

    @abstractmethod
    def load(self) -> None:
        """Refresh the in-memory view from durable storage.

        Most state stores are file-backed and lazily cached; this method
        allows callers to bypass the cache (e.g. after manual edits) and
        is safe to call when no cache exists.
        """


class ILoopClock(ABC):
    """Wall-clock abstraction so tests can pin time.

    Production wiring uses ``SystemClock``; tests substitute a ``FixedClock``
    that returns a predetermined timestamp.
    """

    @abstractmethod
    def now(self) -> datetime:
        """Return the current local-aware datetime."""

    @abstractmethod
    def sleep_seconds(self, seconds: float) -> None:
        """Sleep for the given number of seconds.

        Real clocks should delegate to ``time.sleep``; fixed clocks used in
        tests can advance their internal cursor instead.
        """
