"""Tests for failure classification and operator-facing failure reports.

Covers ``classify_failure``, transient / provider-capacity predicates,
usage-limit root cause detection, attempt history tables, failure comments
and cross-agent fallback order resolution."""

from __future__ import annotations

import subprocess

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    AttemptResult,
    CommandResult,
    FailureType,
    RunnerConfig,
)
from backend.core.use_cases.run_agent_once import (
    MaxRetriesExceededError,
    classify_failure,
    detect_usage_limit_root_cause,
    format_agent_execution_failure,
    format_attempt_history,
    format_failure_comment,
    format_minimal_failure_comment,
    resolve_agent_fallback_order,
)
from backend.core.use_cases.agent_runner_failure import (
    is_provider_capacity_failure,
    is_transient_failure,
)
from backend.infrastructure.process_runner import CommandFailedError
from backend.core.use_cases.agent_runner_feedback import (
    format_prd_delivery_detail,
)
from backend.core.use_cases.agent_runner_validation import (
    format_validation_evidence_detail,
)
from tests.support.agent_runner import (
    make_ready_issue,
    transient_command_error,
)


def test_classify_failure_uncommitted() -> None:
    """classify_failure should return UNCOMMITTED_CHANGES when worktree is dirty."""
    agent_result = CommandResult(("codex",), 0, "", "")
    failure_type = classify_failure(
        before_sha="abc",
        after_sha="abc",
        has_uncommitted=True,
        agent_result=agent_result,
        verification_results=[],
        exc=None,
    )
    assert failure_type == FailureType.UNCOMMITTED_CHANGES


def test_classify_failure_no_commits() -> None:
    """classify_failure should return NO_COMMITS when SHA did not change."""
    agent_result = CommandResult(("codex",), 0, "", "")
    failure_type = classify_failure(
        before_sha="abc",
        after_sha="abc",
        has_uncommitted=False,
        agent_result=agent_result,
        verification_results=[],
        exc=None,
    )
    assert failure_type == FailureType.NO_COMMITS


def test_classify_failure_verification_failed() -> None:
    """classify_failure should return VERIFICATION_FAILED when a check fails."""
    agent_result = CommandResult(("codex",), 0, "", "")
    verification_results = [
        CommandResult(("just", "test"), 1, "", "tests failed"),
    ]
    failure_type = classify_failure(
        before_sha="abc",
        after_sha="def",
        has_uncommitted=False,
        agent_result=agent_result,
        verification_results=verification_results,
        exc=None,
    )
    assert failure_type == FailureType.VERIFICATION_FAILED


def test_classify_failure_agent_error() -> None:
    """classify_failure should return AGENT_ERROR when agent exits non-zero."""
    agent_result = CommandResult(("codex",), 1, "", "API error")
    failure_type = classify_failure(
        before_sha="abc",
        after_sha="def",
        has_uncommitted=False,
        agent_result=agent_result,
        verification_results=[CommandResult(("just", "test"), 0, "", "")],
        exc=None,
    )
    assert failure_type == FailureType.AGENT_ERROR


def test_classify_failure_forbidden_blocked_paths() -> None:
    """classify_failure should return FORBIDDEN_BLOCKED for forbidden path violations."""
    agent_result = CommandResult(("codex",), 0, "", "")
    exc = RuntimeError("Refusing to publish forbidden paths: .env")
    failure_type = classify_failure(
        before_sha="abc",
        after_sha="abc",
        has_uncommitted=False,
        agent_result=agent_result,
        verification_results=[],
        exc=exc,
    )
    assert failure_type == FailureType.FORBIDDEN_BLOCKED


def test_classify_failure_success() -> None:
    """classify_failure should return SUCCESS when everything passes."""
    agent_result = CommandResult(("codex",), 0, "", "")
    failure_type = classify_failure(
        before_sha="abc",
        after_sha="def",
        has_uncommitted=False,
        agent_result=agent_result,
        verification_results=[CommandResult(("just", "test"), 0, "", "")],
        exc=None,
    )
    assert failure_type == FailureType.SUCCESS


