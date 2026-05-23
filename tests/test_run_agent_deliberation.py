"""Tests for multi-agent deliberation session."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


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

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[dict] = []

    def run(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        event_sink: Callable[[DeliberationEvent], None],
    ) -> CommandResult:
        self.calls.append({"agent_name": agent_name, "prompt": prompt, "cwd": cwd})
        output = self.responses.get(agent_name, "default output")
        return CommandResult(
            command=(agent_name,),
            return_code=0,
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
    return DeliberationConfig(profiles=profiles)


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
    fake_runner = FakeTranscriptRunner(
        responses={"claude": "architect out", "kimi": "skeptic out"}
    )
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
