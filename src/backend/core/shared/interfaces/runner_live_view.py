"""Abstract live-view contract for parallel Issue processing.

When ``iar daemon`` processes several Issues concurrently, each Issue's agent
output must be shown without interleaving on a single stdout. This port lets the
core orchestration emit Issue-attributed output chunks while staying unaware of
terminal UI details; the engines layer provides concrete implementations (Rich
live columns for an interactive TTY, line-prefixed plain text otherwise).

This mirrors the deliberation ``IAgentOutputView`` pattern but is keyed by Issue
number instead of deliberation profile/round, so the runner path does not depend
on the deliberation domain model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class IRunnerLiveView(ABC):
    """Abstract contract for displaying live, per-Issue agent output."""

    @abstractmethod
    def register_issue(self, issue_number: int, agent: str) -> None:
        """Register an Issue that is about to run in the current pass.

        Args:
            issue_number: GitHub Issue number used as the panel key.
            agent: Agent name shown in the panel title (e.g. ``"claude"``).
        """
        ...

    @abstractmethod
    def append(self, issue_number: int, chunk: str) -> None:
        """Append a rendered output chunk to an Issue's panel.

        Args:
            issue_number: The Issue whose panel receives the chunk.
            chunk: Readable text to display.
        """
        ...

    @abstractmethod
    def update_status(self, issue_number: int, status: str) -> None:
        """Update an Issue's display status.

        Args:
            issue_number: The Issue whose status changed.
            status: Status string such as ``"running"``, ``"completed"``,
                ``"failed"`` or ``"blocked"``.
        """
        ...

    @abstractmethod
    def log(self, message: str) -> None:
        """Emit a pass-level line not tied to any Issue panel.

        When a live display is active, the message must render without
        corrupting it (e.g. printed above the live region).

        Args:
            message: The line to display.
        """
        ...

    @abstractmethod
    def close(self) -> None:
        """Finalize and clean up the view, leaving the final frame visible."""
        ...


class NoOpRunnerLiveView(IRunnerLiveView):
    """A live view that discards everything.

    Used for the sequential path and for tests, so callers never need to branch
    on whether a live view is present.
    """

    def register_issue(self, issue_number: int, agent: str) -> None:
        """Discard Issue registration."""
        _ = issue_number, agent

    def append(self, issue_number: int, chunk: str) -> None:
        """Discard an output chunk."""
        _ = issue_number, chunk

    def update_status(self, issue_number: int, status: str) -> None:
        """Discard a status update."""
        _ = issue_number, status

    def log(self, message: str) -> None:
        """Discard a pass-level line."""
        _ = message

    def close(self) -> None:
        """No-op close."""