def test_format_attempt_history_empty() -> None:
    """format_attempt_history should return empty string for empty results."""
    assert format_attempt_history([]) == ""


def test_format_attempt_history_table() -> None:
    """format_attempt_history should render a markdown table."""
    results = [
        AttemptResult(
            attempt_number=1,
            failure_type=FailureType.NO_COMMITS,
            recovered=False,
            detail="No commits produced.",
        ),
        AttemptResult(
            attempt_number=2,
            failure_type=FailureType.SUCCESS,
            recovered=True,
            detail="Agent fixed the issue.",
        ),
    ]
    table = format_attempt_history(results)
    assert (
        "| Attempt | Started (UTC) | Agent | Failure Type | Recovered | Duration | Detail |"
        in table
    )
    assert "| 1 | - | - | no_commits | No | 0.0s | No commits produced. |" in table
    assert "| 2 | - | - | success | Yes | 0.0s | Agent fixed the issue. |" in table


def test_format_attempt_history_includes_agent_column() -> None:
    """Attempts stamped with an agent should render it in the Agent column."""
    results = [
        AttemptResult(
            attempt_number=1,
            failure_type=FailureType.PROVIDER_CAPACITY,
            recovered=False,
            detail="Claude at capacity.",
            agent="claude",
        ),
        AttemptResult(
            attempt_number=1,
            failure_type=FailureType.SUCCESS,
            recovered=True,
            detail="Codex finished.",
            agent="codex",
        ),
    ]
    table = format_attempt_history(results)
    assert "| 1 | - | claude | provider_capacity | No | 0.0s | Claude at capacity. |" in table
    assert "| 1 | - | codex | success | Yes | 0.0s | Codex finished. |" in table


def test_format_attempt_history_separates_runs_that_restart_numbering() -> None:
    """Repeated attempt numbers should stay distinguishable via Started + note."""
    results = [
        AttemptResult(
            attempt_number=6,
            failure_type=FailureType.AGENT_ERROR,
            recovered=False,
            detail="Evidence gate rejected the manifest.",
            agent="claude",
            started_at="2026-07-30T05:31:12.367039+00:00",
        ),
        AttemptResult(
            attempt_number=1,
            failure_type=FailureType.AGENT_ERROR,
            recovered=False,
            detail="Evidence gate rejected the manifest.",
            agent="kimi",
            started_at="2026-07-30T06:44:03+00:00",
        ),
    ]
    table = format_attempt_history(results)
    assert "| 6 | 2026-07-30 05:31Z | claude |" in table
    assert "| 1 | 2026-07-30 06:44Z | kimi |" in table
    assert "restarts at 1 on every agent switch and every re-claim" in table


def test_format_attempt_history_tolerates_unusable_start_timestamps() -> None:
    """A malformed or naive start timestamp must not break the whole table."""
    results = [
        AttemptResult(
            attempt_number=1,
            failure_type=FailureType.NO_COMMITS,
            recovered=False,
            detail="Naive timestamp is read as UTC.",
            started_at="2026-07-30T05:31:12",
        ),
        AttemptResult(
            attempt_number=2,
            failure_type=FailureType.NO_COMMITS,
            recovered=False,
            detail="Unparseable timestamp is echoed verbatim.",
            started_at="not-a-timestamp",
        ),
    ]
    table = format_attempt_history(results)
    assert "| 1 | 2026-07-30 05:31Z | - |" in table
    assert "| 2 | not-a-timestamp | - |" in table


_USAGE_LIMIT_STDOUT = (
    "\n[agent error] API Error: Request rejected (429) · usage limit exceeded, "
    "5-hour usage limit reached for Token Plan Max (9917000/9917000 used), "
    "resets at 2026-06-10T15:00:00+08:00 (2056)\n"
)


def _usage_limit_agent_error() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        1,
        ["claude", "--dangerously-skip-permissions", "-p", "HUGE_RECOVERY_PROMPT"],
        output=_USAGE_LIMIT_STDOUT,
        stderr="",
    )


