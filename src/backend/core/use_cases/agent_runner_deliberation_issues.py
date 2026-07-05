"""Phase 0 deliberation queue — async Issue-comment discussion before PRD.

For Issues labelled ``agent/deliberate`` the daemon / ``iar run --once`` polls
run a third party (``iar deliberate``'s multi-agent engine, reused with no
live output view) and post a structured clarifying-question list as an Issue
comment, then wait for the human to reply. Turn state is carried entirely by
an ``iar:event`` marker written into the trailing HTML comment of the AI's
comment, matching the ``issue_comments_count`` and ``cycle`` pattern used by
the rework / blocked gates elsewhere in the runner — no new persistence.

Convergence is intentionally a human action: when the discussion is over, the
operator removes ``agent/deliberate`` and adds ``agent/rework-prd`` and the
existing Phase 1 (``process_prd_rework_issues``) takes over. The AI never
relabels or generates the PRD itself.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IAgentTranscriptRunner,
    IGitHubClient,
)
from backend.core.shared.models.agent_deliberation import (
    DeliberationConfig,
    DeliberationRequest,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    LabelConfig,
)
from backend.core.use_cases.agent_runner_events import (
    DELIBERATION_QUESTION_PHASE,
    format_event_marker,
    parse_latest_event_marker_for_phases,
)
from backend.core.use_cases.run_agent_deliberation import (
    run_agent_deliberation,
)

_logger = logging.getLogger(__name__)


_SOFT_HINT_TEXT = (
    "The discussion looks close to converging. When you are ready to lock the "
    "scope in, remove the `{deliberate_label}` label and add "
    "`{rework_label}` — Phase 1 will then turn this conversation into the "
    "canonical PRD."
)


def _resolve_turn(
    comments: list[str],
    marker: object | None,
) -> tuple[bool, int]:
    """Decide whether the AI should post a new round of questions.

    Args:
        comments: Issue comments in chronological order (the most recent one
            is at ``comments[-1]``).
        marker: The latest ``DELIBERATION_QUESTION_PHASE`` marker if one is
            already on the Issue, otherwise ``None``.

    Returns:
        ``(is_ai_turn, next_cycle)``. ``is_ai_turn`` is True when there is no
        marker yet (first round) or when the user has replied since the marker
        was written (more comments than the marker recorded). ``next_cycle`` is
        ``marker.cycle + 1`` when a marker exists, otherwise ``1``.
    """
    if marker is None:
        return True, 1
    marker_count_raw = getattr(marker, "issue_comments_count", None)
    if marker_count_raw is None:
        return True, int(getattr(marker, "cycle", 0)) + 1
    return len(comments) > marker_count_raw, int(marker.cycle) + 1


def _build_deliberation_issue_prompt(
    issue: IssueSummary,
    comments: list[str],
) -> str:
    """Assemble the Issue body + full comment thread as deliberation input."""
    sections: list[str] = [
        "You are reviewing an open GitHub Issue that the maintainer wants to "
        "discuss asynchronously before any PRD or implementation is written. "
        "The Issue body and the full comment thread are below. Identify the "
        "open questions the maintainer still needs answered so the team can "
        "scope the work clearly.",
        "",
        "## Issue body",
        issue.body or "(empty)",
    ]
    if comments:
        sections.append("")
        sections.append("## Issue comments (chronological)")
        for index, comment_body in enumerate(comments, start=1):
            sections.append(f"### Comment {index}")
            sections.append(comment_body)
            sections.append("")
    sections.append(
        "## Your task\n"
        "Surface the unresolved clarification questions across the five "
        "categories described in the synthesizer instructions: scope, "
        "constraints, acceptance criteria, technology choices, and risks."
    )
    return "\n".join(sections)


def _build_question_list_synthesis_prompt(
    request: DeliberationRequest,
    transcript: str,
) -> str:
    """Build the synthesizer prompt that turns the deliberation into questions."""
    sections = [
        f"Original request:\n{request.prompt}\n",
        "Here is the full public transcript of the multi-agent deliberation:",
        "---",
        transcript,
        "---",
        "",
        "Synthesize the discussion into a structured clarifying-question list "
        "the maintainer can answer directly on this GitHub Issue. Use "
        "**exactly** these five `## ` sections, in this order, with each "
        "section containing a bullet list of concrete questions (one bullet "
        "per question; no commentary, no preamble):",
        "",
        "## 范围边界",
        "## 约束",
        "## 验收标准",
        "## 技术选型",
        "## 风险",
        "",
        "End with a single short line explaining that the maintainer should "
        "either reply with the missing details on this Issue or, once the "
        "scope is clear enough, remove the `agent/deliberate` label and add "
        "`agent/rework-prd` so Phase 1 can draft the canonical PRD.",
        "Be concise and actionable. Output only the markdown — no extra narrative around it.",
    ]
    return "\n".join(sections)


def _format_question_list_comment(
    synthesis_output: str,
    cycle: int,
    issue_comments_count_after_post: int,
    *,
    include_soft_hint: bool,
    label_config: LabelConfig,
) -> str:
    """Compose the final Issue comment body (questions + marker)."""
    body = synthesis_output.rstrip()
    if include_soft_hint:
        body = (
            body
            + "\n\n> "
            + _SOFT_HINT_TEXT.format(
                deliberate_label=label_config.deliberate,
                rework_label=label_config.rework_prd,
            )
        )
    marker = format_event_marker(
        phase=DELIBERATION_QUESTION_PHASE,
        cycle=cycle,
        issue_comments_count=issue_comments_count_after_post,
    )
    return body + "\n\n" + marker


def process_deliberation_issues(
    *,
    repo_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    transcript_runner_factory: Callable[[Path], IAgentTranscriptRunner],
    max_issues: int = 1,
    stale_rounds_before_hint: int = 3,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Drive one Phase 0 pass over Issues labelled ``agent/deliberate``.

    For each eligible Issue:

    1. List comments and look for the latest
       ``DELIBERATION_QUESTION_PHASE`` marker.
    2. If the marker is fresh and no user reply has appeared, skip the Issue
       (do not post another question).
    3. Otherwise, run ``run_agent_deliberation`` (with a question-list
       synthesizer prompt and ``output_view=None`` for a NoOp background view)
       and post the synthesizer's markdown output as an Issue comment plus a
       new marker.
    4. Failures on a single Issue are isolated: the Issue is labelled
       ``labels.failed`` and a diagnostic comment is posted so the operator
       can intervene.

    Args:
        repo_path: Target repository path.
        config: Application configuration.
        github_client: GitHub client used for issue discovery / comments.
        transcript_runner_factory: Factory that builds an
            :class:`IAgentTranscriptRunner` for ``repo_path``. Allows callers
            in ``api/`` to inject the engine-layer implementation while
            keeping ``core/use_cases/`` free of engine imports.
        max_issues: Maximum number of ``agent/deliberate`` Issues to process
            per pass (defaults to 1 to bound deliberation cost).
        stale_rounds_before_hint: After this many AI rounds without the user
            adding new information, the question-list comment appends a soft
            hint suggesting the operator swap labels.
        clock: Monotonic clock used to log per-issue elapsed time in debug
            builds; defaults to ``time.monotonic``.
    """
    issues = github_client.list_issues_by_label(
        config.labels.deliberate, limit=max_issues, state="open"
    )
    if not issues:
        return

    transcript_runner = transcript_runner_factory(repo_path)

    for issue in issues:
        started_at = clock()
        try:
            _process_single_deliberation_issue(
                issue=issue,
                config=config,
                github_client=github_client,
                transcript_runner=transcript_runner,
                stale_rounds_before_hint=stale_rounds_before_hint,
            )
        except Exception as exc:  # noqa: BLE001 - isolate per-Issue failures.
            _logger.exception("Deliberation phase failed for Issue #%d", issue.number)
            _mark_deliberation_failed(
                issue=issue,
                config=config,
                github_client=github_client,
                exc=exc,
            )
        finally:
            _logger.debug(
                "Deliberation Issue #%d finished in %.3fs",
                issue.number,
                clock() - started_at,
            )


