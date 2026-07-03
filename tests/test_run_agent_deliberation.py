"""Tests for multi-agent deliberation session."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from backend.core.shared.interfaces.agent_runner import IAgentTranscriptRunner
from backend.core.shared.models.agent_deliberation import (
    DeliberationAgentProfile,
    DeliberationConfig,
    DeliberationEvent,
    DeliberationRequest,
    DeliberationResult,
)
from backend.core.shared.models.agent_runner import CommandResult
from backend.core.use_cases.run_agent_deliberation import (
    _build_discussion_prompt,
    _build_isolated_prompt,
    _build_synthesis_prompt,
    _default_session_id,
    _parse_synthesis,
    run_agent_deliberation,
)


class FakeTranscriptRunner(IAgentTranscriptRunner):
    """In-memory transcript runner for tests."""

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        return_codes: dict[str, int] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.return_codes = return_codes or {}
        self.calls: list[dict] = []

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
        self.calls.append({"agent_name": agent_name, "prompt": prompt, "cwd": cwd})
        output = self.responses.get(agent_name, "default output")
        # Simulate streaming output if output_sink is provided
        if output_sink is not None:
            for line in output.splitlines():
                output_sink(line)
        return CommandResult(
            command=(agent_name,),
            return_code=self.return_codes.get(agent_name, 0),
            stdout=output,
            stderr="",
        )


def _make_request(
    prompt: str = "test prompt",
    agents: tuple[str, ...] = ("architect", "skeptic"),
    rounds: int = 1,
    synthesizer: str = "claude",
    output_dir: str = "logs/deliberations",
    session_id: str | None = "test-session",
) -> DeliberationRequest:
    return DeliberationRequest(
        prompt=prompt,
        agents=agents,
        rounds=rounds,
        synthesizer=synthesizer,
        output_dir=output_dir,
        session_id=session_id,
    )


def _make_config(
    profiles: tuple[DeliberationAgentProfile, ...] | None = None,
    continue_on_agent_error: bool = True,
) -> DeliberationConfig:
    if profiles is None:
        profiles = (
            DeliberationAgentProfile(
                profile_id="architect",
                agent="claude",
                role="architect",
                behavior_prompt="be an architect",
            ),
            DeliberationAgentProfile(
                profile_id="skeptic",
                agent="kimi",
                role="skeptic",
                behavior_prompt="be a skeptic",
            ),
        )
    return DeliberationConfig(profiles=profiles, continue_on_agent_error=continue_on_agent_error)


def test_build_isolated_prompt_excludes_transcript() -> None:
    """Round 1 prompt must not include other agent outputs."""
    request = _make_request(prompt="Implement auth.")
    profile = _make_config().profiles[0]
    prompt = _build_isolated_prompt(request, profile)
    assert "Implement auth." in prompt
    assert "architect" in prompt
    assert "be an architect" in prompt
    assert "transcript" not in prompt.lower()
    assert "Do NOT modify any files" in prompt


def test_build_discussion_prompt_includes_transcript() -> None:
    """Round 2+ prompt must include public transcript."""
    request = _make_request(prompt="Implement auth.")
    profile = _make_config().profiles[0]
    transcript = "## Round 1\n\n### architect\n\nDesign looks good."
    prompt = _build_discussion_prompt(request, profile, transcript)
    assert "Implement auth." in prompt
    assert transcript in prompt
    assert "public transcript" in prompt.lower()
    assert "Do NOT modify any files" in prompt


def test_build_synthesis_prompt_structured() -> None:
    """Synthesizer prompt must request structured sections."""
    request = _make_request(prompt="Implement auth.")
    transcript = "## Round 1\n\narchitect says yes."
    prompt = _build_synthesis_prompt(request, transcript)
    assert "Implement auth." in prompt
    assert transcript in prompt
    assert "## Recommendation" in prompt
    assert "## Consensus" in prompt
    assert "## Disagreements" in prompt
    assert "## Risks" in prompt
    assert "## Next Actions" in prompt


def test_default_session_id_format() -> None:
    """Session ID should be a timestamp-like string."""
    session_id = _default_session_id()
    assert len(session_id) == 19
    assert session_id[8] == "-"
    assert session_id[15] == "-"


def test_parse_synthesis_extracts_sections() -> None:
    """Synthesis parser should extract markdown sections."""
    text = """## Recommendation
