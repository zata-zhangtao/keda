"""Tests for Phase 0 ``process_deliberation_issues``.

These tests pin down three core behaviours from the
``deliberate-async-discussion`` PRD:

1. **First round** — a freshly labelled ``agent/deliberate`` Issue gets a
   structured question-list comment plus a fresh
   ``DELIBERATION_QUESTION_PHASE`` marker.
2. **Waiting** — when the AI has just asked and no new user reply is on the
   thread, the next pass must **not** post another comment (no spam).
3. **User reply** — after a user posts a new top-level comment, the marker
   becomes "stale" (issue has more comments than the marker recorded) and the
   next pass posts a new round with ``cycle`` bumped.
4. **Failure isolation** — a single Issue raising during deliberation is
   caught, the Issue gets the ``failed`` label, and processing of any other
   Issue continues.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.core.shared.interfaces.agent_runner import IAgentTranscriptRunner
from backend.core.shared.models.agent_deliberation import (
    DeliberationEvent,
    DeliberationResult,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    LabelConfig,
)
from backend.core.use_cases.agent_runner_deliberation_issues import (
    DELIBERATION_QUESTION_PHASE,
    _build_deliberation_issue_prompt,
    _format_question_list_comment,
    _format_fallback_question_list,
    _resolve_turn,
    process_deliberation_issues,
)
from backend.core.use_cases.agent_runner_events import format_event_marker
from tests.conftest import FakeGitHubClient


_DEFAULT_QUESTION_TEXT = (
    "## 范围边界\n- Q1\n\n## 约束\n- Q2\n\n"
    "## 验收标准\n- Q3\n\n## 技术选型\n- Q4\n\n## 风险\n- Q5"
)


class _StubTranscriptRunner(IAgentTranscriptRunner):
    """Test double: returns a deterministic question-list for any agent call.

    The real ``run_agent_deliberation`` engine only invokes the transcript
    runner for participant agents and the synthesizer; this stub returns the
    same canned text for both, which is enough for the orchestration tests.
    """

    def __init__(
        self,
        question_text: str = _DEFAULT_QUESTION_TEXT,
        raise_on_call: bool = False,
    ) -> None:
        self.question_text = question_text
        self.raise_on_call = raise_on_call
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        event_sink: Callable[[DeliberationEvent], None],
        output_sink: Callable[[str], None] | None = None,
        display_sink: Callable[[str], None] | None = None,
    ) -> CommandResult:
        self.calls.append({"agent_name": agent_name, "prompt": prompt})
        if self.raise_on_call:
            raise RuntimeError("deliberation exploded")
        if output_sink is not None:
            for line in self.question_text.splitlines():
                output_sink(line + "\n")
        return CommandResult(
            command=(agent_name,),
            return_code=0,
            stdout=self.question_text,
            stderr="",
        )


def _build_config(label: str = "agent/deliberate") -> AppConfig:
    """Build an :class:`AppConfig` with the deliberate label hooked up."""
    return AppConfig(labels=LabelConfig(deliberate=label))


def _make_deliberate_issue(
    issue_number: int = 100, body: str = "I want to build X."
) -> IssueSummary:
    return IssueSummary(
        number=issue_number,
        title=f"Deliberation #{issue_number}",
        url=f"https://github.com/example/repo/issues/{issue_number}",
        body=body,
        labels=("agent/deliberate",),
    )


def _set_deliberate_issues(
    fake_github: FakeGitHubClient, issues: list[IssueSummary]
) -> None:
    """Seed the fake client to return ``issues`` from ``list_issues_by_label``.

    ``FakeGitHubClient.list_issues_by_label`` returns an empty list by default
    so we patch the call on the instance to filter the seeded list by label.
    """

    def _list_by_label(
        self: FakeGitHubClient,
        label: str,
        limit: int,
        state: str = "all",
    ) -> list[IssueSummary]:
        self.calls.append(
            {
                "method": "list_issues_by_label",
                "label": label,
                "limit": limit,
                "state": state,
            }
        )
        return [issue for issue in issues if label in issue.labels][:limit]

    fake_github.list_issues_by_label = (  # type: ignore[assignment]
        _list_by_label.__get__(fake_github, FakeGitHubClient)
    )


def test_resolve_turn_first_round_when_no_marker() -> None:
    """No marker on the thread means the AI should post the first round."""
    is_ai_turn, cycle = _resolve_turn(comments=[], marker=None)
    assert is_ai_turn is True
    assert cycle == 1


def test_resolve_turn_continues_after_user_reply() -> None:
    """When more comments exist than the marker recorded, it's the AI's turn."""
    marker = type(
        "M",
        (),
        {"issue_comments_count": 2, "cycle": 1},
    )()
    is_ai_turn, cycle = _resolve_turn(comments=["a", "b", "user reply"], marker=marker)
    assert is_ai_turn is True
    assert cycle == 2


def test_resolve_turn_skips_when_user_has_not_replied() -> None:
    """If the latest AI marker already covers all comments, wait for the user."""
    marker = type(
        "M",
        (),
        {"issue_comments_count": 3, "cycle": 2},
    )()
    is_ai_turn, cycle = _resolve_turn(
        comments=["q1", "q2", "ai question"], marker=marker
    )
    assert is_ai_turn is False
    assert cycle == 3


def test_format_question_list_comment_appends_marker() -> None:
    """The posted comment must end with the iar:event marker HTML comment."""
    labels = LabelConfig(deliberate="agent/deliberate", rework_prd="agent/rework-prd")
    body = _format_question_list_comment(
        synthesis_output="## 范围边界\n- Q",
        cycle=4,
        issue_comments_count_after_post=7,
        include_soft_hint=False,
        label_config=labels,
    )
    assert "Q" in body
    assert "<!-- iar:event" in body
    assert f"phase={DELIBERATION_QUESTION_PHASE}" in body
    assert "cycle=4" in body
    assert "issue_comments_count=7" in body


def test_format_question_list_comment_includes_soft_hint_after_threshold() -> None:
    """When ``include_soft_hint`` is True, append the convergence hint."""
    labels = LabelConfig(deliberate="agent/deliberate", rework_prd="agent/rework-prd")
    body = _format_question_list_comment(
        synthesis_output="## 范围边界\n- Q",
        cycle=3,
        issue_comments_count_after_post=5,
        include_soft_hint=True,
        label_config=labels,
    )
    assert "agent/deliberate" in body
    assert "agent/rework-prd" in body
    assert "Phase 1" in body or "scope" in body.lower()


def test_build_deliberation_issue_prompt_includes_comments() -> None:
    """The assembled prompt must carry the Issue body and the full thread."""
    issue = _make_deliberate_issue(body="Need new feature X")
    prompt = _build_deliberation_issue_prompt(
        issue, comments=["User asked: what about Y?"]
    )
    assert "Need new feature X" in prompt
    assert "User asked: what about Y?" in prompt
    assert "## Issue body" in prompt
    assert "## Issue comments (chronological)" in prompt
    assert "## Your task" in prompt


def test_process_deliberation_issues_first_round_posts_question_list(
    tmp_path: Path,
) -> None:
    """First round: a freshly labelled Issue gets a question list + marker."""
    fake_github = FakeGitHubClient()
    issue = _make_deliberate_issue()
    _set_deliberate_issues(fake_github, [issue])

    runner = _StubTranscriptRunner()
    process_deliberation_issues(
        repo_path=tmp_path,
        config=_build_config(),
        github_client=fake_github,
        transcript_runner_factory=lambda _: runner,
    )

    # The runner called: participant agents in round 1 + synthesizer at least.
    assert len(runner.calls) >= 1
    # The synthesizer should have received the question-list prompt.
    synth_calls = [call for call in runner.calls if "five" in call["prompt"].lower()]
    assert synth_calls, "synthesizer should receive the question-list prompt"

    # Exactly one Issue comment was posted.
    comments = fake_github.list_issue_comments(issue.number)
    assert len(comments) == 1
    posted = comments[0]
    assert "## 范围边界" in posted
    assert f"phase={DELIBERATION_QUESTION_PHASE}" in posted
    assert "cycle=1" in posted
    assert "issue_comments_count=1" in posted


def test_process_deliberation_issues_waiting_skips(tmp_path: Path) -> None:
    """When the AI has just asked and no user reply arrived, skip the pass."""
    fake_github = FakeGitHubClient()
    issue = _make_deliberate_issue()

    # Simulate a previous round having posted a question-list comment with the
    # marker recording ``issue_comments_count=1`` (i.e. the AI's question was
    # the only comment). The next pass should see ``len(comments) ==
    # marker.issue_comments_count`` and skip.
    marker_text = format_event_marker(
        phase=DELIBERATION_QUESTION_PHASE,
        cycle=1,
        issue_comments_count=1,
    )
    ai_question = (
        "## 范围边界\n- Q1\n\n## 约束\n- Q2\n\n## 验收标准\n- Q3\n\n"
        "## 技术选型\n- Q4\n\n## 风险\n- Q5\n\n" + marker_text
    )
    fake_github.comment_issue(issue.number, ai_question)

    _set_deliberate_issues(fake_github, [issue])

    runner = _StubTranscriptRunner()
    process_deliberation_issues(
        repo_path=tmp_path,
        config=_build_config(),
        github_client=fake_github,
        transcript_runner_factory=lambda _: runner,
    )

    # No new transcript runner calls and no new Issue comments.
    assert runner.calls == []
    comments = fake_github.list_issue_comments(issue.number)
    assert len(comments) == 1


def test_process_deliberation_issues_user_reply_continues(tmp_path: Path) -> None:
    """When the user replies, the next pass posts a new round with cycle+1."""
    fake_github = FakeGitHubClient()
    issue = _make_deliberate_issue()

    # First AI question was the only comment when the marker was written, so
    # the marker recorded ``issue_comments_count=1``. The user then replied,
    # bringing the thread to 2 comments, so the next pass should see
    # ``len(comments) > marker.issue_comments_count`` (2 > 1) and post a new
    # question with cycle=2 and ``issue_comments_count=3``.
    marker_text = format_event_marker(
        phase=DELIBERATION_QUESTION_PHASE,
        cycle=1,
        issue_comments_count=1,
    )
    ai_question = (
        "## 范围边界\n- Q1\n\n## 约束\n- Q2\n\n## 验收标准\n- Q3\n\n"
        "## 技术选型\n- Q4\n\n## 风险\n- Q5\n\n" + marker_text
    )
    fake_github.comment_issue(issue.number, ai_question)
    fake_github.comment_issue(issue.number, "User: I prefer option A.")

    _set_deliberate_issues(fake_github, [issue])

    runner = _StubTranscriptRunner()
    process_deliberation_issues(
        repo_path=tmp_path,
        config=_build_config(),
        github_client=fake_github,
        transcript_runner_factory=lambda _: runner,
    )

    comments = fake_github.list_issue_comments(issue.number)
    # Two prior comments + one new AI question = 3.
    assert len(comments) == 3
    new_question = comments[-1]
    assert "cycle=2" in new_question
    # ``issue_comments_count`` should reflect the post-AI count.
    assert "issue_comments_count=3" in new_question


def test_process_deliberation_issues_failure_isolates_to_one_issue(
    tmp_path: Path,
) -> None:
    """A failing Issue gets labelled and commented without crashing the pass."""
    fake_github = FakeGitHubClient()
    bad_issue = _make_deliberate_issue(issue_number=201)
    _set_deliberate_issues(fake_github, [bad_issue])

    runner = _StubTranscriptRunner(raise_on_call=True)
    process_deliberation_issues(
        repo_path=tmp_path,
        config=_build_config(),
        github_client=fake_github,
        transcript_runner_factory=lambda _: runner,
        max_issues=1,
    )

    # The bad issue was labelled ``failed`` and ``agent/deliberate`` removed.
    label_calls = [
        entry
        for entry in fake_github.calls
        if entry.get("method") == "edit_issue_labels"
        and entry.get("issue_number") == 201
    ]
    assert label_calls, "expected edit_issue_labels call for the bad issue"
    assert "agent/failed" in label_calls[0]["add"]
    assert "agent/deliberate" in label_calls[0]["remove"]

    # A failure diagnostic comment was posted.
    failure_comments = [
        body
        for body in fake_github.list_issue_comments(201)
        if "Deliberation phase failed" in body
    ]
    assert failure_comments, "expected a failure diagnostic comment"


def test_process_deliberation_issues_does_not_block_on_transient_failure(
    tmp_path: Path,
) -> None:
    """The orchestrator returns normally even when the runner raises."""
    fake_github = FakeGitHubClient()
    bad_issue = _make_deliberate_issue(issue_number=301)
    _set_deliberate_issues(fake_github, [bad_issue])

    runner = _StubTranscriptRunner(raise_on_call=True)
    # Must not raise — the orchestrator catches per-Issue failures.
    process_deliberation_issues(
        repo_path=tmp_path,
        config=_build_config(),
        github_client=fake_github,
        transcript_runner_factory=lambda _: runner,
    )


def test_process_deliberation_issues_no_marker_no_questions(tmp_path: Path) -> None:
    """Empty thread + no Issues → no-op, no API calls beyond the listing."""
    fake_github = FakeGitHubClient()
    runner = _StubTranscriptRunner()
    process_deliberation_issues(
        repo_path=tmp_path,
        config=_build_config(),
        github_client=fake_github,
        transcript_runner_factory=lambda _: runner,
    )
    assert runner.calls == []
    method_names = [entry["method"] for entry in fake_github.calls]
    # Only the list_issues_by_label call is allowed.
    assert method_names == ["list_issues_by_label"]


def test_format_fallback_question_list_uses_consensus_sections() -> None:
    """When the synthesizer returns blank sections, fall back gracefully."""
    result = DeliberationResult(
        session_id="s",
        prompt="p",
        recommendation="rec text",
        consensus="scope text",
        disagreements="",
        risks="risk text",
        next_actions="acc text",
        events=(),
        agent_outputs={},
        output_dir="d",
        started_at="t",
        finished_at="t",
    )
    fallback = _format_fallback_question_list(result)
    assert "## 范围边界" in fallback
    assert "scope text" in fallback
    assert "## 约束" in fallback
    assert "rec text" in fallback
    assert "## 风险" in fallback
    assert "risk text" in fallback


def test_format_fallback_question_list_handles_fully_blank_sections() -> None:
    """A fully empty DeliberationResult still produces a usable scaffold."""
    result = DeliberationResult(
        session_id="s",
        prompt="p",
        recommendation="",
        consensus="",
        disagreements="",
        risks="",
        next_actions="",
        events=(),
        agent_outputs={},
        output_dir="d",
        started_at="t",
        finished_at="t",
    )
    fallback = _format_fallback_question_list(result)
    assert "## 范围边界" in fallback
    assert "did not produce concrete questions" in fallback.lower()
