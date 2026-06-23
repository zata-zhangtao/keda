"""Tests for the loop scheduler and clock interfaces."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.core.shared.models.loop import (
    LoopSchedule,
    LoopScheduleKind,
    LoopTask,
)
from backend.core.use_cases.loop_scheduler import (
    compute_next_fire,
    list_due_tasks,
    parse_interval_seconds,
    should_catch_up,
)
from backend.engines.agent_runner.scheduler.loop_clock import FixedClock, SystemClock


# ---------------------------------------------------------------------------
# Interval parsing
# ---------------------------------------------------------------------------


def test_parse_interval_seconds_minutes() -> None:
    assert parse_interval_seconds("10m") == 600


def test_parse_interval_seconds_hours() -> None:
    assert parse_interval_seconds("1h") == 3600


def test_parse_interval_seconds_days() -> None:
    assert parse_interval_seconds("1d") == 86_400


def test_parse_interval_seconds_invalid_unit_raises() -> None:
    with pytest.raises(ValueError):
        parse_interval_seconds("10x")


def test_parse_interval_seconds_non_positive_raises() -> None:
    with pytest.raises(ValueError):
        parse_interval_seconds("0m")


def test_parse_interval_seconds_bad_magnitude_raises() -> None:
    with pytest.raises(ValueError):
        parse_interval_seconds("abm")


# ---------------------------------------------------------------------------
# Cron / interval next fire
# ---------------------------------------------------------------------------


def test_compute_next_fire_cron_5_field() -> None:
    schedule = LoopSchedule(kind=LoopScheduleKind.CRON, expression="0 8 * * *")
    after = datetime(2026, 6, 23, 7, 30, 0, tzinfo=timezone.utc)
    next_fire = compute_next_fire(schedule, after=after)
    assert next_fire.hour == 8
    assert next_fire.minute == 0
    assert next_fire.day == 23


def test_compute_next_fire_cron_rolls_to_next_day() -> None:
    schedule = LoopSchedule(kind=LoopScheduleKind.CRON, expression="0 8 * * *")
    after = datetime(2026, 6, 23, 9, 0, 0, tzinfo=timezone.utc)
    next_fire = compute_next_fire(schedule, after=after)
    assert next_fire.day == 24
    assert next_fire.hour == 8


def test_compute_next_fire_invalid_cron_raises() -> None:
    schedule = LoopSchedule(kind=LoopScheduleKind.CRON, expression="not a cron")
    with pytest.raises(ValueError):
        compute_next_fire(schedule, after=datetime(2026, 6, 23, tzinfo=timezone.utc))


def test_compute_next_fire_interval() -> None:
    schedule = LoopSchedule(kind=LoopScheduleKind.INTERVAL, expression="30m")
    after = datetime(2026, 6, 23, 7, 30, 0, tzinfo=timezone.utc)
    next_fire = compute_next_fire(schedule, after=after)
    assert (next_fire - after).total_seconds() == 30 * 60


def test_compute_next_fire_interval_with_origin() -> None:
    schedule = LoopSchedule(kind=LoopScheduleKind.INTERVAL, expression="1h")
    after = datetime(2026, 6, 23, 7, 0, 0, tzinfo=timezone.utc)
    origin = datetime(2026, 6, 23, 6, 0, 0, tzinfo=timezone.utc)
    next_fire = compute_next_fire(schedule, after=after, origin=origin)
    assert (next_fire - after).total_seconds() == 60 * 60


def test_compute_next_fire_interval_after_origin_steps() -> None:
    """When ``after`` is past the next slot, step forward to the next boundary."""
    schedule = LoopSchedule(kind=LoopScheduleKind.INTERVAL, expression="1h")
    origin = datetime(2026, 6, 23, 6, 0, 0, tzinfo=timezone.utc)
    after = datetime(2026, 6, 23, 8, 30, 0, tzinfo=timezone.utc)
    next_fire = compute_next_fire(schedule, after=after, origin=origin)
    # 8:30 is 2.5 hours after 6:00; next slot after that is 9:00.
    assert next_fire.hour == 9
    assert next_fire.minute == 0


def test_loop_schedule_daily_alias_maps_to_midnight_cron() -> None:
    schedule = LoopSchedule.from_expression("1d")
    assert schedule.kind is LoopScheduleKind.CRON
    assert schedule.expression == "0 0 * * *"


# ---------------------------------------------------------------------------
# list_due_tasks
# ---------------------------------------------------------------------------


def _make_task(
    *,
    loop_id: str = "demo",
    next_fire_at: str | None = None,
    enabled: bool = True,
) -> LoopTask:
    return LoopTask(
        id=loop_id,
        recipe_path=Path("/tmp/loop.md"),
        repo_id="keda-main",
        schedule=LoopSchedule(kind=LoopScheduleKind.CRON, expression="0 8 * * *"),
        enabled=enabled,
        next_fire_at=next_fire_at,
    )


def test_list_due_tasks_includes_only_overdue() -> None:
    tasks = [
        _make_task(loop_id="due", next_fire_at="2026-06-23T07:00:00+00:00"),
        _make_task(loop_id="future", next_fire_at="2026-06-23T09:00:00+00:00"),
        _make_task(loop_id="none", next_fire_at=None),
    ]
    clock = FixedClock(datetime(2026, 6, 23, 8, 0, 0, tzinfo=timezone.utc))
    due = list_due_tasks(tasks, clock=clock)
    assert [task.id for task in due] == ["due"]


def test_list_due_tasks_skips_disabled() -> None:
    tasks = [
        _make_task(
            loop_id="disabled", next_fire_at="2026-06-23T07:00:00+00:00", enabled=False
        )
    ]
    clock = FixedClock(datetime(2026, 6, 23, 8, 0, 0, tzinfo=timezone.utc))
    assert list_due_tasks(tasks, clock=clock) == []


def test_should_catch_up_only_when_a_slot_missed() -> None:
    task = _make_task(
        next_fire_at="2026-06-23T08:00:00+00:00",
    )
    # Simulate that the loop last fired at 7:00 and the 8:00 slot was missed.
    task = dataclasses.replace(task, last_fire_at="2026-06-23T07:00:00+00:00")
    clock = FixedClock(datetime(2026, 6, 23, 9, 0, 0, tzinfo=timezone.utc))
    assert should_catch_up(task, clock=clock) is True


def test_should_catch_up_false_when_already_fired() -> None:
    task = _make_task(
        next_fire_at="2026-06-23T08:00:00+00:00",
    )
    task = dataclasses.replace(task, last_fire_at="2026-06-23T08:00:00+00:00")
    clock = FixedClock(datetime(2026, 6, 23, 9, 0, 0, tzinfo=timezone.utc))
    assert should_catch_up(task, clock=clock) is False


# ---------------------------------------------------------------------------
# Clock implementations
# ---------------------------------------------------------------------------


def test_system_clock_sleep_seconds_does_not_raise() -> None:
    clock = SystemClock()
    clock.sleep_seconds(0)
    assert isinstance(clock.now(), datetime)


def test_fixed_clock_advance_and_sleep() -> None:
    clock = FixedClock(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))
    clock.sleep_seconds(30)
    assert clock.now().second == 30
    clock.advance(hours=1, minutes=1)
    assert clock.now().hour == 1
    assert clock.now().minute == 1