def test_format_attempt_history_keeps_error_tail() -> None:
    """The Detail column should surface the actual error, not boilerplate."""
    detail = format_agent_execution_failure(_usage_limit_agent_error())
    table = format_attempt_history(
        [
            AttemptResult(
                attempt_number=1,
                failure_type=FailureType.AGENT_ERROR,
                recovered=False,
                detail=detail,
            )
        ]
    )
    assert "usage limit exceeded" in table
    assert "resets at 2026-06-10T15:00:00+08:00" in table
    assert "Agent command failed before runner verification" not in table


def test_format_attempt_history_surfaces_validation_reason() -> None:
    """RV evidence failures must show the real reason, not recovery boilerplate.

    Regression for the case where the Detail column only showed "Run the
    validation plan for real…" and hid the actual command exit-code failure.
    """
    reason = (
        "Realistic Validation item 2 failed when keda re-ran its command: "
        "`uv run python -m iar.evidence.run_realistic_validation (item 2)` exited 2."
    )
    table = format_attempt_history(
        [
            AttemptResult(
                attempt_number=1,
                failure_type=FailureType.AGENT_ERROR,
                recovered=False,
                detail=format_validation_evidence_detail(reason),
            )
        ]
    )
    assert "exited 2" in table
    assert "Run the validation plan for real" not in table


def test_format_attempt_history_surfaces_prd_delivery_reason() -> None:
    """PRD delivery failures must show the real reason, not recovery boilerplate."""
    reason = "Acceptance Checklist has 3 unchecked items before archival."
    table = format_attempt_history(
        [
            AttemptResult(
                attempt_number=1,
                failure_type=FailureType.AGENT_ERROR,
                recovered=False,
                detail=format_prd_delivery_detail(reason),
            )
        ]
    )
    assert "3 unchecked items" in table
    assert "Update the canonical PRD" not in table


def test_format_attempt_history_escapes_table_pipes() -> None:
    """Pipes in the detail must not break the Markdown table."""
    table = format_attempt_history(
        [
            AttemptResult(
                attempt_number=1,
                failure_type=FailureType.AGENT_ERROR,
                recovered=False,
                detail="left | right",
            )
        ]
    )
    assert "left \\| right" in table


def test_detect_usage_limit_root_cause() -> None:
    """Usage-limit errors should yield a summary with the reset time."""
    summary = detect_usage_limit_root_cause(_USAGE_LIMIT_STDOUT)
    assert summary is not None
    assert "429" in summary
    assert "2026-06-10T15:00:00+08:00" in summary
    assert detect_usage_limit_root_cause("just lint failed with exit code 1") is None


def test_is_transient_failure_matches_400_invalid_params() -> None:
    """400 / invalid-parameter provider errors are retried like network errors."""
    transient_cases = [
        RuntimeError("API Error: 400 invalid params"),
        RuntimeError("InvalidParameter: input should be a valid dictionary"),
        RuntimeError("BadRequest: malformed request"),
        subprocess.CalledProcessError(
            1, ["claude"], output="Error: 400 invalid request", stderr=""
        ),
    ]
    for exc in transient_cases:
        assert is_transient_failure(exc), f"{exc} should be transient"


def test_is_transient_failure_still_excludes_provider_capacity() -> None:
    """429 / usage-limit errors are not transient so the runner switches agents."""
    capacity_exc = RuntimeError("API Error: 429 usage limit exceeded")
    assert not is_transient_failure(capacity_exc)


def test_format_failure_comment_surfaces_usage_limit_root_cause() -> None:
    """The comment should lead with a root-cause line for usage limit failures."""
    attempt_history = [
        AttemptResult(
            attempt_number=1,
            failure_type=FailureType.AGENT_ERROR,
            recovered=False,
            detail=format_agent_execution_failure(_usage_limit_agent_error()),
        )
    ]
    body = format_failure_comment(MaxRetriesExceededError(attempt_history), attempt_history)
    root_cause_index = body.index("**Root cause:**")
    assert "2026-06-10T15:00:00+08:00" in body
    assert root_cause_index < body.index("### Attempt History")