def _process_single_deliberation_issue(
    *,
    issue: IssueSummary,
    config: AppConfig,
    github_client: IGitHubClient,
    transcript_runner: IAgentTranscriptRunner,
    stale_rounds_before_hint: int,
) -> None:
    """Process a single ``agent/deliberate`` Issue.

    Helper extracted from :func:`process_deliberation_issues` so per-issue
    exceptions stay scoped to the iteration and ``process_deliberation_issues``
    only has to wrap a single function call in try/except.
    """
    comments = github_client.list_issue_comments(issue.number)
    marker = parse_latest_event_marker_for_phases(comments, {DELIBERATION_QUESTION_PHASE})
    is_ai_turn, next_cycle = _resolve_turn(comments, marker)
    if not is_ai_turn:
        _logger.info(
            "Issue #%d still waiting on user reply (cycle=%s); skipping Phase 0 turn.",
            issue.number,
            getattr(marker, "cycle", None),
        )
        return

    prompt = _build_deliberation_issue_prompt(issue, comments)
    session_id = f"issue-{issue.number}-cycle-{next_cycle}"
    output_dir = Path(config.deliberation.default_output_dir) / "deliberate-issues" / session_id
    configured_profiles: tuple[str, ...] = tuple(
        profile.profile_id for profile in config.deliberation.profiles
    )
    request = DeliberationRequest(
        prompt=prompt,
        agents=configured_profiles
        or (
            "architect",
            "skeptic",
            "implementer",
        ),
        rounds=config.deliberation.default_rounds,
        synthesizer=config.deliberation.default_synthesizer,
        output_dir=str(output_dir),
        session_id=session_id,
    )
    deliberation_config = _config_to_deliberation_config(config)
    emitted_events: list = []

    def _record_event(event) -> None:
        emitted_events.append(event)

    result = run_agent_deliberation(
        request=request,
        config=deliberation_config,
        transcript_runner=transcript_runner,
        event_sink=_record_event,
        target_repo_path=_repo_path_from_config(config),
        output_view=None,
        synthesis_prompt_builder=_build_question_list_synthesis_prompt,
    )

    include_soft_hint = next_cycle >= stale_rounds_before_hint
    comment_body = _format_question_list_comment(
        result.recommendation or _format_fallback_question_list(result),
        cycle=next_cycle,
        issue_comments_count_after_post=len(comments) + 1,
        include_soft_hint=include_soft_hint,
        label_config=config.labels,
    )
    github_client.comment_issue(issue.number, comment_body)


