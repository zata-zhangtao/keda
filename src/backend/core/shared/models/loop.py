"""Loop subsystem domain value objects.

These dataclasses represent parsed loop recipes, registered loop tasks, and the
result of a single loop fire. They are intentionally pure: every field is a
primitive (or a tuple / path) so that the use-case layer can manipulate them
without touching infrastructure. All loops in the codebase flow through these
objects; serialization to / from disk lives in
:mod:`backend.engines.agent_runner.persistence.loop_state_json`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


# ---------------------------------------------------------------------------
# Schedule primitives
# ---------------------------------------------------------------------------


class LoopScheduleKind(str, Enum):
    """How the loop's schedule string should be interpreted."""

    CRON = "cron"
    INTERVAL = "interval"


_INTERVAL_PATTERN = re.compile(r"^(?P<value>\d+)(?P<unit>[mhd])$")


@dataclass(frozen=True)
class LoopSchedule:
    """A loop schedule declaration.

    Attributes:
        kind: Whether ``expression`` is a cron string or an interval like
            ``"10m"`` / ``"1h"`` / ``"1d"``.
        expression: The raw expression as supplied by the user. For cron,
            this is a 5-field cron string in local time. For interval, this
            is a ``<number><unit>`` string.
    """

    kind: LoopScheduleKind
    expression: str

    @classmethod
    def from_expression(cls, expression: str) -> "LoopSchedule":
        """Infer the schedule kind from the expression text.

        Args:
            expression: Schedule string. ``"@every ..."`` is not supported in
                the MVP; pure cron (5 whitespace-separated fields) or an
                interval token like ``"10m"`` / ``"1h"`` / ``"1d"`` are
                accepted.

        Returns:
            A ``LoopSchedule`` with the inferred kind.

        Raises:
            ValueError: When the expression is empty or unsupported.
        """
        cleaned = expression.strip()
        if not cleaned:
            raise ValueError("Loop schedule expression must not be empty.")
        parts = cleaned.split()
        if len(parts) == 5:
            return cls(kind=LoopScheduleKind.CRON, expression=cleaned)
        if len(parts) == 1 and _INTERVAL_PATTERN.match(cleaned):
            # ``1d`` is a user-friendly alias for daily-at-midnight cron
            # so callers don't have to remember ``"0 0 * * *"``. Other
            # intervals keep their cadence-relative semantics.
            if cleaned == "1d":
                return cls(kind=LoopScheduleKind.CRON, expression="0 0 * * *")
            return cls(kind=LoopScheduleKind.INTERVAL, expression=cleaned)
        raise ValueError(
            "Unsupported loop schedule expression: "
            f"{expression!r}. Use a 5-field cron expression or an interval "
            "like '10m', '1h', or '1d'."
        )


# ---------------------------------------------------------------------------
# Recipe (parsed frontmatter + body)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopRecipe:
    """A parsed loop recipe (frontmatter + PRD body template).

    Attributes:
        id: Short kebab-case identifier; must match the ``id`` frontmatter.
        schedule: The schedule declared in the recipe (cron or interval).
        repo_id: Target repository registered in ``config.toml``.
        body_template: PRD body template with ``{{...}}`` placeholders.
        source_path: Absolute path to the recipe file on disk.
        issue_type: Issue type label (``feature`` / ``refactor`` / ``bug``).
        agent: Agent routing hint.
        labels: Extra GitHub labels to apply on top of the defaults.
        publish_prd: Whether to commit/push the generated PRD before creating
            the Issue.
        queue_ready: Whether to attach the ``agent/ready`` label.
        run_now: Whether to invoke ``run_agent_repositories_once`` immediately
            after Issue creation.
        pre_command: Optional shell command whose ``KEY=value`` stdout lines are
            injected as template variables before rendering.
        timezone_name: Optional IANA timezone name for cron scheduling.
        priority: PRD priority tag used in the generated filename
            (``P0`` / ``P1`` / ``P2`` / ``P3``).
        slug: Optional short slug for the generated PRD filename. Defaults to
            the loop id.
    """

    id: str
    schedule: LoopSchedule
    repo_id: str
    body_template: str
    source_path: Path
    issue_type: str = "feature"
    agent: str = "auto"
    labels: tuple[str, ...] = ()
    publish_prd: bool = True
    queue_ready: bool = True
    run_now: bool = False
    pre_command: str | None = None
    timezone_name: str | None = None
    priority: str = "P2"
    slug: str | None = None

    def effective_slug(self) -> str:
        """Return the slug used for the generated PRD filename."""
        return self.slug or self.id

    def default_labels(self) -> tuple[str, ...]:
        """Return the base label set every loop fire applies."""
        return ("loop/" + self.id,)

    def all_labels(self) -> tuple[str, ...]:
        """Return the deduplicated union of default and extra labels."""
        seen: set[str] = set()
        result: list[str] = []
        for label in (*self.default_labels(), *self.labels):
            if label and label not in seen:
                seen.add(label)
                result.append(label)
        return tuple(result)


