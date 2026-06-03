"""Abstract output view contract for terminal live output.

This module defines the abstract interface for displaying multi-agent
deliberation output in real-time. The core layer uses this interface
to emit profile-aware output chunks, while the engines layer provides
concrete implementations (Rich live columns for TTY, plain text for
non-TTY/CI environments).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.shared.models.agent_deliberation import DeliberationAgentProfile


class IAgentOutputView(ABC):
    """Abstract contract for displaying live agent output.

    Implementations handle real-time display of agent output during
    deliberation sessions. The interface allows core orchestration to
    remain unaware of terminal UI details while still providing
    structured output events.
    """

    @abstractmethod
    def register_round_profiles(
        self,
        round_number: int,
        profiles: tuple["DeliberationAgentProfile", ...],
    ) -> None:
        """Register the profiles that will run concurrently in a round.

        Args:
            round_number: The round number (1-indexed for participants,
                0 for synthesizer).
            profiles: Tuple of profiles that will run concurrently.
        """
        ...

    @abstractmethod
    def append_output(
        self,
        round_number: int,
        profile_id: str,
        chunk: str,
    ) -> None:
        """Append a chunk of rendered output for a specific agent.

        Args:
            round_number: Current round number.
            profile_id: The profile ID (e.g., "architect", "synthesizer").
            chunk: A readable text chunk to display.
        """
        ...

    @abstractmethod
    def update_status(
        self,
        round_number: int,
        profile_id: str,
        status: str,
    ) -> None:
        """Update the status of a specific agent.

        Args:
            round_number: Current round number.
            profile_id: The profile ID.
            status: Status string (e.g., "running", "finished", "failed").
        """
        ...

    @abstractmethod
    def log(self, message: str) -> None:
        """Emit a standalone log line that is not tied to an agent panel.

        Used for session-level events (e.g., started/finished). When a live
        display is active, the message must be rendered without corrupting it
        (e.g., printed above the live region rather than written to stdout
        directly).

        Args:
            message: The line to display.
        """
        ...

    @abstractmethod
    def close(self) -> None:
        """Finalize and clean up the output view.

        Called after all agents complete. Implementations should flush
        any pending output and restore terminal state if needed.
        """
        ...


class NoOpOutputView(IAgentOutputView):
    """A no-op implementation that discards all output.

    Useful for testing or when output display is explicitly disabled.
    """

    def register_round_profiles(
        self,
        round_number: int,
        profiles: tuple["DeliberationAgentProfile", ...],
    ) -> None:
        """Discard profile registration."""
        _ = round_number, profiles

    def append_output(
        self,
        round_number: int,
        profile_id: str,
        chunk: str,
    ) -> None:
        """Discard output chunk."""
        _ = round_number, profile_id, chunk

    def update_status(
        self,
        round_number: int,
        profile_id: str,
        status: str,
    ) -> None:
        """Discard status update."""
        _ = round_number, profile_id, status

    def log(self, message: str) -> None:
        """Discard log message."""
        _ = message

    def close(self) -> None:
        """No-op close."""
