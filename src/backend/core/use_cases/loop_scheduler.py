"""Loop schedule evaluation.

Translates a :class:`LoopSchedule` (cron expression or interval) into the
next fire datetime using :mod:`croniter`, and provides helpers for deciding
which registered loops are due at a given clock tick.

This module deliberately depends only on :class:`LoopSchedule` /
:class:`LoopTask` value objects and the abstract :class:`ILoopClock`. It
must not import any infrastructure layer so the core use case stays
re-runnable in unit tests.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from croniter import croniter

from backend.core.shared.interfaces.loop_scheduler import ILoopClock
from backend.core.shared.models.loop import (
    LoopSchedule,
    LoopScheduleKind,
    LoopTask,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cron / interval parsing
# ---------------------------------------------------------------------------


_INTERVAL_PATTERN_TO_DELTA: dict[str, timedelta] = {
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}


def parse_interval_seconds(expression: str) -> int:
    """Convert a ``<number><unit>`` interval expression to seconds.

    Args:
        expression: Interval expression such as ``"10m"`` / ``"1h"`` /
            ``"1d"``. The unit must be one of ``m`` / ``h`` / ``d``.

    Returns:
        Number of seconds the interval represents.

    Raises:
        ValueError: When the expression is not a valid ``<number><unit>``
            interval token.
    """
    text = expression.strip()
    if len(text) < 2:
        raise ValueError(
            f"Invalid interval expression: {expression!r}. "
            "Expected '<number><unit>' (e.g. '10m', '1h', '1d')."
        )
    unit = text[-1]
    if unit not in _INTERVAL_PATTERN_TO_DELTA:
        raise ValueError(
            f"Invalid interval unit {unit!r} in {expression!r}. "
            "Use 'm' (minutes), 'h' (hours), or 'd' (days)."
        )
    value_text = text[:-1]
    try:
        magnitude = int(value_text)
    except ValueError as exc:
        raise ValueError(
            f"Invalid interval magnitude {value_text!r} in {expression!r}."
        ) from exc
    if magnitude <= 0:
        raise ValueError(
            f"Interval magnitude must be positive; got {magnitude} in {expression!r}."
        )
    return int(_INTERVAL_PATTERN_TO_DELTA[unit].total_seconds() * magnitude)


# ---------------------------------------------------------------------------
# Next fire computation
# ---------------------------------------------------------------------------


def _ensure_aware_utc(moment: datetime) -> datetime:
    """Return ``moment`` in UTC. Naive inputs are assumed to be local time."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def compute_next_fire(
    schedule: LoopSchedule,
    *,
    after: datetime,
    origin: datetime | None = None,
) -> datetime:
    """Compute the next fire datetime strictly after ``after``.

    Args:
        schedule: The loop schedule to evaluate.
        after: A timezone-aware UTC datetime. The next fire is strictly
            later than this point.
        origin: Optional starting point for interval schedules. Defaults to
            ``after`` when omitted.

    Returns:
        The next fire datetime in UTC.

    Raises:
        ValueError: When ``schedule`` cannot be evaluated (e.g. invalid cron).
    """
    after_utc = _ensure_aware_utc(after)
    if schedule.kind is LoopScheduleKind.CRON:
        try:
            iterator = croniter(schedule.expression, after_utc)
        except (ValueError, KeyError) as exc:
            raise ValueError(
                f"Invalid cron expression {schedule.expression!r}: {exc}"
            ) from exc
        return iterator.get_next(datetime).astimezone(timezone.utc)
    seconds = parse_interval_seconds(schedule.expression)
    base = _ensure_aware_utc(origin or after_utc)
    next_fire = base + timedelta(seconds=seconds)
    if next_fire <= after_utc:
        # ``after`` advanced past the next interval boundary; step forward.
        delta_seconds = (after_utc - base).total_seconds()
        steps = int(delta_seconds // seconds) + 1
        next_fire = base + timedelta(seconds=steps * seconds)
    return next_fire


# ---------------------------------------------------------------------------
# List due tasks
# ---------------------------------------------------------------------------


def list_due_tasks(
    state_tasks: list[LoopTask],
    *,
    clock: ILoopClock,
) -> list[LoopTask]:
    """Return enabled tasks whose next fire is at or before ``clock.now()``.

    Args:
        state_tasks: All known loop tasks (typically from
            :class:`ILoopStateStore`).
        clock: Wall-clock abstraction supplying the comparison timestamp.

    Returns:
        Subset of ``state_tasks`` that are due now. The original order of
        the input list is preserved.
    """
    now_utc = _ensure_aware_utc(clock.now())
    due: list[LoopTask] = []
    for task in state_tasks:
        if not task.enabled:
            continue
        if task.next_fire_at is None:
            continue
        try:
            next_fire_dt = datetime.fromisoformat(task.next_fire_at)
        except ValueError:
            _logger.warning(
                "Loop '%s' has unparsable next_fire_at=%r; skipping.",
                task.id,
                task.next_fire_at,
            )
            continue
        if _ensure_aware_utc(next_fire_dt) <= now_utc:
            due.append(task)
    return due


# ---------------------------------------------------------------------------
# Catch-up evaluation
# ---------------------------------------------------------------------------


def should_catch_up(
    task: LoopTask,
    *,
    clock: ILoopClock,
) -> bool:
    """Return True when the loop missed a fire and should run a single catch-up.

    The check is intentionally conservative: only one catch-up is performed
    per daemon start, no matter how many schedule slots were missed.

    Args:
        task: The loop task to inspect.
        clock: Wall-clock abstraction supplying the comparison timestamp.

    Returns:
        True when a catch-up fire is required.
    """
    if not task.enabled or task.last_fire_at is None or task.next_fire_at is None:
        return False
    try:
        last_fire_dt = datetime.fromisoformat(task.last_fire_at)
        next_fire_dt = datetime.fromisoformat(task.next_fire_at)
    except ValueError:
        return False
    now_utc = _ensure_aware_utc(clock.now())
    last_fire_utc = _ensure_aware_utc(last_fire_dt)
    next_fire_utc = _ensure_aware_utc(next_fire_dt)
    return last_fire_utc < next_fire_utc and next_fire_utc <= now_utc
