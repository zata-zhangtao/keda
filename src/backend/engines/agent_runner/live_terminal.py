"""Terminal live view adapter for multi-agent deliberation output.

Provides concrete implementations of ``IAgentOutputView`` for displaying
real-time agent output during deliberation sessions. Two implementations
are available:

- ``RichLiveOutputView``: Interactive TTY display using a single persistent
  Rich ``Live`` with one ``Columns`` panel per concurrently running agent.
- ``PlainOutputView``: Line-prefixed plain text output for non-TTY, CI,
  redirected, or explicit plain mode.

Both implementations reside in the engines layer, keeping terminal UI
dependencies (``rich``) out of the core business layer.

All terminal output during a live session (agent chunks, status changes and
session-level log lines) flows through the single ``Console`` owned by the
view, so nothing writes to ``stdout`` behind Rich's back and corrupts the
live region.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from backend.core.shared.interfaces.agent_output_view import IAgentOutputView

if TYPE_CHECKING:
    from backend.core.shared.models.agent_deliberation import DeliberationAgentProfile

# Lines kept in memory per agent panel. The workspace file remains the source
# of truth for full history; the panel only needs enough to fill the screen.
_PANEL_BUFFER_LINES = 200

# Minimum readable text width per side-by-side column. Below this, columns are
# too narrow to read, so the view falls back to full-width vertical stacking.
_MIN_COLUMN_WIDTH = 40


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
    except Exception:  # noqa: BLE001 - any Rich init failure falls back to plain.
        return PlainOutputView()


class _AgentPanelState:
    """Track per-agent display state for a panel."""

    __slots__ = ("profile_id", "provider", "status", "lines")

    def __init__(self, profile_id: str, provider: str) -> None:
        self.profile_id = profile_id
        self.provider = provider
        self.status = "pending"
        self.lines: list[str] = []

    def append(self, chunk: str) -> None:
        """Append a text chunk, keeping only the most recent lines."""
        if not chunk:
            return
        if not self.lines:
            self.lines.append("")
        normalized_chunk = chunk.replace("\r\n", "\n").replace("\r", "\n")
        chunk_line_parts = normalized_chunk.split("\n")
        self.lines[-1] += chunk_line_parts[0]
        self.lines.extend(chunk_line_parts[1:])
        if len(self.lines) > _PANEL_BUFFER_LINES:
            self.lines = self.lines[-_PANEL_BUFFER_LINES:]


class RichLiveOutputView(IAgentOutputView):
    """Interactive TTY display using a single persistent Rich Live.

    Displays one column per concurrently running profile. The number of
    columns equals the number of profiles registered for the current round.

    A single ``Console`` and ``Live`` are created lazily and reused for the
    whole session. When a new round starts, the previous round's columns are
    printed once above the live region (so they scroll into terminal history)
    before the live region switches to the new round. This avoids the flicker
    and lost history of stopping and restarting the display each round.

    Thread-safety: all Rich operations are serialized through a lock to
    prevent concurrent redraws from multiple worker threads.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._console = Console()
        self._panels: dict[str, _AgentPanelState] = {}
        self._current_round = 0
        self._live: Live | None = None

    def register_round_profiles(
        self,
        round_number: int,
        profiles: tuple["DeliberationAgentProfile", ...],
    ) -> None:
        """Register profiles and (re)point the live display at this round."""
        with self._lock:
            if self._live is None:
                self._live = Live(
                    console=self._console,
                    refresh_per_second=8,
                    vertical_overflow="crop",
                    transient=False,
                )
                self._live.start()
            elif self._panels:
                # Freeze the finished round into scrollback before switching.
                self._console.print(self._build_renderable())
            self._current_round = round_number
            self._panels = {
                profile.profile_id: _AgentPanelState(
                    profile_id=profile.profile_id,
                    provider=profile.agent,
                )
                for profile in profiles
            }
            self._live.update(self._build_renderable())

    def _make_panel(self, state: "_AgentPanelState", body_lines: int) -> Panel:
        """Build one agent panel showing the most recent ``body_lines`` lines.

        Text wraps within the panel (never truncated horizontally); the
        workspace file holds the full, untruncated history.
        """
        title = (
            f"round={self._current_round} agent={state.profile_id} "
            f"provider={state.provider} {state.status}"
        )
        visible = state.lines[-body_lines:]
        content = "\n".join(visible) if visible else "..."
        return Panel(
            Text(content),
            title=title,
            subtitle=f"workspaces/{state.profile_id}/",
            border_style="cyan" if state.status == "running" else "green",
        )

    def _build_renderable(self) -> object:
        """Build the Rich renderable, adapting to the terminal width.

        Wide terminals show one column per agent; narrow terminals (where each
        column would fall below ``_MIN_COLUMN_WIDTH``) stack full-width panels
        vertically so text stays readable.
        """
        if not self._panels:
            return Text("")
        states = list(self._panels.values())
        count = len(states)
        size = self._console.size

        if size.width // count >= _MIN_COLUMN_WIDTH:
            # Force exactly one equal-width column per agent. Table.grid is used
            # instead of rich.Columns because Columns auto-fits by each panel's
            # minimum width and would collapse to fewer columns when titles are
            # long.
            body_lines = max(3, size.height - 6)
            grid = Table.grid(expand=True)
            for _ in states:
                grid.add_column(ratio=1)
            grid.add_row(*(self._make_panel(state, body_lines) for state in states))
            return grid

        # Full-width vertical stack; split the available height across panels.
        body_lines = max(2, (size.height - 2) // count - 2)
        panels = [self._make_panel(state, body_lines) for state in states]
        return Group(*panels)

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

    def log(self, message: str) -> None:
        """Print a session-level line above the live region."""
        with self._lock:
            self._console.print(message, highlight=False)

    def close(self) -> None:
        """Stop the live display, leaving the final frame on screen."""
        with self._lock:
            if self._live is not None:
                self._live.stop()
                self._live = None


class PlainOutputView(IAgentOutputView):
    """Line-prefixed plain text output for non-TTY, CI, or plain mode.

    Each visible output block is prefixed with
    ``[round=<n> agent=<id> status=<status>]`` to provide agent attribution
    without relying on terminal capabilities.
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

    def log(self, message: str) -> None:
        """Print a session-level line."""
        print(message, flush=True)

    def close(self) -> None:
        """No-op close for plain output."""