def test_format_failure_comment_omits_agent_prompt_from_cause() -> None:
    """A CalledProcessError cause must not echo the full agent prompt."""
    attempt_history = [
        AttemptResult(
            attempt_number=1,
            failure_type=FailureType.AGENT_ERROR,
            recovered=False,
            detail="Agent command failed.",
        )
    ]
    failure = MaxRetriesExceededError(attempt_history)
    failure.__cause__ = _usage_limit_agent_error()
    body = format_failure_comment(failure, attempt_history)
    assert "HUGE_RECOVERY_PROMPT" not in body
    assert "Command: `claude`" in body
    assert "usage limit exceeded" in body


def test_format_failure_comment_includes_recovery_guidance() -> None:
    """With an issue number, the comment must end with relabel recovery steps."""
    attempt_history = [
        AttemptResult(
            attempt_number=1,
            failure_type=FailureType.AGENT_ERROR,
            recovered=False,
            detail="Agent command failed.",
        )
    ]
    failure = MaxRetriesExceededError(attempt_history)
    body = format_failure_comment(failure, attempt_history, issue_number=53)
    assert "### How To Recover" in body
    assert "gh issue edit 53 --add-label agent/ready --remove-label agent/failed" in body
    assert "docs/guides/agent-runner.md" in body
    assert body.index("### How To Recover") > body.index("### Attempt History")


def test_format_failure_comment_without_issue_number_unchanged() -> None:
    """Without an issue number, no recovery guidance is appended."""
    attempt_history = [
        AttemptResult(
            attempt_number=1,
            failure_type=FailureType.AGENT_ERROR,
            recovered=False,
            detail="Agent command failed.",
        )
    ]
    body = format_failure_comment(MaxRetriesExceededError(attempt_history))
    assert "gh issue edit" not in body
    assert "How To Recover" not in body


def test_format_failure_comment_transition_to_supervising_recovery() -> None:
    """A failed transition to supervising should suggest retrying that transition."""
    exc = subprocess.CalledProcessError(
        returncode=1,
        cmd=[
            "gh",
            "issue",
            "edit",
            "104",
            "--add-label",
            "agent/supervising",
            "--remove-label",
            "agent/running",
        ],
    )
    body = format_failure_comment(exc, issue_number=104)
    assert "### How To Recover" in body
    assert "gh issue edit 104 --add-label agent/supervising --remove-label agent/failed" in body
    assert "finished its work" in body
    assert "without re-running the agent" in body
    assert "agent/ready" not in body


def test_format_failure_comment_transition_to_review_recovery() -> None:
    """A failed transition to review should suggest retrying that transition."""
    exc = subprocess.CalledProcessError(
        returncode=1,
        cmd=[
            "gh",
            "issue",
            "edit",
            "99",
            "--add-label",
            "agent/review",
            "--remove-label",
            "agent/supervising",
        ],
    )
    body = format_failure_comment(exc, issue_number=99)
    assert "gh issue edit 99 --add-label agent/review --remove-label agent/failed" in body
    assert "finished its work" in body
    assert "agent/ready" not in body


def test_format_failure_comment_non_completion_transition_recovery() -> None:
    """A failed transition to a non-completion label still falls back to ready."""
    exc = subprocess.CalledProcessError(
        returncode=1,
        cmd=[
            "gh",
            "issue",
            "edit",
            "77",
            "--add-label",
            "agent/running",
            "--remove-label",
            "agent/ready",
        ],
    )
    body = format_failure_comment(exc, issue_number=77)
    assert "gh issue edit 77 --add-label agent/ready --remove-label agent/failed" in body
    assert "finished its work" not in body


