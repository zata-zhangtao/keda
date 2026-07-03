"""CLI handlers for the ``iar loop`` subcommand.

The argparse / Typer surfaces in :mod:`backend.api.cli_parser` and
:mod:`backend.api.cli_typer` delegate the actual work to the helpers in
this module. The handlers stay focused on argument translation, the use
cases in :mod:`backend.core.use_cases.loop_*` are responsible for the
business logic.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.shared.models.loop import LoopSchedule, LoopScheduleKind, LoopTask
from backend.core.use_cases.loop_create import (
    LoopAlreadyExistsError,
    cancel_loop,
    create_loop_from_recipe,
    list_loops,
)
from backend.core.use_cases.loop_fire import fire_loop
from backend.core.use_cases.loop_recipe import parse_loop_recipe

_logger = logging.getLogger(__name__)

# Re-use the slug pattern from the loop_recipe module so we can validate
# loop ids supplied on the CLI before consulting the recipe file.
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------


def build_schedule_from_args(parsed: argparse.Namespace) -> LoopSchedule | None:
    """Translate CLI ``--cron`` / ``--every`` flags into a :class:`LoopSchedule`.

    Args:
        parsed: The argparse namespace with optional ``cron`` and ``every`` fields.

    Returns:
        A :class:`LoopSchedule`, or ``None`` when neither flag was provided.

    Raises:
        ValueError: When both flags are supplied, or when both are missing.
    """
    cron_expression = getattr(parsed, "cron", None)
    every_expression = getattr(parsed, "every", None)
    if cron_expression and every_expression:
        raise ValueError("--cron and --every are mutually exclusive.")
    if cron_expression:
        return LoopSchedule(kind=LoopScheduleKind.CRON, expression=cron_expression.strip())
    if every_expression:
        # ``1d`` short-form: callers may pass ``"1d"`` and expect cron-style
        # ``"0 0 * * *"`` semantics. Use the parser to validate the format.
        return LoopSchedule.from_expression(every_expression.strip())
    return None


def _format_task_row(task: LoopTask) -> str:
    """Render a single loop entry as a fixed-width row for the table output."""
    schedule = f"{task.schedule.kind.value}:{task.schedule.expression}"
    enabled = "yes" if task.enabled else "no"
    next_fire = task.next_fire_at or "—"
    return (
        f"{task.id:<24} {task.repo_id:<24} {schedule:<28} "
        f"enabled={enabled:<3} next_fire={next_fire}"
    )


def _print_loop_table(tasks: list[LoopTask]) -> None:
    """Print a human-readable loop table to stdout."""
    if not tasks:
        print("No loops registered. Use `iar loop create` to add one.")
        return
    header = f"{'id':<24} {'repo_id':<24} {'schedule':<28} {'enabled':<10} next_fire"
    print(header)
    print("-" * len(header))
    for task in tasks:
        print(_format_task_row(task))


# ---------------------------------------------------------------------------
# Argument validators (reused by Typer and argparse paths)
# ---------------------------------------------------------------------------


def validate_recipe_path(recipe_path: str) -> Path:
    """Validate that the recipe path exists and is a file.

    Args:
        recipe_path: User-supplied path string.

    Returns:
        Resolved :class:`Path`.

    Raises:
        FileNotFoundError: When the file does not exist.
    """
    resolved = Path(recipe_path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Loop recipe not found: {resolved}")
    return resolved


# ---------------------------------------------------------------------------
# CLI command implementations
# ---------------------------------------------------------------------------


def _resolve_loop_repo_id(parsed: argparse.Namespace, recipe_repo_id: str) -> str:
    """Return the explicit ``--repo-id`` from the CLI or the recipe's default."""
    explicit = getattr(parsed, "loop_repo_id", None)
    if explicit:
        return explicit
    return recipe_repo_id


def _resolve_loop_repo_path(parsed: argparse.Namespace) -> Path | None:
    """Return the explicit ``--repo`` path from the CLI, if any."""
    repo_path = getattr(parsed, "loop_repo", None)
    if not repo_path:
        return None
    return Path(repo_path).expanduser().resolve()


