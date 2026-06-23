"""Loop fire use case.

A single :func:`fire_loop` invocation:

1. Loads the recipe from disk and renders the PRD body template with the
   built-in and ``pre_command`` variables.
2. Writes a new PRD to ``tasks/pending/`` with a date-stamped filename.
3. Reuses :func:`create_issue_from_prd` to create a GitHub Issue.
4. Attaches the default ``loop/<id>`` label (and any extras from the
   recipe) via :meth:`IGitHubClient.edit_issue_labels`.
5. Updates the loop's persisted state with the new ``last_fire_at``,
   ``next_fire_at`` and ``fire_count`` so the daemon can schedule the
   subsequent run.

The function honours a ``dry_run`` flag that short-circuits steps 2-5 so
the user can preview what a fire would produce without touching the
filesystem or GitHub.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.interfaces.loop_scheduler import ILoopClock
from backend.core.shared.models.agent_runner import LabelConfig
from backend.core.shared.models.loop import (
    LoopFireResult,
    LoopFireStatus,
    LoopTask,
)
from backend.core.use_cases.create_issue_from_prd import (
    IssueFromPrdRequest,
    create_issue_from_prd,
)
from backend.core.use_cases.loop_recipe import (
    parse_loop_recipe,
    parse_pre_command_output,
    render_loop_recipe,
    render_loop_recipe_title,
)
from backend.core.use_cases.loop_scheduler import compute_next_fire

_logger = logging.getLogger(__name__)


_DEFAULT_PRD_DIR = Path("tasks") / "pending"


# ---------------------------------------------------------------------------
# Path / filename helpers
# ---------------------------------------------------------------------------


def build_prd_path(
    repo_path: Path,
    *,
    loop_id: str,
    priority: str,
    fire_at: datetime,
) -> Path:
    """Compute the absolute path of the PRD to generate for a fire.

    Args:
        repo_path: Target repository root.
        loop_id: Loop identifier (used as the slug when no explicit slug
            is configured).
        priority: PRD priority tag (``P0``/``P1``/``P2``/``P3``).
        fire_at: Wall-clock time of the fire.

    Returns:
        Absolute path of the generated PRD.
    """
    timestamp = fire_at.strftime("%Y%m%d-%H%M%S")
    file_name = f"{priority}-FEAT-{timestamp}-{loop_id}.md"
    return (repo_path / _DEFAULT_PRD_DIR / file_name).resolve()


def _slugify(text: str) -> str:
    """Best-effort slug from a recipe body title."""
    lowered = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-") or "loop"


# ---------------------------------------------------------------------------
# Issue deduplication
# ---------------------------------------------------------------------------


def _issue_already_exists(
    github_client: IGitHubClient,
    *,
    loop_id: str,
    day_token: str,
    limit: int = 50,
) -> bool:
    """Return True when an open Issue with ``loop/<id>`` label exists for today.

    Args:
        github_client: GitHub client used to query the target repository.
        loop_id: Loop identifier; the label is ``loop/<id>``.
        day_token: ``YYYY-MM-DD`` token expected to appear in the Issue
            title.
        limit: Maximum number of issues to fetch for the label.

    Returns:
        True when an open Issue matches both the label and the day token.
    """
    label = f"loop/{loop_id}"
    try:
        issues = github_client.list_issues_by_label(label, limit, state="open")
    except Exception as exc:  # noqa: BLE001 - GitHub client may raise.
        _logger.warning(
            "Could not check for existing loop issues on label %r: %s",
            label,
            exc,
        )
        return False
    return any(day_token in issue.title for issue in issues)


# ---------------------------------------------------------------------------
# Pre-command execution
# ---------------------------------------------------------------------------


def _run_pre_command(
    command: str,
    *,
    cwd: Path,
    process_runner: IProcessRunner,
) -> dict[str, str]:
    """Execute the pre_command and parse its ``KEY=value`` stdout.

    Args:
        command: Shell command to run. The command is passed to a real
            shell so users can use ``&&`` / pipes / env vars.
        cwd: Working directory for the command (typically the recipe's
            parent directory or repository root).
        process_runner: Process runner abstraction.

    Returns:
        Mapping of variable name to value. Returns an empty dict when the
        command produces no parseable output.
    """
    try:
        result = process_runner.run(
            ["/bin/sh", "-c", command],
            cwd=cwd,
            check=False,
            capture_output=True,
        )
    except Exception as exc:  # noqa: BLE001 - keep loop running.
        _logger.warning("pre_command failed: %s", exc)
        return {}
    if result.return_code != 0:
        _logger.warning(
            "pre_command exited with %d; stderr=%s",
            result.return_code,
            result.stderr.strip(),
        )
    return parse_pre_command_output(result.stdout)


# ---------------------------------------------------------------------------
# Public fire entry point
# ---------------------------------------------------------------------------


def fire_loop(
    task: LoopTask,
    *,
    repo_path: Path,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    state_store,
    clock: ILoopClock,
    content_generator: IContentGenerator | None = None,
    labels_config: LabelConfig | None = None,
    dry_run: bool = False,
) -> LoopFireResult:
    """Execute a single loop fire.

    Args:
        task: Registered loop task to fire.
        repo_path: Target repository root.
        github_client: GitHub client used to create the Issue.
        process_runner: Process runner used for ``pre_command`` and PRD
            publishing.
        state_store: Loop state store; the task's ``last_fire_at`` /
            ``next_fire_at`` are updated when ``dry_run`` is False.
        clock: Wall-clock abstraction.
        content_generator: Optional content generator passed through to
            :func:`create_issue_from_prd`.
        labels_config: Optional label config; defaults to :class:`LabelConfig`.
        dry_run: When True, render the PRD and report what *would* happen
            without writing the file, calling GitHub, or mutating the
            state store.

    Returns:
        A :class:`LoopFireResult` describing the outcome.

    Raises:
        FileNotFoundError: When the recipe file is missing.
        ValueError: When the recipe is malformed.
    """
    recipe = parse_loop_recipe(task.recipe_path)
    fire_at = clock.now().astimezone(timezone.utc)
    day_token = fire_at.strftime("%Y-%m-%d")
    effective_labels = labels_config or LabelConfig()

    extra_variables: dict[str, str] = {}
    if task.pre_command:
        extra_variables = _run_pre_command(
            task.pre_command,
            cwd=task.recipe_path.parent,
            process_runner=process_runner,
        )

    rendered_body = render_loop_recipe(
        recipe,
        fire_at=fire_at,
        extra_variables=extra_variables,
    )
    rendered_title = render_loop_recipe_title(
        recipe,
        fire_at=fire_at,
        extra_variables=extra_variables,
    )
    issue_title = rendered_title or recipe.id
    prd_path = build_prd_path(
        repo_path,
        loop_id=task.slug or task.id,
        priority=task.priority,
        fire_at=fire_at,
    )

    if dry_run:
        next_fire_dt = compute_next_fire(task.schedule, after=fire_at)
        return LoopFireResult(
            loop_id=task.id,
            status=LoopFireStatus.DRY_RUN,
            prd_path=prd_path,
            relative_prd_path=prd_path.relative_to(repo_path),
            skipped_reason=(
                f"Would render PRD at {prd_path} with title "
                f"{issue_title!r} and labels {list(recipe.all_labels())}; "
                f"next fire at {next_fire_dt.isoformat()}."
            ),
            next_fire_at=next_fire_dt.isoformat(),
        )

    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(rendered_body, encoding="utf-8")

    if _issue_already_exists(
        github_client,
        loop_id=task.id,
        day_token=day_token,
    ):
        next_fire_dt = compute_next_fire(task.schedule, after=fire_at)
        updated = replace(
            task,
            last_fire_at=fire_at.isoformat(),
            next_fire_at=next_fire_dt.isoformat(),
        )
        state_store.upsert_task(updated)
        return LoopFireResult(
            loop_id=task.id,
            status=LoopFireStatus.SKIPPED_DUPLICATE,
            prd_path=prd_path,
            relative_prd_path=prd_path.relative_to(repo_path),
            skipped_reason=(
                f"Open Issue with label loop/{task.id} already exists for "
                f"{day_token}; skipped creation."
            ),
            next_fire_at=next_fire_dt.isoformat(),
        )

    relative_prd_path = prd_path.relative_to(repo_path)
    issue_url = create_issue_from_prd(
        request=IssueFromPrdRequest(
            repo_path=repo_path,
            prd_path=prd_path,
            issue_type=task.issue_type,
            title_override=f"[{task.issue_type.title()}] {issue_title}",
            queue_ready=task.queue_ready,
            issue_agent=task.agent,
            labels_config=effective_labels,
            force=False,
            publish_prd=task.publish_prd,
            git_remote="origin",
            git_base_branch="main",
            generated_content_config=None,
            depends_on=(),
            depends_on_group=(),
            parse_evidence_format_with_agent=False,
            validation_language="zh-CN",
            structured_evidence=True,
        ),
        github_client=github_client,
        process_runner=process_runner,
        content_generator=content_generator,
    )
    issue_number = _extract_issue_number(issue_url)

    extra_labels = list(_effective_extra_labels(task, recipe))
    if extra_labels:
        if issue_number is None:
            _logger.warning(
                "Could not derive Issue number from %s; skipping extra labels.",
                issue_url,
            )
        else:
            github_client.edit_issue_labels(issue_number, add=extra_labels)

    next_fire_dt = compute_next_fire(task.schedule, after=fire_at)
    updated = replace(
        task,
        last_fire_at=fire_at.isoformat(),
        next_fire_at=next_fire_dt.isoformat(),
        fire_count=task.fire_count + 1,
    )
    state_store.upsert_task(updated)

    return LoopFireResult(
        loop_id=task.id,
        status=LoopFireStatus.FIRED,
        prd_path=prd_path,
        relative_prd_path=relative_prd_path,
        issue_url=issue_url,
        issue_number=issue_number,
        next_fire_at=next_fire_dt.isoformat(),
    )


def _extract_issue_number(issue_url: str) -> int | None:
    """Parse the trailing ``/issues/<N>`` segment from a GitHub Issue URL."""
    match = re.search(r"/issues/(\d+)", issue_url)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _effective_extra_labels(
    task: LoopTask,
    recipe,
) -> Sequence[str]:
    """Return the deduped label set to apply on top of the create_issue defaults."""
    seen: set[str] = set()
    ordered: list[str] = []
    for label in (*recipe.all_labels(), *task.labels):
        if label and label not in seen:
            seen.add(label)
            ordered.append(label)
    return ordered
