"""Multi-agent deliberation session orchestrator."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from backend.core.shared.interfaces.agent_runner import (
    IAgentTranscriptRunner,
)
from backend.core.shared.models.agent_deliberation import (
    DeliberationAgentFailure,
    DeliberationAgentProfile,
    DeliberationConfig,
    DeliberationEvent,
    DeliberationRequest,
    DeliberationResult,
)
from backend.core.shared.interfaces.agent_output_view import IAgentOutputView

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)

_SYNTHESIS_HEADER_PATTERN = re.compile(
    r"^#{2,6}\s+(recommendation|consensus|disagreements|risks|next actions)\s*:?\s*$",
    re.IGNORECASE,
)

_READ_ONLY_RULES = """\nRead-only deliberation rules:
- Do NOT modify any files in the workspace.
- Do NOT run git add, git commit, git push, or create PRs.
- Do NOT create, delete, or rename files.
- Do NOT execute commands that modify the repository or system state.
- Provide your analysis in plain text only.
- Finish with a concise, structured answer."""


def _build_isolated_prompt(
    request: DeliberationRequest,
    profile: DeliberationAgentProfile,
) -> str:
    return (
        f"{request.prompt}\n\n"
        f"Your role: {profile.role}\n"
        f"{profile.behavior_prompt}"
        f"{_READ_ONLY_RULES}"
    )


def _build_discussion_prompt(
    request: DeliberationRequest,
    profile: DeliberationAgentProfile,
    transcript: str,
) -> str:
    return (
        f"{request.prompt}\n\n"
        f"Your role: {profile.role}\n"
        f"{profile.behavior_prompt}\n\n"
        "Here is the public transcript of the discussion so far:\n"
        "---\n"
        f"{transcript}\n"
        "---\n\n"
        "Respond to the points raised by other participants. "
        "Challenge, refine, or defend positions as appropriate."
        f"{_READ_ONLY_RULES}"
    )


def _build_synthesis_prompt(
    request: DeliberationRequest,
    transcript: str,
) -> str:
    return (
        f"Original request:\n{request.prompt}\n\n"
        "Here is the full public transcript of the multi-agent deliberation:\n"
        "---\n"
        f"{transcript}\n"
        "---\n\n"
        "Please synthesize the discussion into a structured report with these sections:\n"
        "## Recommendation\n"
        "## Consensus\n"
        "## Disagreements\n"
        "## Risks\n"
        "## Next Actions\n\n"
        "Be concise and actionable."
        f"{_READ_ONLY_RULES}"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(
    event_sink: Callable[[DeliberationEvent], None],
    session_id: str,
    round_number: int,
    agent: str,
    event_type: str,
    message: str,
) -> None:
    event_sink(
        DeliberationEvent(
            session_id=session_id,
            round=round_number,
            agent=agent,
            event_type=event_type,
            message=message,
            timestamp=_now_iso(),
        )
    )


def _write_workspace_output(output_path: Path, output_text: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        output_text + ("\n" if output_text and not output_text.endswith("\n") else ""),
        encoding="utf-8",
    )


@dataclass(frozen=True)
class AgentRunOutcome:
    """Outcome of running a single deliberation agent."""

    success: bool
    stdout: str
    return_code: int
    profile_id: str


def _create_streaming_sink(
    output_path: Path,
    output_view: IAgentOutputView,
    round_number: int,
    profile_id: str,
) -> Callable[[str], None]:
    """Create a sink that appends chunks to a workspace file and updates the view.

    The workspace file is created (or truncated) on first call, then
    each subsequent chunk is appended. This enables real-time file
    growth while the agent subprocess is running.
    """
    # Ensure the file exists before any chunk arrives so an agent that
    # produces no output still leaves an empty workspace file.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")
    lock = threading.Lock()

    def _sink(chunk: str) -> None:
        with lock:
            with open(output_path, "a", encoding="utf-8") as f:
                f.write(chunk)
        output_view.append_output(round_number, profile_id, chunk)

    return _sink


def _create_display_sink(
    output_view: IAgentOutputView,
    round_number: int,
    profile_id: str,
) -> Callable[[str], None]:
    """Create a display-only sink that updates the live view.

    Unlike the streaming sink, this does not write to the workspace file
    and its chunks are never collected into the transcript. It is used for
    transient progress output (e.g. an agent's reasoning/tool log on stderr)
    so the panel shows live activity without polluting the saved output.
    """

    def _sink(chunk: str) -> None:
        output_view.append_output(round_number, profile_id, chunk)

    return _sink


def _run_single_agent(
    agent_name: str,
    prompt: str,
    cwd: Path,
    transcript_runner: IAgentTranscriptRunner,
    event_sink: Callable[[DeliberationEvent], None],
    session_id: str,
    round_number: int,
    agent_label: str,
    output_view: IAgentOutputView,
    output_sink: Callable[[str], None] | None = None,
    display_sink: Callable[[str], None] | None = None,
) -> "AgentRunOutcome":
    _emit(
        event_sink,
        session_id,
        round_number,
        agent_label,
        "agent_started",
        "started analysis",
    )
    output_view.update_status(round_number, agent_label, "running")
    result = transcript_runner.run(
        agent_name=agent_name,
        prompt=prompt,
        cwd=cwd,
        event_sink=event_sink,
        output_sink=output_sink,
        display_sink=display_sink,
    )
    output_text = result.stdout.strip()
    if output_sink is None:
        _write_workspace_output(cwd / f"round-{round_number}-output.md", output_text)
    final_status = "finished" if result.return_code == 0 else "failed"
    output_view.update_status(round_number, agent_label, final_status)
    _emit(
        event_sink,
        session_id,
        round_number,
        agent_label,
        "agent_finished",
        f"exit={result.return_code}",
    )
    return AgentRunOutcome(
        success=result.return_code == 0,
        stdout=output_text,
        return_code=result.return_code,
        profile_id=agent_label,
    )


def _run_round(
    request: DeliberationRequest,
    profiles: tuple[DeliberationAgentProfile, ...],
    round_number: int,
    prompt_builder: Callable[[DeliberationAgentProfile], str],
    transcript_runner: IAgentTranscriptRunner,
    event_sink: Callable[[DeliberationEvent], None],
    workspace_root: Path,
    session_id: str,
    output_view: IAgentOutputView,
    config: DeliberationConfig,
    resolver: "Callable[[DeliberationAgentProfile, str], DeliberationAgentProfile | None] | None" = None,
) -> tuple[dict[str, str], list[DeliberationAgentFailure]]:
    outputs: dict[str, str] = {}
    failed_agents: list[DeliberationAgentFailure] = []

    # Register profiles with the output view for this round
    output_view.register_round_profiles(round_number, profiles)

    def _run_profile(profile: DeliberationAgentProfile) -> AgentRunOutcome:
        prompt = prompt_builder(profile)
        profile_workspace = workspace_root / profile.profile_id
        profile_workspace.mkdir(parents=True, exist_ok=True)
        output_path = profile_workspace / f"round-{round_number}-output.md"
        streaming_sink = _create_streaming_sink(
            output_path, output_view, round_number, profile.profile_id
        )
        display_sink = _create_display_sink(output_view, round_number, profile.profile_id)
        return _run_single_agent(
            agent_name=profile.agent,
            prompt=prompt,
            cwd=profile_workspace,
            transcript_runner=transcript_runner,
            event_sink=event_sink,
            session_id=session_id,
            round_number=round_number,
            agent_label=profile.profile_id,
            output_view=output_view,
            output_sink=streaming_sink,
            display_sink=display_sink,
        )

    outcomes: list[AgentRunOutcome] = []
    with ThreadPoolExecutor(max_workers=len(profiles)) as executor:
        futures = [executor.submit(_run_profile, profile) for profile in profiles]
        for future in futures:
            outcomes.append(future.result())

    for outcome in outcomes:
        if outcome.success:
            outputs[outcome.profile_id] = outcome.stdout
            continue
        if not config.continue_on_agent_error:
            raise RuntimeError(
                f"Deliberation agent '{outcome.profile_id}' failed with exit code "
                f"{outcome.return_code}."
            )
        profile_by_id = {p.profile_id: p for p in profiles}
        failed_profile = profile_by_id[outcome.profile_id]
        reason = f"exit={outcome.return_code}"
        fallback: DeliberationAgentProfile | None = None
        if resolver is not None:
            try:
                fallback = resolver(failed_profile, reason, config)
            except TypeError:
                fallback = resolver(failed_profile, reason)
        if fallback is not None:
            _emit(
                event_sink,
                session_id,
                round_number,
                failed_profile.profile_id,
                "agent_fallback",
                f"fallback={fallback.agent}",
            )
            fallback_outcome = _run_profile(fallback)
            if fallback_outcome.success:
                outputs[fallback.profile_id] = fallback_outcome.stdout
                failed_agents.append(
                    DeliberationAgentFailure(
                        profile_id=failed_profile.profile_id,
                        attempted_agent=failed_profile.agent,
                        fallback_agent=fallback.agent,
                        reason=reason,
                    )
                )
                continue
            reason = f"fallback={fallback.agent} exit={fallback_outcome.return_code}"
        outputs[failed_profile.profile_id] = outcome.stdout
        failed_agents.append(
            DeliberationAgentFailure(
                profile_id=failed_profile.profile_id,
                attempted_agent=failed_profile.agent,
                fallback_agent=None,
                reason=reason,
            )
        )

    return outputs, failed_agents


def _format_round_transcript(
    round_number: int,
    outputs: dict[str, str],
) -> str:
    lines = [f"## Round {round_number}: Discussion\n"]
    for profile_id, output in outputs.items():
        lines.append(f"### {profile_id}\n\n{output}\n")
    return "\n".join(lines)


def _parse_synthesis(stdout: str) -> dict[str, str]:
    sections = {
        "recommendation": "",
        "consensus": "",
        "disagreements": "",
        "risks": "",
        "next_actions": "",
    }
    current_key: str | None = None
    buffer_lines: list[str] = []
    header_map = {
        "recommendation": "recommendation",
        "consensus": "consensus",
        "disagreements": "disagreements",
        "risks": "risks",
        "next actions": "next_actions",
    }
    for line in stdout.splitlines():
        header_match = _SYNTHESIS_HEADER_PATTERN.match(line.strip())
        if header_match is not None:
            if current_key is not None:
                sections[current_key] = "\n".join(buffer_lines).strip()
            current_key = header_map[header_match.group(1).lower()]
            buffer_lines = []
            continue
        if current_key is not None:
            buffer_lines.append(line)
    if current_key is not None:
        sections[current_key] = "\n".join(buffer_lines).strip()
    return sections


def run_agent_deliberation(
    request: DeliberationRequest,
    config: DeliberationConfig,
    transcript_runner: IAgentTranscriptRunner,
    event_sink: Callable[[DeliberationEvent], None],
    target_repo_path: Path,
    output_view: IAgentOutputView | None = None,
    resolver: "Callable[[DeliberationAgentProfile, str], DeliberationAgentProfile | None] | None" = None,
    synthesis_prompt_builder: Callable[[DeliberationRequest, str], str] | None = None,
) -> DeliberationResult:
    """Run a multi-agent deliberation session.

    Args:
        request: User request.
        config: Deliberation configuration with profiles.
        transcript_runner: Runner that executes agents and emits events.
        event_sink: Callback for structured events.
        target_repo_path: Path to the target repository for read-only safety checks.
        output_view: Optional output view for live terminal display. If not
            provided, a NoOpOutputView is used.
        resolver: Optional callable that decides how to recover from a single
            agent failure. Receives the failed profile and a reason string, and
            returns a fallback profile or None. If None, failures are recorded
            without retry.
        synthesis_prompt_builder: Optional callable that builds the synthesizer
            prompt from the original ``request`` and the full discussion
            ``transcript``. When ``None`` the built-in
            :func:`_build_synthesis_prompt` is used (the historical
            ``iar deliberate`` 5-section report shape); callers such as the
            Phase 0 ``agent/deliberate`` queue pass a custom builder that
            asks for a structured clarifying-question list instead.

    Returns:
        DeliberationResult with the final report.
    """
    from backend.core.shared.interfaces.agent_output_view import NoOpOutputView

    if output_view is None:
        output_view = NoOpOutputView()

    try:
        session_id = request.session_id or create_default_session_id()
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        workspace_root = output_dir / "workspaces"
        workspace_root.mkdir(parents=True, exist_ok=True)

        started_at = _now_iso()
        emitted_events: list[DeliberationEvent] = []
        session_failed_agents: list[DeliberationAgentFailure] = []

        def _record_event(event: DeliberationEvent) -> None:
            emitted_events.append(event)
            event_sink(event)

        _emit(
            _record_event,
            session_id,
            0,
            "system",
            "session_started",
            f"session={session_id} rounds={request.rounds}",
        )

        profiles_by_id = {profile.profile_id: profile for profile in config.profiles}
        selected_profiles = tuple(
            profiles_by_id[profile_id]
            for profile_id in request.agents
            if profile_id in profiles_by_id
        )
        if not selected_profiles:
            selected_profiles = config.profiles

        transcript_parts: list[str] = []
        round_outputs: dict[int, dict[str, str]] = {}

        # Isolation round
        isolation_outputs, isolation_failures = _run_round(
            request=request,
            profiles=selected_profiles,
            round_number=1,
            prompt_builder=lambda profile: _build_isolated_prompt(request, profile),
            transcript_runner=transcript_runner,
            event_sink=_record_event,
            workspace_root=workspace_root,
            session_id=session_id,
            output_view=output_view,
            config=config,
            resolver=resolver,
        )
        session_failed_agents.extend(isolation_failures)
        round_outputs[1] = isolation_outputs
        transcript_parts.append(_format_round_transcript(1, isolation_outputs))

        # Discussion rounds
        for round_number in range(2, request.rounds + 1):
            current_transcript = "\n\n".join(transcript_parts)
            discussion_outputs, discussion_failures = _run_round(
                request=request,
                profiles=selected_profiles,
                round_number=round_number,
                prompt_builder=lambda profile: _build_discussion_prompt(
                    request, profile, current_transcript
                ),
                transcript_runner=transcript_runner,
                event_sink=_record_event,
                workspace_root=workspace_root,
                session_id=session_id,
                output_view=output_view,
                config=config,
                resolver=resolver,
            )
            session_failed_agents.extend(discussion_failures)
            round_outputs[round_number] = discussion_outputs
            transcript_parts.append(_format_round_transcript(round_number, discussion_outputs))

        # Synthesis
        full_transcript = "\n\n".join(transcript_parts)
        if synthesis_prompt_builder is None:
            synthesis_prompt = _build_synthesis_prompt(request, full_transcript)
        else:
            synthesis_prompt = synthesis_prompt_builder(request, full_transcript)
        synthesizer_workspace = workspace_root / "synthesizer"
        synthesizer_workspace.mkdir(parents=True, exist_ok=True)
        synthesis_output_path = synthesizer_workspace / "synthesis-output.md"

        # Register synthesizer as a single-agent "round" for the output view
        synthesizer_profile = DeliberationAgentProfile(
            profile_id="synthesizer",
            agent=request.synthesizer,
            role="synthesizer",
            behavior_prompt="",
        )
        output_view.register_round_profiles(0, (synthesizer_profile,))

        _emit(
            _record_event,
            session_id,
            0,
            "synthesizer",
            "agent_started",
            "started synthesis",
        )
        output_view.update_status(0, "synthesizer", "running")
        synthesis_sink = _create_streaming_sink(
            synthesis_output_path, output_view, 0, "synthesizer"
        )
        synthesis_display_sink = _create_display_sink(output_view, 0, "synthesizer")
        synthesis_result = transcript_runner.run(
            agent_name=request.synthesizer,
            prompt=synthesis_prompt,
            cwd=synthesizer_workspace,
            event_sink=_record_event,
            output_sink=synthesis_sink,
            display_sink=synthesis_display_sink,
        )
        synthesis_output = synthesis_result.stdout.strip()
        synthesis_status = "finished" if synthesis_result.return_code == 0 else "failed"
        output_view.update_status(0, "synthesizer", synthesis_status)
        _emit(
            _record_event,
            session_id,
            0,
            "synthesizer",
            "agent_finished",
            f"exit={synthesis_result.return_code}",
        )

        parsed = _parse_synthesis(synthesis_output)
        if synthesis_result.return_code != 0:
            if not config.continue_on_agent_error:
                raise RuntimeError(
                    "Deliberation synthesizer failed with exit code "
                    f"{synthesis_result.return_code}."
                )
            session_failed_agents.append(
                DeliberationAgentFailure(
                    profile_id="synthesizer",
                    attempted_agent=request.synthesizer,
                    fallback_agent=None,
                    reason=f"exit={synthesis_result.return_code}",
                )
            )
            _emit(
                _record_event,
                session_id,
                0,
                "system",
                "synthesis_failed",
                f"exit={synthesis_result.return_code}",
            )

        finished_at = _now_iso()
        _emit(
            _record_event,
            session_id,
            0,
            "system",
            "session_finished",
            f"session={session_id}",
        )

        result = DeliberationResult(
            session_id=session_id,
            prompt=request.prompt,
            recommendation=parsed["recommendation"],
            consensus=parsed["consensus"],
            disagreements=parsed["disagreements"],
            risks=parsed["risks"],
            next_actions=parsed["next_actions"],
            events=tuple(emitted_events),
            agent_outputs={
                f"round_{round_number}": dict(outputs)
                for round_number, outputs in round_outputs.items()
            },
            output_dir=str(output_dir),
            started_at=started_at,
            finished_at=finished_at,
            failed_agents=tuple(session_failed_agents),
        )

        return result
    finally:
        output_view.close()


def create_default_session_id() -> str:
    """Create a timestamp-based deliberation session ID."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _default_session_id() -> str:
    return create_default_session_id()
