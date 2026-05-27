"""Multi-agent deliberation session orchestrator."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from backend.core.shared.interfaces.agent_runner import (
    IAgentTranscriptRunner,
)
from backend.core.shared.models.agent_deliberation import (
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
    import threading

    lock = threading.Lock()
    initialized = False

    def _sink(chunk: str) -> None:
        nonlocal initialized
        with lock:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if not initialized:
                output_path.write_text("", encoding="utf-8")
                initialized = True
            with open(output_path, "a", encoding="utf-8") as f:
                f.write(chunk + "\n")
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
) -> str:
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
    if result.return_code != 0:
        raise RuntimeError(
            f"Deliberation agent '{agent_label}' failed with exit code "
            f"{result.return_code}."
        )
    return output_text


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
) -> dict[str, str]:
    outputs: dict[str, str] = {}

    # Register profiles with the output view for this round
    output_view.register_round_profiles(round_number, profiles)

    def _run_profile(profile: DeliberationAgentProfile) -> tuple[str, str]:
        prompt = prompt_builder(profile)
        profile_workspace = workspace_root / profile.profile_id
        profile_workspace.mkdir(parents=True, exist_ok=True)
        output_path = profile_workspace / f"round-{round_number}-output.md"
        streaming_sink = _create_streaming_sink(
            output_path, output_view, round_number, profile.profile_id
        )
        output = _run_single_agent(
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
        )
        return profile.profile_id, output

    with ThreadPoolExecutor(max_workers=len(profiles)) as executor:
        futures = [executor.submit(_run_profile, profile) for profile in profiles]
        for future in futures:
            profile_id, output = future.result()
            outputs[profile_id] = output

    return outputs


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
        isolation_outputs = _run_round(
            request=request,
            profiles=selected_profiles,
            round_number=1,
            prompt_builder=lambda profile: _build_isolated_prompt(request, profile),
            transcript_runner=transcript_runner,
            event_sink=_record_event,
            workspace_root=workspace_root,
            session_id=session_id,
            output_view=output_view,
        )
        round_outputs[1] = isolation_outputs
        transcript_parts.append(_format_round_transcript(1, isolation_outputs))

        # Discussion rounds
        for round_number in range(2, request.rounds + 1):
            current_transcript = "\n\n".join(transcript_parts)
            discussion_outputs = _run_round(
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
            )
            round_outputs[round_number] = discussion_outputs
            transcript_parts.append(
                _format_round_transcript(round_number, discussion_outputs)
            )

        # Synthesis
        full_transcript = "\n\n".join(transcript_parts)
        synthesis_prompt = _build_synthesis_prompt(request, full_transcript)
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
        synthesis_result = transcript_runner.run(
            agent_name=request.synthesizer,
            prompt=synthesis_prompt,
            cwd=synthesizer_workspace,
            event_sink=_record_event,
            output_sink=synthesis_sink,
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
        if synthesis_result.return_code != 0:
            raise RuntimeError(
                "Deliberation synthesizer failed with exit code "
                f"{synthesis_result.return_code}."
            )

        parsed = _parse_synthesis(synthesis_output)

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
        )

        return result
    finally:
        output_view.close()


def create_default_session_id() -> str:
    """Create a timestamp-based deliberation session ID."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _default_session_id() -> str:
    return create_default_session_id()
