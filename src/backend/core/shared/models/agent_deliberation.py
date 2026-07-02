"""Agent deliberation domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DeliberationAgentProfile:
    """Participant profile for a deliberation session."""

    profile_id: str
    agent: str
    role: str
    behavior_prompt: str


@dataclass(frozen=True)
class DeliberationAgentFailure:
    """Record of a single agent failure during deliberation."""

    profile_id: str
    attempted_agent: str
    fallback_agent: str | None
    reason: str


@dataclass(frozen=True)
class DeliberationConfig:
    """Deliberation defaults and profiles."""

    default_rounds: int = 2
    default_synthesizer: str = "claude"
    default_output_dir: str = "logs/agent-runner/deliberations"
    continue_on_agent_error: bool = True
    agent_failure_timeout_seconds: int = 300
    # After this many consecutive AI-asked rounds without a fresh user reply,
    # the Phase 0 question-list comment appends a soft hint suggesting the
    # operator swap labels to converge. Mirrors the pydantic settings layer.
    stale_rounds_before_hint: int = 3
    profiles: tuple[DeliberationAgentProfile, ...] = field(
        default_factory=lambda: (
            DeliberationAgentProfile(
                profile_id="architect",
                agent="claude",
                role="architect",
                behavior_prompt=(
                    "You are an experienced software architect. "
                    "Analyze the requirement from a system design perspective. "
                    "Focus on modularity, scalability, and maintainability."
                ),
            ),
            DeliberationAgentProfile(
                profile_id="skeptic",
                agent="kimi",
                role="skeptic",
                behavior_prompt=(
                    "You are a skeptical reviewer. "
                    "Challenge assumptions, identify risks, and point out edge cases. "
                    "Ask hard questions that others might miss."
                ),
            ),
            DeliberationAgentProfile(
                profile_id="implementer",
                agent="codex",
                role="implementer",
                behavior_prompt=(
                    "You are a pragmatic implementer. "
                    "Focus on feasibility, concrete steps, and implementation details. "
                    "Highlight what can be built and what resources are needed."
                ),
            ),
        )
    )


@dataclass(frozen=True)
class DeliberationRequest:
    """User request to start a deliberation session."""

    prompt: str
    agents: tuple[str, ...] = ("architect", "skeptic", "implementer")
    rounds: int = 2
    synthesizer: str = "claude"
    output_dir: str = "logs/agent-runner/deliberations"
    session_id: str | None = None


@dataclass(frozen=True)
class DeliberationEvent:
    """Single event in a deliberation session."""

    session_id: str
    round: int
    agent: str
    event_type: str
    message: str
    timestamp: str


@dataclass(frozen=True)
class DeliberationResult:
    """Final output of a deliberation session."""

    session_id: str
    prompt: str
    recommendation: str
    consensus: str
    disagreements: str
    risks: str
    next_actions: str
    events: tuple[DeliberationEvent, ...]
    agent_outputs: dict[str, dict[str, str]]
    output_dir: str
    started_at: str
    finished_at: str
    failed_agents: tuple[DeliberationAgentFailure, ...] = ()


@dataclass(frozen=True)
class DeliberationSession:
    """Session metadata assembled during deliberation."""

    session_id: str
    prompt: str
    profiles: tuple[DeliberationAgentProfile, ...]
    rounds: int
    synthesizer: str
    output_dir: Path
    started_at: str
    finished_at: str | None = None
