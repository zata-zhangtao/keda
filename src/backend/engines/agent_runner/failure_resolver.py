"""Agent failure resolver for deliberation sessions.

Provides interactive and automatic model fallback when a single participant
or synthesizer fails during a deliberation round.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

from backend.core.shared.models.agent_deliberation import (
    DeliberationAgentProfile,
    DeliberationConfig,
)


@dataclass(frozen=True)
class FallbackSelection:
    """Result of a fallback model selection."""

    profile: "DeliberationAgentProfile"
    auto_selected: bool


class AgentFailureResolver:
    """Resolve a failed agent by selecting a fallback model.

    The resolver keeps the original profile_id, role, and behavior_prompt
    and only swaps the underlying agent command. In TTY mode it prompts the
    user; in non-TTY mode it automatically picks the next available model.
    """

    def __init__(
        self,
        *,
        is_tty: bool | None = None,
        input_reader: Callable[[str], str] | None = None,
        printer: Callable[[str], None] | None = None,
    ) -> None:
        self._is_tty = (
            is_tty if is_tty is not None else (sys.stdin.isatty() and sys.stdout.isatty())
        )
        self._input_reader = input_reader or input
        self._printer = printer or print

    def _available_agents(
        self,
        failed_profile: "DeliberationAgentProfile",
        config: "DeliberationConfig",
    ) -> tuple[str, ...]:
        seen: list[str] = []
        for profile in config.profiles:
            if profile.agent == failed_profile.agent:
                continue
            if profile.agent not in seen:
                seen.append(profile.agent)
        return tuple(seen)

    def _prompt_for_fallback(
        self,
        failed_profile: "DeliberationAgentProfile",
        available_agents: tuple[str, ...],
        timeout_seconds: int,
    ) -> str | None:
        if not available_agents:
            return None

        lines = [
            f"\n[round failure] agent={failed_profile.profile_id} "
            f"provider={failed_profile.agent}",
            "",
            "The agent failed. Available fallback models:",
        ]
        for index, agent_name in enumerate(available_agents, start=1):
            lines.append(f"  {index}) {agent_name}")
        lines.append("")
        prompt_text = (
            f"Choose a fallback model (auto-selecting {available_agents[0]} "
            f"in {timeout_seconds}s): "
        )
        self._printer("\n".join(lines))
        self._printer(prompt_text, end="")

        result_container: list[str | None] = [None]
        lock = threading.Lock()

        def _read_input() -> None:
            try:
                value = self._input_reader("")
                with lock:
                    result_container[0] = value.strip()
            except EOFError:
                pass
            except Exception:  # noqa: BLE001
                pass

        reader_thread = threading.Thread(target=_read_input, daemon=True)
        reader_thread.start()
        reader_thread.join(timeout=timeout_seconds)

        with lock:
            choice = result_container[0]
        if choice is None:
            self._printer(
                f"\nNo selection within {timeout_seconds}s; automatically "
                f"switching {failed_profile.profile_id} to {available_agents[0]}."
            )
            return available_agents[0]

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(available_agents):
                return available_agents[idx]

        matched = [a for a in available_agents if a.lower() == choice.lower()]
        if matched:
            return matched[0]

        self._printer(
            f"\nInvalid selection {choice!r}; automatically switching "
            f"{failed_profile.profile_id} to {available_agents[0]}."
        )
        return available_agents[0]

    def resolve(
        self,
        failed_profile: "DeliberationAgentProfile",
        reason: str,
        config: "DeliberationConfig | None" = None,
    ) -> "DeliberationAgentProfile | None":
        """Return a fallback profile or None if no fallback is available.

        Args:
            failed_profile: The profile that failed.
            config: Deliberation configuration used to derive available models.
            reason: Human-readable failure reason for logging/prompts.

        Returns:
            A new profile with a different agent, or None when no other agent
            is configured.
        """
        _ = reason
        available_agents = self._available_agents(failed_profile, config or DeliberationConfig())
        if not available_agents:
            return None

        if self._is_tty:
            selected_agent = self._prompt_for_fallback(
                failed_profile, available_agents, config.agent_failure_timeout_seconds
            )
        else:
            selected_agent = available_agents[0]

        if selected_agent is None:
            return None

        return DeliberationAgentProfile(
            profile_id=failed_profile.profile_id,
            agent=selected_agent,
            role=failed_profile.role,
            behavior_prompt=failed_profile.behavior_prompt,
        )