def run_loop_create_command(
    parsed: argparse.Namespace,
    *,
    state_store_factory,
) -> int:
    """Implement ``iar loop create`` for both argparse and Typer paths.

    Args:
        parsed: The argparse namespace.
        state_store_factory: Callable returning a fresh state store.

    Returns:
        Exit code (0 on success, 1 on validation error).
    """
    loop_id = getattr(parsed, "loop_id", None)
    if not loop_id:
        logger.error("loop id is required.")
        return 1
    if not _SLUG_PATTERN.match(loop_id):
        logger.error(
            "Loop id %r must be kebab-case (lowercase letters, digits, hyphens).",
            loop_id,
        )
        return 1
    recipe_path = validate_recipe_path(getattr(parsed, "recipe", ""))
    recipe = parse_loop_recipe(recipe_path)
    if recipe.id != loop_id:
        logger.error(
            "Recipe frontmatter id %r does not match command-line id %r.",
            recipe.id,
            loop_id,
        )
        return 1
    try:
        schedule_override = build_schedule_from_args(parsed)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    state_store = state_store_factory()
    try:
        task = create_loop_from_recipe(
            recipe_path,
            state_store=state_store,
            schedule=schedule_override,
            overwrite=bool(getattr(parsed, "force", False)),
        )
    except LoopAlreadyExistsError as exc:
        logger.error("%s", exc)
        return 1
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    print(
        f"Registered loop '{task.id}' "
        f"(repo_id={task.repo_id}, schedule={task.schedule.expression}, "
        f"next_fire={task.next_fire_at})."
    )
    return 0


def run_loop_list_command(*, state_store_factory) -> int:
    """Implement ``iar loop list``.

    Args:
        state_store_factory: Callable returning a fresh state store.

    Returns:
        Exit code (always 0).
    """
    state_store = state_store_factory()
    tasks = list_loops(state_store=state_store)
    _print_loop_table(tasks)
    return 0


def run_loop_cancel_command(
    parsed: argparse.Namespace,
    *,
    state_store_factory,
) -> int:
    """Implement ``iar loop cancel``.

    Args:
        parsed: The argparse namespace.
        state_store_factory: Callable returning a fresh state store.

    Returns:
        Exit code (0 on success, 1 when the loop does not exist).
    """
    loop_id = getattr(parsed, "loop_id", None)
    if not loop_id:
        logger.error("loop id is required.")
        return 1
    state_store = state_store_factory()
    if cancel_loop(loop_id, state_store=state_store):
        print(f"Cancelled loop '{loop_id}'.")
        return 0
    logger.error("Loop '%s' is not registered.", loop_id)
    return 1


