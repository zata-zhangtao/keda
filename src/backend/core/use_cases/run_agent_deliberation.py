"""Multi-agent deliberation session orchestrator."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IAgentTranscriptRunner,
    IProcessRunner,
)
from backend.core.shared.models.agent_deliberation import (
    DeliberationAgentProfile,
    DeliberationConfig,
    DeliberationEvent,
    DeliberationRequest,
    DeliberationResult,
)

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


def _run_single_agent(
    agent_name: str,
    prompt: str,
    cwd: Path,
    transcript_runner: IAgentTranscriptRunner,
    event_sink: Callable[[DeliberationEvent], None],
    session_id: str,
    round_number: int,
    agent_label: str,
) -> str:
    _emit(
        event_sink,
        session_id,
        round_number,
        agent_label,
        "agent_started",
        "started analysis",
    )
    result = transcript_runner.run(
        agent_name=agent_name,
        prompt=prompt,
        cwd=cwd,
        event_sink=event_sink,
    )
    output_text = result.stdout.strip()
    _emit(
        event_sink,
        session_id,
        round_number,
        agent_label,
        "agent_finished",
        f"exit={result.return_code}",
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
) -> dict[str, str]:
    outputs: dict[str, str] = {}

    def _run_profile(profile: DeliberationAgentProfile) -> tuple[str, str]:
        prompt = prompt_builder(profile)
        profile_workspace = workspace_root / profile.profile_id
        profile_workspace.mkdir(parents=True, exist_ok=True)
        output = _run_single_agent(
            agent_name=profile.agent,
            prompt=prompt,
            cwd=profile_workspace,
            transcript_runner=transcript_runner,
            event_sink=event_sink,
            session_id=session_id,
            round_number=round_number,
            agent_label=profile.profile_id,
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
    process_runner: IProcessRunner,
) -> DeliberationResult:
    """Run a multi-agent deliberation session.

    Args:
        request: User request.
        config: Deliberation configuration with profiles.
        transcript_runner: Runner that executes agents and emits events.
        event_sink: Callback for structured events.
        target_repo_path: Path to the target repository for read-only safety checks.
        process_runner: Function to run git status checks.

    Returns:
        DeliberationResult with the final report.
    """
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

    # Verify target repo is clean before starting.
    _verify_repo_clean(target_repo_path, process_runner, _record_event, session_id)

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
    )
    round_outputs[1] = isolation_outputs
    transcript_parts.append(_format_round_transcript(1, isolation_outputs))

    _verify_repo_clean(target_repo_path, process_runner, _record_event, session_id)

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
        )
        round_outputs[round_number] = discussion_outputs
        transcript_parts.append(
            _format_round_transcript(round_number, discussion_outputs)
        )
        _verify_repo_clean(target_repo_path, process_runner, _record_event, session_id)

    # Synthesis
    full_transcript = "\n\n".join(transcript_parts)
    synthesis_prompt = _build_synthesis_prompt(request, full_transcript)
    synthesizer_workspace = workspace_root / "synthesizer"
    synthesizer_workspace.mkdir(parents=True, exist_ok=True)

    _emit(
        _record_event,
        session_id,
        0,
        "synthesizer",
        "agent_started",
        "started synthesis",
    )
    synthesis_result = transcript_runner.run(
        agent_name=request.synthesizer,
        prompt=synthesis_prompt,
        cwd=synthesizer_workspace,
        event_sink=_record_event,
    )
    _emit(
        _record_event,
        session_id,
        0,
        "synthesizer",
        "agent_finished",
        f"exit={synthesis_result.return_code}",
    )

    parsed = _parse_synthesis(synthesis_result.stdout)

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
            f"round_{round_number}": list(outputs.values())
            for round_number, outputs in round_outputs.items()
        },
        output_dir=str(output_dir),
        started_at=started_at,
        finished_at=finished_at,
    )

    return result


def create_default_session_id() -> str:
    """Create a timestamp-based deliberation session ID."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _default_session_id() -> str:
    return create_default_session_id()


def _verify_repo_clean(
    repo_path: Path,
    process_runner: IProcessRunner,
    event_sink: Callable[[DeliberationEvent], None],
    session_id: str,
) -> None:
    result = process_runner.run(
        ["git", "status", "--porcelain"], cwd=repo_path, check=False
    )
    if result.return_code != 0:
        _emit(
            event_sink,
            session_id,
            0,
            "system",
            "repo_status_failed",
            result.stderr.strip() or "Unable to verify target repository status.",
        )
        raise RuntimeError("Unable to verify target repository status.")
    if result.stdout.strip():
        _emit(
            event_sink,
            session_id,
            0,
            "system",
            "repo_changed",
            "Target repository status changed during deliberation.",
        )
        raise RuntimeError("Target repository status changed during deliberation.")
