"""Concrete ILoopClock implementations.

The default :class:`SystemClock` delegates to :func:`datetime.now` and
``time.sleep``; :class:`FixedClock` is a deterministic clock useful in unit
tests that need to advance time by a controlled amount.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from backend.core.shared.interfaces.loop_scheduler import ILoopClock


class SystemClock(ILoopClock):
    """Production clock: wall time + real sleeps."""

    def now(self) -> datetime:
        """Return the current wall-clock time (local time zone, UTC offset)."""
        return datetime.now().astimezone()

    def sleep_seconds(self, seconds: float) -> None:
        """Sleep for the given number of seconds."""
        if seconds > 0:
            time.sleep(seconds)


class FixedClock(ILoopClock):
    """Deterministic clock for tests.

    The clock can be advanced with :meth:`advance`. ``sleep_seconds`` does
    not actually block; it advances the cursor so tests can simulate the
    passage of time without slowing down the suite.
    """

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        elif start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._current: datetime = start

    def now(self) -> datetime:
        """Return the current cursor (always tz-aware)."""
        return self._current

    def sleep_seconds(self, seconds: float) -> None:
        """Advance the cursor without blocking."""
        if seconds > 0:
            self._current = self._current + timedelta(seconds=seconds)

    def advance(self, *, seconds: float = 0, minutes: float = 0, hours: float = 0) -> None:
        """Advance the clock by the given offsets."""
        delta = timedelta(seconds=seconds, minutes=minutes, hours=hours)
        if delta.total_seconds() > 0:
            self._current = self._current + delta