# ---------------------------------------------------------------------------
# Registered task (state file entry)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopTask:
    """A persisted loop entry stored in ``~/.iar/loop-state.json``.

    Attributes:
        id: Loop identifier.
        recipe_path: Absolute path of the source recipe file.
        repo_id: Repository the loop fires against.
        schedule: Resolved schedule for the loop.
        enabled: Whether the loop should run. Disabled loops are kept in the
            state file for inspection but skipped by the daemon.
        created_at: ISO-8601 UTC timestamp of when the loop was registered.
        last_fire_at: ISO-8601 UTC timestamp of the most recent fire, or
            ``None`` if the loop has never fired.
        next_fire_at: ISO-8601 UTC timestamp of the next scheduled fire, or
            ``None`` when the schedule cannot be evaluated (e.g. on first
            registration).
        fire_count: Number of completed fires.
        last_error: Free-form error message from the most recent failed fire,
            or ``None`` if the last fire succeeded.
        priority: PRD priority tag propagated to generated PRDs.
        slug: Optional PRD slug.
        issue_type: Issue type label.
        agent: Agent routing hint.
        labels: Extra labels applied on top of the default ``loop/<id>``.
        publish_prd: Whether to publish the PRD before Issue creation.
        queue_ready: Whether to apply the ready label.
        run_now: Whether to invoke the runner immediately after Issue creation.
        pre_command: Optional pre-fire shell command.
        timezone_name: Optional IANA timezone name for cron schedules.
    """

    id: str
    recipe_path: Path
    repo_id: str
    schedule: LoopSchedule
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_fire_at: str | None = None
    next_fire_at: str | None = None
    fire_count: int = 0
    last_error: str | None = None
    priority: str = "P2"
    slug: str | None = None
    issue_type: str = "feature"
    agent: str = "auto"
    labels: tuple[str, ...] = ()
    publish_prd: bool = True
    queue_ready: bool = True
    run_now: bool = False
    pre_command: str | None = None
    timezone_name: str | None = None


# ---------------------------------------------------------------------------
# Fire results
# ---------------------------------------------------------------------------


class LoopFireStatus(str, Enum):
    """Outcome of a single loop fire."""

    FIRED = "fired"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class LoopFireResult:
    """Result of a single loop trigger.

    Attributes:
        loop_id: Loop that was triggered.
        status: Outcome kind.
        prd_path: Absolute path to the generated PRD, when a PRD was rendered.
        relative_prd_path: Path relative to ``repo_path`` for the generated
            PRD, when applicable.
        issue_url: GitHub Issue URL when an Issue was actually created.
        issue_number: GitHub Issue number when one was created.
        skipped_reason: Free-form reason when ``status`` is a skip / dry-run.
        next_fire_at: ISO-8601 UTC timestamp of the loop's next scheduled
            fire, when known.
    """

    loop_id: str
    status: LoopFireStatus
    prd_path: Path | None = None
    relative_prd_path: Path | None = None
    issue_url: str | None = None
    issue_number: int | None = None
    skipped_reason: str | None = None
    next_fire_at: str | None = None