Use OAuth2.

## Consensus
Team agrees.

## Disagreements
None.

## Risks
Latency.

## Next Actions
Implement token refresh."""
    parsed = _parse_synthesis(text)
    assert parsed["recommendation"] == "Use OAuth2."
    assert parsed["consensus"] == "Team agrees."
    assert parsed["disagreements"] == "None."
    assert parsed["risks"] == "Latency."
    assert parsed["next_actions"] == "Implement token refresh."


def test_parse_synthesis_accepts_heading_variants() -> None:
    """Synthesis parser should tolerate common markdown heading variants."""
    text = """### Recommendation:
Use OAuth2.

## RISKS:
Latency.

#### Next Actions
Implement token refresh."""
    parsed = _parse_synthesis(text)
    assert parsed["recommendation"] == "Use OAuth2."
    assert parsed["risks"] == "Latency."
    assert parsed["next_actions"] == "Implement token refresh."


def test_parse_synthesis_empty_for_missing_sections() -> None:
    """Missing sections should return empty strings."""
    parsed = _parse_synthesis("No sections here.")
    assert parsed["recommendation"] == ""
    assert parsed["consensus"] == ""


def test_run_agent_deliberation_isolation_round_only(
    tmp_path: Path,
) -> None:
    """Single-round deliberation should run agents in isolation."""
    request = _make_request(rounds=1)
    config = _make_config()
    fake_runner = FakeTranscriptRunner(
        responses={"claude": "architect output", "kimi": "skeptic output"}
    )
    events: list[DeliberationEvent] = []

    result = run_agent_deliberation(
        request=request,
        config=config,
        transcript_runner=fake_runner,
        event_sink=events.append,
        target_repo_path=tmp_path,
    )

    assert isinstance(result, DeliberationResult)
    assert result.prompt == "test prompt"
    assert result.events
    assert result.events[-1].event_type == "session_finished"
    # 2 agents + 1 synthesizer = 3 calls
    assert len(fake_runner.calls) == 3
    # Verify isolation: round-1 prompts should not contain other agent outputs.
    # The synthesizer (claude) prompt contains the transcript, so skip it.
    # Actually architect is also claude; check by looking at prompts before synthesis.
    # The first two calls are round 1 agents.
    round1_calls = fake_runner.calls[:2]
    for call in round1_calls:
        assert "architect output" not in call["prompt"]
        assert "skeptic output" not in call["prompt"]


def test_run_agent_deliberation_two_rounds_injects_transcript(
    tmp_path: Path,
) -> None:
    """Round 2 prompt should include round 1 outputs."""
    request = _make_request(rounds=2)
    config = _make_config()
    fake_runner = FakeTranscriptRunner(
        responses={"claude": "architect output", "kimi": "skeptic output"}
    )
    events: list[DeliberationEvent] = []

    result = run_agent_deliberation(
        request=request,
        config=config,
        transcript_runner=fake_runner,
        event_sink=events.append,
        target_repo_path=tmp_path,
    )

    assert isinstance(result, DeliberationResult)
    # 2 agents * 2 rounds + 1 synthesizer = 5 calls
    assert len(fake_runner.calls) == 5
    round2_calls = [c for c in fake_runner.calls if "architect output" in c["prompt"]]
    assert len(round2_calls) >= 1


def test_run_agent_deliberation_output_files_written(
    tmp_path: Path,
) -> None:
    """Deliberation should write output files."""
    request = _make_request(rounds=1, output_dir=str(tmp_path))
    config = _make_config()
    fake_runner = FakeTranscriptRunner(responses={"claude": "architect out", "kimi": "skeptic out"})
    events: list[DeliberationEvent] = []

    result = run_agent_deliberation(
        request=request,
        config=config,
        transcript_runner=fake_runner,
        event_sink=events.append,
        target_repo_path=tmp_path,
    )

    output_dir = Path(result.output_dir)
    assert output_dir == tmp_path
    assert (output_dir / "workspaces").is_dir()
    # Streaming sink writes chunks verbatim (no forced newline per chunk)
    assert (output_dir / "workspaces" / "architect" / "round-1-output.md").read_text(
        encoding="utf-8"
    ) == "architect out"
    assert (output_dir / "workspaces" / "skeptic" / "round-1-output.md").read_text(
        encoding="utf-8"
    ) == "skeptic out"
    assert (output_dir / "workspaces" / "synthesizer" / "synthesis-output.md").read_text(
        encoding="utf-8"
    ) == "architect out"


class _DisplayEmittingRunner(IAgentTranscriptRunner):
    """Runner that emits a progress chunk via display_sink only."""

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
        if display_sink is not None:
            display_sink(f"reasoning:{agent_name}")
        if output_sink is not None:
            output_sink(f"answer:{agent_name}")
        return CommandResult(
            command=(agent_name,),
            return_code=0,
            stdout=f"answer:{agent_name}",
            stderr="",
        )


class _RecordingOutputView:
    """Output view that records appended chunks per profile."""

    def __init__(self) -> None:
        self.chunks: list[tuple[str, str]] = []

    def register_round_profiles(self, round_number, profiles) -> None:
        _ = round_number, profiles

    def append_output(self, round_number, profile_id, chunk) -> None:
        _ = round_number
        self.chunks.append((profile_id, chunk))

    def update_status(self, round_number, profile_id, status) -> None:
        _ = round_number, profile_id, status

    def log(self, message) -> None:
        _ = message

    def close(self) -> None:
        pass


def test_display_sink_shown_but_not_persisted(tmp_path: Path) -> None:
    """display_sink chunks reach the view but never the file or transcript."""
    request = _make_request(agents=("skeptic",), rounds=1, output_dir=str(tmp_path))
    config = _make_config()
    view = _RecordingOutputView()

    result = run_agent_deliberation(
        request=request,
        config=config,
        transcript_runner=_DisplayEmittingRunner(),
        event_sink=lambda _: None,
        target_repo_path=tmp_path,
        output_view=view,
    )

    # The reasoning (display_sink) is visible in the live view...
    assert ("skeptic", "reasoning:kimi") in view.chunks
    # ...but only the answer (output_sink) is persisted to the workspace file.
    workspace_file = tmp_path / "workspaces" / "skeptic" / "round-1-output.md"
    file_text = workspace_file.read_text(encoding="utf-8")
    assert "answer:kimi" in file_text
    assert "reasoning:kimi" not in file_text
    # ...and only the answer reaches the transcript-bound outputs.
    assert result.agent_outputs["round_1"] == {"skeptic": "answer:kimi"}


def test_run_agent_deliberation_synthesizer_produces_result(
    tmp_path: Path,
) -> None:
    """Synthesizer output should be parsed into result sections."""
    synthesis_text = """## Recommendation
