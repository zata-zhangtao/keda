"""Loop registration use case.

``create_loop_from_recipe`` is the single entry point that turns a parsed
loop recipe on disk into a persisted :class:`LoopTask` entry. The
persistence layer is the abstract :class:`ILoopStateStore` so the core
layer remains decoupled from the JSON file format.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from backend.core.shared.interfaces.loop_scheduler import ILoopStateStore
from backend.core.shared.models.loop import (
    LoopRecipe,
    LoopSchedule,
    LoopTask,
)
from backend.core.use_cases.loop_recipe import parse_loop_recipe
from backend.core.use_cases.loop_scheduler import compute_next_fire

_logger = logging.getLogger(__name__)


class LoopAlreadyExistsError(ValueError):
    """Raised when ``create`` is called for a loop id that is already registered."""


def _task_from_recipe(
    recipe: LoopRecipe,
    *,
    schedule: LoopSchedule | None = None,
    fire_at: datetime | None = None,
) -> LoopTask:
    """Translate a parsed recipe into a :class:`LoopTask` ready to persist.

    Args:
        recipe: Parsed loop recipe.
        schedule: Optional override schedule (e.g. when the user passed
            ``--cron`` on the command line). Defaults to the recipe's
            schedule.
        fire_at: Reference time used to compute ``next_fire_at``. Defaults
            to the current UTC instant.

    Returns:
        A :class:`LoopTask` mirroring the recipe fields.
    """
    effective_schedule = schedule or recipe.schedule
    reference = (fire_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    next_fire_dt = compute_next_fire(effective_schedule, after=reference)
    return LoopTask(
        id=recipe.id,
        recipe_path=recipe.source_path,
        repo_id=recipe.repo_id,
        schedule=effective_schedule,
        enabled=True,
        created_at=datetime.now(timezone.utc).isoformat(),
        next_fire_at=next_fire_dt.isoformat(),
        priority=recipe.priority,
        slug=recipe.slug,
        issue_type=recipe.issue_type,
        agent=recipe.agent,
        labels=recipe.labels,
        publish_prd=recipe.publish_prd,
        queue_ready=recipe.queue_ready,
        run_now=recipe.run_now,
        pre_command=recipe.pre_command,
        timezone_name=recipe.timezone_name,
    )


def create_loop_from_recipe(
    recipe_path: Path,
    *,
    state_store: ILoopStateStore,
    schedule: LoopSchedule | None = None,
    overwrite: bool = False,
    fire_at: datetime | None = None,
) -> LoopTask:
    """Register a loop recipe into the persistent state store.

    Args:
        recipe_path: Path to the recipe Markdown file.
        state_store: Loop state store implementation.
        schedule: Optional schedule override (e.g. from ``--cron``/``--every``).
        overwrite: When True, replace an existing task with the same id.
        fire_at: Reference time used to compute ``next_fire_at``; defaults
            to the current UTC instant.

    Returns:
        The persisted :class:`LoopTask`.

    Raises:
        FileNotFoundError: When ``recipe_path`` does not exist.
        ValueError: When the recipe is malformed.
        LoopAlreadyExistsError: When the loop id is already registered and
            ``overwrite`` is False.
    """
    recipe = parse_loop_recipe(recipe_path)
    state_store.load()
    existing = state_store.get_task(recipe.id)
    if existing is not None and not overwrite:
        raise LoopAlreadyExistsError(
            f"Loop '{recipe.id}' is already registered. Use --force to overwrite."
        )
    task = _task_from_recipe(recipe, schedule=schedule, fire_at=fire_at)
    state_store.upsert_task(task)
    _logger.info("Registered loop '%s' -> next fire at %s", task.id, task.next_fire_at)
    return task


def update_loop_schedule(
    loop_id: str,
    *,
    state_store: ILoopStateStore,
    new_schedule: LoopSchedule,
    fire_at: datetime | None = None,
) -> LoopTask:
    """Replace the schedule on an existing loop and recompute next fire.

    Args:
        loop_id: The registered loop id.
        state_store: Loop state store.
        new_schedule: New schedule (cron or interval) to install.
        fire_at: Reference time used to compute ``next_fire_at``.

    Returns:
        The updated :class:`LoopTask`.

    Raises:
        KeyError: When no task exists for ``loop_id``.
    """
    state_store.load()
    existing = state_store.get_task(loop_id)
    if existing is None:
        raise KeyError(f"Loop '{loop_id}' is not registered.")
    reference = (fire_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    next_fire_dt = compute_next_fire(new_schedule, after=reference)
    updated = replace(
        existing,
        schedule=new_schedule,
        next_fire_at=next_fire_dt.isoformat(),
    )
    state_store.upsert_task(updated)
    return updated


def cancel_loop(loop_id: str, *, state_store: ILoopStateStore) -> bool:
    """Remove a loop from the state store.

    Args:
        loop_id: The loop id to remove.
        state_store: Loop state store.

    Returns:
        True when the loop was removed, False when it was not registered.
    """
    state_store.load()
    removed = state_store.delete_task(loop_id)
    if removed:
        _logger.info("Cancelled loop '%s'.", loop_id)
    return removed


def list_loops(*, state_store: ILoopStateStore) -> list[LoopTask]:
    """Return all registered loops.

    Args:
        state_store: Loop state store.

    Returns:
        Sorted list of :class:`LoopTask` objects.
    """
    state_store.load()
    return state_store.list_tasks()
