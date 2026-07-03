"""Tests for AgentFailureResolver fallback selection."""

from __future__ import annotations

import re
import time

from backend.core.shared.models.agent_deliberation import (
    DeliberationAgentProfile,
    DeliberationConfig,
)
from backend.engines.agent_runner.failure_resolver import AgentFailureResolver


def _make_profile(agent: str = "kimi", profile_id: str = "skeptic") -> DeliberationAgentProfile:
    """Build a profile for failure resolver tests."""
    return DeliberationAgentProfile(
        profile_id=profile_id,
        agent=agent,
        role="skeptic",
        behavior_prompt="be skeptical",
    )


def _make_config(
    profiles: tuple[DeliberationAgentProfile, ...] | None = None,
    timeout_seconds: int = 300,
) -> DeliberationConfig:
    """Build a deliberation config with multiple agent profiles."""
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
            DeliberationAgentProfile(
                profile_id="implementer",
                agent="codex",
                role="implementer",
                behavior_prompt="be an implementer",
            ),
        )
    return DeliberationConfig(profiles=profiles, agent_failure_timeout_seconds=timeout_seconds)


def test_resolve_non_tty_selects_first_available_fallback() -> None:
    """Non-TTY mode should automatically pick the first available fallback agent."""
    resolver = AgentFailureResolver(is_tty=False)
    failed_profile = _make_profile(agent="kimi")

    result = resolver.resolve(failed_profile, reason="exit=1", config=_make_config())

    assert result is not None
    assert result.profile_id == "skeptic"
    assert result.agent == "claude"
    assert result.role == "skeptic"
    assert result.behavior_prompt == "be skeptical"


def test_resolve_tty_prompt_selects_choice_by_number() -> None:
    """TTY mode should use the agent selected by numeric input."""
    printed: list[str] = []
    resolver = AgentFailureResolver(
        is_tty=True,
        input_reader=lambda _: "2",
        printer=lambda text, **kwargs: printed.append(text),
    )
    failed_profile = _make_profile(agent="kimi")

    result = resolver.resolve(failed_profile, reason="exit=1", config=_make_config())

    assert result is not None
    assert result.agent == "codex"
    assert any("Available fallback models" in line for line in printed)


def test_resolve_tty_prompt_selects_choice_by_name() -> None:
    """TTY mode should match agent names case-insensitively."""
    resolver = AgentFailureResolver(
        is_tty=True,
        input_reader=lambda _: "CLAUDE",
        printer=lambda text, **kwargs: None,
    )
    failed_profile = _make_profile(agent="kimi")

    result = resolver.resolve(failed_profile, reason="exit=1", config=_make_config())

    assert result is not None
    assert result.agent == "claude"


def test_resolve_tty_prompt_timeout_selects_first_fallback() -> None:
    """TTY mode should auto-select the first fallback when input times out."""
    printed: list[str] = []

    def slow_input(_prompt: str) -> str:
        # Sleep longer than the configured timeout so the resolver times out
        # before the reader thread can record any input.
        time.sleep(1.5)
        return ""

    resolver = AgentFailureResolver(
        is_tty=True,
        input_reader=slow_input,
        printer=lambda text, **kwargs: printed.append(text),
    )
    config = _make_config(timeout_seconds=1)
    failed_profile = _make_profile(agent="kimi")

    result = resolver.resolve(failed_profile, reason="exit=1", config=config)

    assert result is not None
    assert result.agent == "claude"
    assert any("No selection within" in line for line in printed)


def test_resolve_tty_invalid_choice_falls_back_to_first() -> None:
    """TTY mode should fall back to the first agent on invalid input."""
    printed: list[str] = []
    resolver = AgentFailureResolver(
        is_tty=True,
        input_reader=lambda _: "not-an-agent",
        printer=lambda text, **kwargs: printed.append(text),
    )
    failed_profile = _make_profile(agent="kimi")

    result = resolver.resolve(failed_profile, reason="exit=1", config=_make_config())

    assert result is not None
    assert result.agent == "claude"
    assert any("Invalid selection" in line for line in printed)


def test_resolve_returns_none_when_no_fallback_available() -> None:
    """When all configured profiles use the same agent, no fallback exists."""
    resolver = AgentFailureResolver(is_tty=False)
    failed_profile = _make_profile(agent="kimi")
    config = _make_config(
        profiles=(
            DeliberationAgentProfile(
                profile_id="skeptic",
                agent="kimi",
                role="skeptic",
                behavior_prompt="be a skeptic",
            ),
        )
    )

    result = resolver.resolve(failed_profile, reason="exit=1", config=config)

    assert result is None


def test_resolve_uses_default_config_when_none_provided() -> None:
    """Resolver should tolerate a missing config by using defaults."""
    resolver = AgentFailureResolver(is_tty=False)
    failed_profile = _make_profile(agent="kimi")

    result = resolver.resolve(failed_profile, reason="exit=1", config=None)

    assert result is not None
    assert result.agent == "claude"


def test_resolve_excludes_duplicate_agents_in_fallback_list() -> None:
    """Multiple profiles sharing the same agent should only appear once."""
    printed: list[str] = []
    resolver = AgentFailureResolver(
        is_tty=True,
        input_reader=lambda _: "1",
        printer=lambda text, **kwargs: printed.append(text),
    )
    failed_profile = _make_profile(agent="kimi")
    config = _make_config(
        profiles=(
            DeliberationAgentProfile(
                profile_id="architect",
                agent="claude",
                role="architect",
                behavior_prompt="be an architect",
            ),
            DeliberationAgentProfile(
                profile_id="reviewer",
                agent="claude",
                role="reviewer",
                behavior_prompt="be a reviewer",
            ),
            DeliberationAgentProfile(
                profile_id="skeptic",
                agent="kimi",
                role="skeptic",
                behavior_prompt="be a skeptic",
            ),
        )
    )

    result = resolver.resolve(failed_profile, reason="exit=1", config=config)

    assert result is not None
    assert result.agent == "claude"
    fallback_listing = "\n".join(printed)
    # Only one numbered list entry for claude; the prompt line also mentions it.
    assert len(re.findall(r"^\s*\d+\) claude$", fallback_listing, re.M)) == 1