def test_format_minimal_failure_comment_transition_to_supervising_recovery() -> None:
    """The fallback comment also suggests retrying a completed transition."""
    exc = subprocess.CalledProcessError(
        returncode=1,
        cmd=[
            "gh",
            "issue",
            "edit",
            "104",
            "--add-label",
            "agent/supervising",
            "--remove-label",
            "agent/running",
        ],
    )
    body = format_minimal_failure_comment(exc, issue_number=104)
    assert "### How To Recover" in body
    assert "gh issue edit 104 --add-label agent/supervising --remove-label agent/failed" in body
    assert "finished its work" in body
    assert "agent/ready" not in body


@pytest.mark.parametrize(
    "message",
    [
        "The socket connection was closed unexpectedly",
        "connection reset by peer",
        "upstream connect error 503 service unavailable",
        "Read timed out",
        "502 bad gateway",
    ],
)
def test_is_transient_failure_matches_network_errors(message: str) -> None:
    """Network/transport signatures are classified as transient."""
    assert is_transient_failure(RuntimeError(message))


@pytest.mark.parametrize(
    "message",
    [
        "5-hour usage limit reached",
        "Request rejected (429)",
        "Error 529 overloaded",
        "too many requests",
        "rate limit exceeded",
    ],
)
def test_is_provider_capacity_failure_matches_capacity_errors(message: str) -> None:
    """Capacity / rate-limit signatures are classified as provider capacity."""
    assert is_provider_capacity_failure(RuntimeError(message))


def test_provider_capacity_takes_precedence_over_transient() -> None:
    """A 429 must classify as capacity, never as a retryable transient error."""
    exc = RuntimeError("429 too many requests")
    assert is_provider_capacity_failure(exc)
    assert not is_transient_failure(exc)


def test_transient_predicate_reads_subprocess_output() -> None:
    """Signatures captured in subprocess output must be detected."""
    assert is_transient_failure(transient_command_error())


def test_transient_predicate_ignores_unrelated_errors() -> None:
    """A plain verification failure is not a transient error."""
    assert not is_transient_failure(RuntimeError("verification failed: 3 tests"))


def test_classify_failure_detects_provider_capacity_when_enabled() -> None:
    """classify_failure flags provider capacity only when asked to."""
    exc = CommandFailedError(1, ["claude"], output="usage limit reached", stderr="")
    failure_type = classify_failure(
        before_sha="a",
        after_sha="a",
        has_uncommitted=False,
        agent_result=CommandResult(("",), 0, "", ""),
        verification_results=[],
        exc=exc,
        detect_provider_errors=True,
    )
    assert failure_type == FailureType.PROVIDER_CAPACITY


def test_classify_failure_detects_transient_when_enabled() -> None:
    """classify_failure flags transient errors when detection is enabled."""
    failure_type = classify_failure(
        before_sha="a",
        after_sha="a",
        has_uncommitted=False,
        agent_result=CommandResult(("",), 0, "", ""),
        verification_results=[],
        exc=transient_command_error(),
        detect_provider_errors=True,
    )
    assert failure_type == FailureType.TRANSIENT


def test_classify_failure_ignores_provider_errors_by_default() -> None:
    """Without detection, an agent error mentioning capacity stays AGENT_ERROR.

    Guards against commit/verification failures being reclassified as transient
    just because their output mentions a network word.
    """
    exc = CommandFailedError(1, ["claude"], output="usage limit reached", stderr="")
    failure_type = classify_failure(
        before_sha="a",
        after_sha="a",
        has_uncommitted=False,
        agent_result=CommandResult(("",), 0, "", ""),
        verification_results=[],
        exc=exc,
    )
    assert failure_type == FailureType.AGENT_ERROR


def test_resolve_agent_fallback_order_default_includes_primary_then_chain() -> None:
    """With the default fallback chain, primary agent comes first, then defaults."""
    issue = make_ready_issue()
    order = resolve_agent_fallback_order(issue, AppConfig(), "auto")
    assert order == ["codex", "claude", "kimi"]


def test_resolve_agent_fallback_order_dedupes_and_preserves_order() -> None:
    """The primary agent is de-duplicated and configured order is preserved."""
    issue = make_ready_issue()
    config = AppConfig(runner=RunnerConfig(agent_fallback_order=("codex", "claude", "kimi")))
    order = resolve_agent_fallback_order(issue, config, "auto")
    assert order == ["codex", "claude", "kimi"]