def _format_fallback_question_list(result) -> str:
    """Build a best-effort question list when the synthesizer produced nothing.

    The synthesizer prompt explicitly asks for the five ``## `` sections. If
    the model returns blank sections we still want a non-empty comment so the
    operator gets *something* to react to; fall back to the structured
    ``DeliberationResult`` fields the engine already collects.
    """
    parts: list[str] = []
    if result.consensus:
        parts.append("## 范围边界\n" + result.consensus)
    if result.recommendation:
        parts.append("## 约束\n" + result.recommendation)
    if result.next_actions:
        parts.append("## 验收标准\n" + result.next_actions)
    if result.risks:
        parts.append("## 风险\n" + result.risks)
    if not parts:
        parts.append(
            "## 范围边界\n"
            "The deliberation did not produce concrete questions. "
            "Please reply with any clarification you can."
        )
    return "\n\n".join(parts)


def _config_to_deliberation_config(config: AppConfig) -> DeliberationConfig:
    """Pass-through translation (both layers already use the same dataclass)."""
    return DeliberationConfig(
        default_rounds=config.deliberation.default_rounds,
        default_synthesizer=config.deliberation.default_synthesizer,
        default_output_dir=config.deliberation.default_output_dir,
        profiles=config.deliberation.profiles,
    )


def _repo_path_from_config(config: AppConfig) -> Path:
    """Best-effort repo path used as the deliberation ``target_repo_path``.

    The deliberation engine only treats ``target_repo_path`` as a safety
    anchor for read-only enforcement; it does not need to exist on disk for
    the question-list synthesis path because every participating agent runs
    in read-only mode. Pick a stable scratch path so engine logging is
    consistent across calls.
    """
    return Path(config.deliberation.default_output_dir).resolve().parent.parent


def _mark_deliberation_failed(
    *,
    issue: IssueSummary,
    config: AppConfig,
    github_client: IGitHubClient,
    exc: BaseException,
) -> None:
    """Best-effort failure labelling mirroring :func:`process_prd_rework_issues`."""
    try:
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.failed],
            remove=[config.labels.deliberate],
        )
    except Exception as label_exc:  # noqa: BLE001 - best-effort.
        _logger.error(
            "Failed to mark Issue #%d as %s: %s",
            issue.number,
            config.labels.failed,
            label_exc,
        )
    try:
        github_client.comment_issue(
            issue.number,
            "Deliberation phase failed: "
            f"{exc}\n\n"
            f"Issue was labelled `{config.labels.failed}`. Remove that label "
            f"and re-add `{config.labels.deliberate}` (or swap to "
            f"`{config.labels.rework_prd}`) to retry.",
        )
    except Exception as comment_exc:  # noqa: BLE001 - best-effort.
        _logger.error(
            "Failed to comment on Issue #%d deliberation failure: %s",
            issue.number,
            comment_exc,
        )