Build it.

## Consensus
All agree.

## Disagreements
None.

## Risks
Low.

## Next Actions
Start coding."""
    request = _make_request(rounds=1)
    config = _make_config(
        profiles=(
            DeliberationAgentProfile(
                profile_id="architect",
                agent="architect-agent",
                role="architect",
                behavior_prompt="be an architect",
            ),
            DeliberationAgentProfile(
                profile_id="skeptic",
                agent="kimi",
                role="skeptic",
                behavior_prompt="be a skeptic",
            ),
        )
    )
    fake_runner = FakeTranscriptRunner(
        responses={
            "architect-agent": "architect out",
            "kimi": "skeptic out",
            "claude": synthesis_text,
        }
    )

    events: list[DeliberationEvent] = []

    result = run_agent_deliberation(
        request=request,
        config=config,
        transcript_runner=fake_runner,
        event_sink=events.append,
        target_repo_path=tmp_path,
    )

    assert result.recommendation == "Build it."
    assert result.consensus == "All agree."
    assert result.next_actions == "Start coding."


def test_run_agent_deliberation_preserves_profile_ids_in_outputs(
    tmp_path: Path,
) -> None:
    """Round outputs should keep profile IDs for transcript rendering."""
    request = _make_request(agents=("skeptic",), rounds=1)
    config = _make_config()
    fake_runner = FakeTranscriptRunner(responses={"kimi": "skeptic output", "claude": "summary"})

    result = run_agent_deliberation(
        request=request,
        config=config,
        transcript_runner=fake_runner,
        event_sink=lambda _: None,
        target_repo_path=tmp_path,
    )

    assert result.agent_outputs["round_1"] == {"skeptic": "skeptic output"}
    assert "### skeptic" in fake_runner.calls[-1]["prompt"]


def test_run_agent_deliberation_raises_on_agent_failure(
    tmp_path: Path,
) -> None:
    """A failed participant should fail the deliberation when error tolerance is off."""
    request = _make_request(agents=("skeptic",), rounds=1)
    config = _make_config(continue_on_agent_error=False)
    events: list[DeliberationEvent] = []
    fake_runner = FakeTranscriptRunner(
        responses={"kimi": "tool failed"},
        return_codes={"kimi": 7},
    )

    with pytest.raises(RuntimeError, match="skeptic.*exit code 7"):
        run_agent_deliberation(
            request=request,
            config=config,
            transcript_runner=fake_runner,
            event_sink=events.append,
            target_repo_path=tmp_path,
        )

    assert any(
        event.agent == "skeptic"
        and event.event_type == "agent_finished"
        and event.message == "exit=7"
        for event in events
    )


def test_run_agent_deliberation_raises_on_synthesizer_failure(
    tmp_path: Path,
) -> None:
    """A failed synthesizer should fail the deliberation when error tolerance is off."""
    request = _make_request(agents=("skeptic",), rounds=1, synthesizer="synth")
    config = _make_config(continue_on_agent_error=False)
    events: list[DeliberationEvent] = []
    fake_runner = FakeTranscriptRunner(
        responses={"kimi": "skeptic output", "synth": "synth failed"},
        return_codes={"synth": 9},
    )

    with pytest.raises(RuntimeError, match="synthesizer.*exit code 9"):
        run_agent_deliberation(
            request=request,
            config=config,
            transcript_runner=fake_runner,
            event_sink=events.append,
            target_repo_path=tmp_path,
        )

    assert any(
        event.agent == "synthesizer"
        and event.event_type == "agent_finished"
        and event.message == "exit=9"
        for event in events
    )


def test_run_agent_deliberation_isolates_single_agent_failure(
    tmp_path: Path,
) -> None:
    """Default behavior continues when one participant fails and records it."""
    request = _make_request(agents=("architect", "skeptic"), rounds=1)
    config = _make_config()
    events: list[DeliberationEvent] = []
    fake_runner = FakeTranscriptRunner(
        responses={"claude": "architect output", "kimi": "skeptic failed"},
        return_codes={"kimi": 5},
    )

    result = run_agent_deliberation(
        request=request,
        config=config,
        transcript_runner=fake_runner,
        event_sink=events.append,
        target_repo_path=tmp_path,
    )

    assert isinstance(result, DeliberationResult)
    assert len(result.failed_agents) == 1
    failure = result.failed_agents[0]
    assert failure.profile_id == "skeptic"
    assert failure.attempted_agent == "kimi"
    assert failure.fallback_agent is None
    assert failure.reason == "exit=5"
    assert result.agent_outputs["round_1"]["architect"] == "architect output"
    assert "skeptic" in result.agent_outputs["round_1"]


def test_run_agent_deliberation_fallback_resolver_retries_failed_agent(
    tmp_path: Path,
) -> None:
    """Resolver returning a fallback re-runs the failed profile with a new agent."""
    request = _make_request(agents=("architect", "skeptic"), rounds=1)
    config = _make_config()
    events: list[DeliberationEvent] = []
    fake_runner = FakeTranscriptRunner(
        responses={
            "claude": "architect output",
            "kimi": "skeptic failed",
            "codex": "skeptic fallback output",
        },
        return_codes={"kimi": 5, "codex": 0},
    )

    def _resolver(profile, reason):
        _ = reason
        if profile.agent == "kimi":
            return DeliberationAgentProfile(
                profile_id=profile.profile_id,
                agent="codex",
                role=profile.role,
                behavior_prompt=profile.behavior_prompt,
            )
        return None

    result = run_agent_deliberation(
        request=request,
        config=config,
        transcript_runner=fake_runner,
        event_sink=events.append,
        target_repo_path=tmp_path,
        resolver=_resolver,
    )

    assert isinstance(result, DeliberationResult)
    assert len(result.failed_agents) == 1
    failure = result.failed_agents[0]
    assert failure.profile_id == "skeptic"
    assert failure.attempted_agent == "kimi"
    assert failure.fallback_agent == "codex"
    assert result.agent_outputs["round_1"]["skeptic"] == "skeptic fallback output"


def test_run_agent_deliberation_continues_when_synthesizer_fails(
    tmp_path: Path,
) -> None:
    """Default behavior returns empty result sections when synthesizer fails."""
    request = _make_request(agents=("skeptic",), rounds=1, synthesizer="synth")
    config = _make_config()
    events: list[DeliberationEvent] = []
    fake_runner = FakeTranscriptRunner(
        responses={"kimi": "skeptic output", "synth": "synth failed"},
        return_codes={"synth": 3},
    )

    result = run_agent_deliberation(
        request=request,
        config=config,
        transcript_runner=fake_runner,
        event_sink=events.append,
        target_repo_path=tmp_path,
    )

    assert isinstance(result, DeliberationResult)
    assert result.recommendation == ""
    assert result.failed_agents[0].profile_id == "synthesizer"
    assert result.failed_agents[0].attempted_agent == "synth"


def test_run_agent_deliberation_uses_injected_synthesis_prompt_builder(
    tmp_path: Path,
) -> None:
    """A custom synthesis_prompt_builder should override the default prompt."""
    request = _make_request(agents=("skeptic",), rounds=1)
    config = _make_config()
    fake_runner = FakeTranscriptRunner(responses={"kimi": "skeptic output", "claude": "summary"})

    seen_prompts: list[str] = []

    def _custom_builder(req: DeliberationRequest, transcript: str) -> str:
        seen_prompts.append("custom")
        return f"Custom synthesis prompt for {req.prompt!r} with {transcript!r}"

    run_agent_deliberation(
        request=request,
        config=config,
        transcript_runner=fake_runner,
        event_sink=lambda _: None,
        target_repo_path=tmp_path,
        synthesis_prompt_builder=_custom_builder,
    )

    assert seen_prompts == ["custom"]
    # The synthesizer call received the custom prompt, not the default one.
    synth_call = fake_runner.calls[-1]
    assert "Custom synthesis prompt" in synth_call["prompt"]
    assert "## Recommendation" not in synth_call["prompt"]


def test_run_agent_deliberation_default_synthesis_when_builder_omitted(
    tmp_path: Path,
) -> None:
    """No builder → existing 5-section ``_build_synthesis_prompt`` is used."""
    request = _make_request(agents=("skeptic",), rounds=1)
    config = _make_config()
    fake_runner = FakeTranscriptRunner(responses={"kimi": "skeptic output", "claude": "summary"})

    run_agent_deliberation(
        request=request,
        config=config,
        transcript_runner=fake_runner,
        event_sink=lambda _: None,
        target_repo_path=tmp_path,
    )

    synth_call = fake_runner.calls[-1]
    # Default builder still injects the canonical 5 sections.
    assert "## Recommendation" in synth_call["prompt"]
    assert "## Consensus" in synth_call["prompt"]
    assert "## Next Actions" in synth_call["prompt"]


def test_run_agent_deliberation_custom_synthesis_propagates_to_result(
    tmp_path: Path,
) -> None:
    """A custom builder doesn't change the section parser; result sections reflect it."""
    # Use the canonical 5-section header so the parser still extracts content.
    custom_synthesis = (
        "## Recommendation\nUse canonical headers.\n\n"
        "## Consensus\n- Q1\n\n"
        "## Disagreements\n- Q2\n\n"
        "## Risks\n- Q3\n\n"
        "## Next Actions\n- Q4"
    )
    request = _make_request(agents=("skeptic",), rounds=1)
    config = _make_config()
    fake_runner = FakeTranscriptRunner(
        responses={"kimi": "skeptic output", "claude": custom_synthesis}
    )

    def _question_list_builder(req: DeliberationRequest, transcript: str) -> str:
        return "Synthesize a structured clarifying-question list."

    result = run_agent_deliberation(
        request=request,
        config=config,
        transcript_runner=fake_runner,
        event_sink=lambda _: None,
        target_repo_path=tmp_path,
        synthesis_prompt_builder=_question_list_builder,
    )

    # With canonical headers the parser populates the standard fields.
    assert "Use canonical headers" in result.recommendation
    assert "Q1" in result.consensus
    assert "Q2" in result.disagreements
    assert "Q3" in result.risks
    assert "Q4" in result.next_actions