def test_resolve_agent_fallback_order_honors_override_primary() -> None:
    """An explicit --agent override becomes the primary agent."""
    issue = make_ready_issue()
    config = AppConfig(runner=RunnerConfig(agent_fallback_order=("claude", "codex")))
    order = resolve_agent_fallback_order(issue, config, "claude")
    assert order == ["claude", "codex"]


def test_resolve_agent_fallback_order_empty_disables_fallback() -> None:
    """Setting the fallback chain to empty disables cross-agent switching."""
    issue = make_ready_issue()
    config = AppConfig(runner=RunnerConfig(agent_fallback_order=()))
    order = resolve_agent_fallback_order(issue, config, "auto")
    assert order == ["codex"]


def test_format_attempt_duration_shows_phase_breakdown() -> None:
    """Duration 列必须摊出耗时最大的几个阶段，否则"卡了很久"定位不到环节。"""
    from backend.core.shared.models.agent_runner import PhaseDuration
    from backend.core.use_cases.agent_runner_failure import format_attempt_duration

    result = AttemptResult(
        attempt_number=4,
        failure_type=FailureType.TRANSIENT,
        recovered=False,
        detail="kimi died",
        agent="kimi",
        duration_seconds=7153.9,
        phase_durations=(
            PhaseDuration(name="agent", seconds=7100.2),
            PhaseDuration(name="verification", seconds=40.1),
            PhaseDuration(name="rv_reexec", seconds=12.4),
            PhaseDuration(name="commit", seconds=1.2),
        ),
    )

    rendered = format_attempt_duration(result)

    assert rendered.startswith("7153.9s (")
    assert "agent 7100.2s" in rendered
    assert "verification 40.1s" in rendered
    assert "rv_reexec 12.4s" in rendered
    # 只摊前三个，避免把表格单元格撑爆。
    assert "commit" not in rendered


def test_format_attempt_duration_without_phases_is_plain_total() -> None:
    """旧记录没有阶段明细时退回纯总时长，不能渲染出空括号。"""
    from backend.core.use_cases.agent_runner_failure import format_attempt_duration

    result = AttemptResult(
        attempt_number=1,
        failure_type=FailureType.NO_COMMITS,
        recovered=False,
        detail="lint failed",
        agent="claude",
        duration_seconds=12.0,
    )

    assert format_attempt_duration(result) == "12.0s"


def test_format_attempt_duration_hides_negligible_phases() -> None:
    """亚秒级阶段不进单元格——它们不是"卡住"的原因，只会挤掉真正的大头。"""
    from backend.core.shared.models.agent_runner import PhaseDuration
    from backend.core.use_cases.agent_runner_failure import format_attempt_duration

    result = AttemptResult(
        attempt_number=2,
        failure_type=FailureType.SUCCESS,
        recovered=False,
        detail="",
        agent="claude",
        duration_seconds=900.4,
        phase_durations=(
            PhaseDuration(name="agent", seconds=900.0),
            PhaseDuration(name="prd_delivery", seconds=0.2),
        ),
    )

    rendered = format_attempt_duration(result)

    assert rendered == "900.4s (agent 900.0s)"


def test_attempt_history_table_carries_phase_breakdown() -> None:
    """表格渲染必须真的把阶段明细带进 Duration 列。"""
    from backend.core.shared.models.agent_runner import PhaseDuration

    table = format_attempt_history(
        [
            AttemptResult(
                attempt_number=1,
                failure_type=FailureType.VERIFICATION_FAILED,
                recovered=False,
                detail="just test failed",
                agent="claude",
                duration_seconds=1000.0,
                phase_durations=(
                    PhaseDuration(name="verification", seconds=940.0),
                    PhaseDuration(name="agent", seconds=60.0),
                ),
            )
        ]
    )

    assert "verification 940.0s" in table
    assert "agent 60.0s" in table
