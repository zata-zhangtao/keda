"""File writers for agent deliberation outputs."""

from __future__ import annotations

import json
from pathlib import Path

from backend.core.shared.models.agent_deliberation import (
    DeliberationAgentFailure,
    DeliberationResult,
    DeliberationSession,
)


def _serialize_failure(failure: DeliberationAgentFailure) -> dict:
    return {
        "profile_id": failure.profile_id,
        "attempted_agent": failure.attempted_agent,
        "fallback_agent": failure.fallback_agent,
        "reason": failure.reason,
    }


def write_deliberation_outputs(
    result: DeliberationResult,
    session: DeliberationSession,
    output_dir: Path,
) -> None:
    """Write transcript, result, and session files for a deliberation run.

    Args:
        result: Completed deliberation result.
        session: Deliberation session metadata.
        output_dir: Destination directory.

    Returns:
        None.
    """
    transcript_path = output_dir / "transcript.md"
    transcript_lines = [
        "# Deliberation Transcript",
        "",
        "## Session",
        "",
        f"- Session ID: `{session.session_id}`",
        f"- Prompt: {session.prompt}",
        f"- Agents: {', '.join(p.profile_id for p in session.profiles)}",
        "",
    ]
    for round_key, outputs in result.agent_outputs.items():
        transcript_lines.append(f"## {round_key}")
        transcript_lines.append("")
        for profile_id, output in outputs.items():
            transcript_lines.append(f"### {profile_id}")
            transcript_lines.append("")
            transcript_lines.append(output)
            transcript_lines.append("")
    transcript_path.write_text("\n".join(transcript_lines), encoding="utf-8")

    result_path = output_dir / "result.md"
    result_lines = [
        "# Deliberation Result",
        "",
        "## Recommendation",
        "",
        result.recommendation,
        "",
        "## Consensus",
        "",
        result.consensus,
        "",
        "## Disagreements",
        "",
        result.disagreements,
        "",
        "## Risks",
        "",
        result.risks,
        "",
        "## Next Actions",
        "",
        result.next_actions,
        "",
    ]
    result_path.write_text("\n".join(result_lines), encoding="utf-8")

    session_path = output_dir / "session.json"
    session_data = {
        "session_id": session.session_id,
        "prompt": session.prompt,
        "profiles": [
            {
                "profile_id": profile.profile_id,
                "agent": profile.agent,
                "role": profile.role,
            }
            for profile in session.profiles
        ],
        "rounds": session.rounds,
        "synthesizer": session.synthesizer,
        "output_dir": str(session.output_dir),
        "started_at": session.started_at,
        "finished_at": session.finished_at,
        "failed_agents": [_serialize_failure(f) for f in result.failed_agents],
    }
    session_path.write_text(
        json.dumps(session_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
