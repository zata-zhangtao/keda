"""Terminal live view adapter for multi-agent deliberation output.

Provides concrete implementations of ``IAgentOutputView`` for displaying
real-time agent output during deliberation sessions. Two implementations
are available:

- ``RichLiveOutputView``: Interactive TTY display using Rich ``Live`` and
  ``Columns`` for dynamic per-agent columns.
- ``PlainOutputView``: Line-prefixed plain text output for non-TTY, CI,
  redirected, or explicit plain mode.

Both implementations reside in the engines layer, keeping terminal UI
dependencies (``rich``) out of the core business layer.
"""

from __future__ import annotations

import io
import os
import sys
import threading
from typing import TYPE_CHECKING

from backend.core.shared.interfaces.agent_output_view import IAgentOutputView

if TYPE_CHECKING:
    from backend.core.shared.models.agent_deliberation import DeliberationAgentProfile


def _is_interactive_tty() -> bool:
    """Return True when stdout is a TTY and we are not in a CI environment."""
    if not sys.stdout.isatty():
        return False
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return False
    return True


def create_output_view(*, plain: bool = False) -> IAgentOutputView:
    """Create the appropriate output view based on environment.

    Args:
        plain: If True, force plain text output regardless of TTY state.

    Returns:
        An ``IAgentOutputView`` instance suitable for the current
        environment.
    """
    if plain or not _is_interactive_tty():
        return PlainOutputView()
    try:
        return RichLiveOutputView()
    except Exception:  # noqa: BLE001
        return PlainOutputView()


class _AgentPanelState:
    """Track per-agent display state for a panel."""

    __slots__ = ("profile_id", "provider", "status", "lines", "max_lines")

    def __init__(
        self,
        profile_id: str,
        provider: str,
        max_lines: int = 50,
    ) -> None:
        self.profile_id = profile_id
        self.provider = provider
        self.status = "pending"
        self.lines: list[str] = []
        self.max_lines = max_lines

    def append(self, chunk: str) -> None:
        """Append a text chunk, keeping only the most recent lines."""
        new_lines = chunk.splitlines()
        self.lines.extend(new_lines)
        if len(self.lines) > self.max_lines:
            self.lines = self.lines[-self.max_lines :]


class RichLiveOutputView(IAgentOutputView):
    """Interactive TTY display using Rich Live and Columns.

    Displays per-agent columns that update in real-time as output
    chunks arrive. The number of columns equals the number of
    concurrently running profiles.

    Thread-safety: all Rich operations are serialized through a lock
    to prevent concurrent redraws from multiple worker threads.
    """

    def __init__(self) -> None:
        from rich.columns import Columns
        from rich.live import Live
        from rich.panel import Panel
        from rich.text import Text

        self._lock = threading.Lock()
        self._panels: dict[str, _AgentPanelState] = {}
        self._current_round = 0
        self._live: Live | None = None

        # Import rich types for later use
        self._RichLive = Live
        self._RichColumns = Columns
        self._RichPanel = Panel
        self._RichText = Text

    def register_round_profiles(
        self,
        round_number: int,
        profiles: tuple["DeliberationAgentProfile", ...],
    ) -> None:
        """Register profiles and start live display for this round."""
        with self._lock:
            self._current_round = round_number
            self._panels.clear()
            for profile in profiles:
                self._panels[profile.profile_id] = _AgentPanelState(
                    profile_id=profile.profile_id,
                    provider=profile.agent,
                )
            self._start_live()

    def _start_live(self) -> None:
        """Start or restart the Rich Live display."""
        if self._live is not None:
            self._live.stop()
        renderable = self._build_renderable()
        self._live = self._RichLive(
            renderable,
            refresh_per_second=4,
            vertical_overflow="visible",
        )
        self._live.start()

    def _build_renderable(self) -> object:
        """Build the Rich renderable for the current state."""
        panels = []
        for profile_id, state in self._panels.items():
            title = (
                f"round={self._current_round} agent={profile_id} "
                f"provider={state.provider} {state.status}"
            )
            subtitle = f"workspaces/{profile_id}/"
            content = "\n".join(state.lines) if state.lines else "..."
            panel = self._RichPanel(
                self._RichText(content),
                title=title,
                subtitle=subtitle,
                border_style="blue" if state.status == "running" else "green",
            )
            panels.append(panel)
        if not panels:
            return self._RichText("")
        return self._RichColumns(panels, equal=True, expand=True)

    def append_output(
        self,
        round_number: int,
        profile_id: str,
        chunk: str,
    ) -> None:
        """Append output to the corresponding agent panel."""
        _ = round_number
        with self._lock:
            panel_state = self._panels.get(profile_id)
            if panel_state is not None:
                panel_state.append(chunk)
                if self._live is not None:
                    self._live.update(self._build_renderable())

    def update_status(
        self,
        round_number: int,
        profile_id: str,
        status: str,
    ) -> None:
        """Update agent status in the display."""
        _ = round_number
        with self._lock:
            panel_state = self._panels.get(profile_id)
            if panel_state is not None:
                panel_state.status = status
                if self._live is not None:
                    self._live.update(self._build_renderable())

    def close(self) -> None:
        """Stop the live display."""
        with self._lock:
            if self._live is not None:
                self._live.stop()
                self._live = None


class PlainOutputView(IAgentOutputView):
    """Line-prefixed plain text output for non-TTY, CI, or plain mode.

    Each visible output block is prefixed with ``[round=<n> agent=<id> status=<status>]``
    to provide agent attribution without relying on terminal capabilities.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agent_status: dict[str, str] = {}
        self._current_round = 0

    def register_round_profiles(
        self,
        round_number: int,
        profiles: tuple["DeliberationAgentProfile", ...],
    ) -> None:
        """Register profiles for the round."""
        with self._lock:
            self._current_round = round_number
            for profile in profiles:
                self._agent_status[profile.profile_id] = "pending"

    def append_output(
        self,
        round_number: int,
        profile_id: str,
        chunk: str,
    ) -> None:
        """Print output with round/agent prefix."""
        with self._lock:
            status = self._agent_status.get(profile_id, "running")
        prefix = f"[round={round_number} agent={profile_id} status={status}]"
        for line in chunk.splitlines():
            if line.strip():
                print(f"{prefix} {line}", flush=True)

    def update_status(
        self,
        round_number: int,
        profile_id: str,
        status: str,
    ) -> None:
        """Update agent status and print status change."""
        _ = round_number
        with self._lock:
            self._agent_status[profile_id] = status
        prefix = f"[round={round_number} agent={profile_id} status={status}]"
        print(f"{prefix}", flush=True)

    def close(self) -> None:
        """No-op close for plain output."""