def run_loop_run_now_command(
    parsed: argparse.Namespace,
    *,
    state_store_factory,
    github_client_factory,
    process_runner,
    clock,
    repo_resolver,
    content_generator_factory=None,
    labels_config=None,
) -> int:
    """Implement ``iar loop run --now <id>``.

    Args:
        parsed: The argparse namespace.
        state_store_factory: Callable returning a fresh state store.
        github_client_factory: Callable producing an :class:`IGitHubClient`.
        process_runner: Process runner used by fire_loop.
        clock: Wall-clock abstraction.
        repo_resolver: Resolves the repository path for a loop task.
        content_generator_factory: Optional content generator factory.
        labels_config: Optional label config.

    Returns:
        Exit code (0 on success, 1 on validation error).
    """
    loop_id = getattr(parsed, "loop_id", None)
    if not loop_id:
        logger.error("loop id is required.")
        return 1
    dry_run = bool(getattr(parsed, "dry_run", False))
    state_store = state_store_factory()
    state_store.load()
    task = state_store.get_task(loop_id)
    if task is None:
        logger.error("Loop '%s' is not registered.", loop_id)
        return 1
    repo_path = repo_resolver(task)
    github_client = github_client_factory(repo_path)
    content_generator = content_generator_factory(repo_path) if content_generator_factory else None
    try:
        result = fire_loop(
            task,
            repo_path=repo_path,
            github_client=github_client,
            process_runner=process_runner,
            state_store=state_store,
            clock=clock,
            content_generator=content_generator,
            labels_config=labels_config,
            dry_run=dry_run,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    if result.status.value == "fired":
        print(
            f"Loop '{result.loop_id}' fired: PRD={result.relative_prd_path}, "
            f"issue={result.issue_url}, next_fire={result.next_fire_at}."
        )
    elif result.status.value == "skipped_duplicate":
        print(
            f"Loop '{result.loop_id}' skipped: {result.skipped_reason} "
            f"PRD={result.relative_prd_path}, next_fire={result.next_fire_at}."
        )
    else:  # dry_run
        print(f"Loop '{result.loop_id}' dry-run: {result.skipped_reason}")
    return 0


def run_loop_daemon_command(
    parsed: argparse.Namespace,
    *,
    state_store_factory,
    github_client_factory,
    process_runner,
    clock,
    repo_resolver,
    content_generator_factory=None,
    labels_config=None,
) -> int:
    """Implement ``iar loop-daemon``.

    Args:
        parsed: The argparse namespace.
        state_store_factory: Callable returning a fresh state store.
        github_client_factory: Callable producing an :class:`IGitHubClient`.
        process_runner: Process runner used by the daemon.
        clock: Wall-clock abstraction.
        repo_resolver: Resolves the repository path for a loop task.
        content_generator_factory: Optional content generator factory.
        labels_config: Optional label config.

    Returns:
        Exit code (0 on graceful shutdown, 1 on configuration error).
    """
    from backend.core.use_cases.loop_daemon import run_loop_daemon

    interval = getattr(parsed, "interval", None)
    dry_run = bool(getattr(parsed, "dry_run", False))
    if interval is None:
        interval = int(os.environ.get("IAR_LOOP_DAEMON_INTERVAL", "60"))
    if interval <= 0:
        logger.error("loop-daemon --interval must be positive.")
        return 1
    state_store = state_store_factory()
    try:
        run_loop_daemon(
            state_store=state_store,
            github_client_factory=github_client_factory,
            process_runner=process_runner,
            clock=clock,
            repo_resolver=repo_resolver,
            interval=interval,
            content_generator_factory=content_generator_factory,
            labels_config=labels_config,
            dry_run=dry_run,
            max_passes=1 if dry_run else None,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    return 0


# Re-export logger for the CLI dispatcher to share.
logger = _logger


def utcnow_iso() -> str:
    """Return the current UTC time as ISO-8601 (used by tests)."""
    return datetime.now(timezone.utc).isoformat()


# Helper for callers that need the singleton state store.
def default_state_store():
    """Return the default JSON-backed state store."""
    from backend.engines.agent_runner.persistence.loop_state_json import (
        JsonLoopStateStore,
    )

    return JsonLoopStateStore()


# ---------------------------------------------------------------------------
# Default dependency wiring for ``backend.api.cli`` dispatchers.
# ---------------------------------------------------------------------------


def _resolve_loop_repo_path_for_task(task: LoopTask) -> Path:
    """Resolve a :class:`LoopTask`'s ``repo_id`` to an absolute path.

    Args:
        task: Loop task whose ``repo_id`` should be resolved.

    Returns:
        Absolute :class:`Path` of the target repository.

    Raises:
        ValueError: When the repository is not registered in ``config.toml``
            or is disabled.
    """
    from backend.engines.agent_runner.factory import get_agent_runner_settings

    settings = get_agent_runner_settings()
    if task.repo_id not in settings.repositories:
        raise ValueError(f"Loop '{task.id}' targets unknown repository '{task.repo_id}'.")
    repo_settings = settings.repositories[task.repo_id]
    if not repo_settings.enabled:
        raise ValueError(f"Loop '{task.id}' targets disabled repository '{task.repo_id}'.")
    return Path(repo_settings.path).expanduser().resolve()


def build_loop_cli_dependencies() -> dict[str, Any]:
    """Build the dependency dict used by ``run_loop_*`` commands.

    Returns:
        Mapping with state-store factory, GitHub-client factory, process
        runner, clock, content-generator factory and repo resolver.
    """
    from backend.engines.agent_runner.factory import (
        create_content_generator,
        create_github_client,
        create_loop_clock,
        create_process_runner,
    )

    process_runner = create_process_runner()

    def _github_client_factory(repo_path: Path):
        return create_github_client(repo_path, process_runner)

    def _content_generator_factory(repo_path: Path):
        return create_content_generator(process_runner)

    return {
        "state_store_factory": default_state_store,
        "github_client_factory": _github_client_factory,
        "process_runner": process_runner,
        "clock": create_loop_clock(),
        "repo_resolver": _resolve_loop_repo_path_for_task,
        "content_generator_factory": _content_generator_factory,
    }


def run_loop_command(parsed: argparse.Namespace) -> int:
    """Dispatch a parsed ``iar loop ...`` invocation.

    Args:
        parsed: Parsed CLI namespace with a ``loop_command`` attribute.

    Returns:
        Process exit code.
    """
    sub = getattr(parsed, "loop_command", None)
    if sub == "create":
        return run_loop_create_command(parsed, state_store_factory=default_state_store)
    if sub == "list":
        return run_loop_list_command(state_store_factory=default_state_store)
    if sub == "cancel":
        return run_loop_cancel_command(parsed, state_store_factory=default_state_store)
    if sub == "run":
        deps = build_loop_cli_dependencies()
        return run_loop_run_now_command(
            parsed,
            state_store_factory=deps["state_store_factory"],
            github_client_factory=deps["github_client_factory"],
            process_runner=deps["process_runner"],
            clock=deps["clock"],
            repo_resolver=deps["repo_resolver"],
            content_generator_factory=deps["content_generator_factory"],
        )
    if sub == "daemon":
        deps = build_loop_cli_dependencies()
        return run_loop_daemon_command(
            parsed,
            state_store_factory=deps["state_store_factory"],
            github_client_factory=deps["github_client_factory"],
            process_runner=deps["process_runner"],
            clock=deps["clock"],
            repo_resolver=deps["repo_resolver"],
            content_generator_factory=deps["content_generator_factory"],
        )
    logger.error("Unknown loop subcommand: %r", sub)
    return 1
