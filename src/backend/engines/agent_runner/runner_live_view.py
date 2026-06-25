"""Terminal live view for parallel Issue processing.

Concrete :class:`IRunnerLiveView` implementations used by ``iar daemon`` when it
processes several Issues concurrently (``--concurrency > 1``):

- :class:`RichRunnerLiveView`: interactive TTY display with one live column per
  running Issue, built on the shared :mod:`live_panels` renderer (same look as
  the deliberation view).
- :class:`PlainRunnerLiveView`: line-prefixed plain text for non-TTY, CI,
  redirected, or explicit plain mode.

The per-Issue log file (written by the core output-routing layer) remains the
durable source of truth; these views are only for live, attended viewing.
"""

from __future__ import annotations

import threading

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from backend.core.shared.interfaces.runner_live_view import (
    IRunnerLiveView,
    NoOpRunnerLiveView,
)
from backend.engines.agent_runner.live_panels import PanelState, render_panel_grid
from backend.engines.agent_runner.live_terminal import _is_interactive_tty


def create_runner_live_view(*, plain: bool = False) -> IRunnerLiveView:
    """Create the appropriate runner live view for the environment.

    Args:
        plain: If True, force plain text output regardless of TTY state.

    Returns:
        A :class:`RichRunnerLiveView` on an interactive TTY, otherwise a
        :class:`PlainRunnerLiveView`. Falls back to plain on any Rich init
        failure so the daemon never crashes because of the display layer.
    """
    if plain or not _is_interactive_tty():
        return PlainRunnerLiveView()
    try:
        return RichRunnerLiveView()
    except Exception:  # noqa: BLE001 - any Rich init failure falls back to plain.
        return PlainRunnerLiveView()


class RichRunnerLiveView(IRunnerLiveView):
    """Interactive TTY display with one live column per running Issue.

    Thread-safety: all Rich operations are serialized through a lock because
    worker threads append/update concurrently.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._console = Console()
        self._panels: dict[int, PanelState] = {}
        self._live: Live | None = None

    def register_issue(self, issue_number: int, agent: str) -> None:
        """Add a column for ``issue_number`` and (re)start the live region."""
        with self._lock:
            if self._live is None:
                self._live = Live(
                    console=self._console,
                    refresh_per_second=8,
                    vertical_overflow="crop",
                    transient=False,
                )
                self._live.start()
            self._panels[issue_number] = PanelState(
                panel_id=f"#{issue_number}",
                provider=agent,
            )
            self._panels[issue_number].status = "running"
            self._live.update(self._build_renderable())

    def _make_panel(self, state: PanelState, body_lines: int) -> Panel:
        """Build one Issue panel showing the most recent ``body_lines`` lines."""
        title = f"issue={state.panel_id} agent={state.provider} {state.status}"
        visible = state.lines[-body_lines:]
        content = "\n".join(visible) if visible else "..."
        return Panel(
            Text(content),
            title=title,
            border_style="cyan" if state.status == "running" else "green",
        )

    def _build_renderable(self) -> object:
        """Build the width-adaptive renderable for all Issue panels."""
        return render_panel_grid(
            self._console, list(self._panels.values()), self._make_panel
        )

    def append(self, issue_number: int, chunk: str) -> None:
        """Append output to the corresponding Issue panel."""
        with self._lock:
            panel_state = self._panels.get(issue_number)
            if panel_state is not None:
                panel_state.append(chunk)
                if self._live is not None:
                    self._live.update(self._build_renderable())

    def update_status(self, issue_number: int, status: str) -> None:
        """Update an Issue's status in the display."""
        with self._lock:
            panel_state = self._panels.get(issue_number)
            if panel_state is not None:
                panel_state.status = status
                if self._live is not None:
                    self._live.update(self._build_renderable())

    def log(self, message: str) -> None:
        """Print a pass-level line above the live region."""
        with self._lock:
            self._console.print(message, highlight=False)

    def close(self) -> None:
        """Stop the live display, leaving the final frame on screen."""
        with self._lock:
            if self._live is not None:
                self._live.stop()
                self._live = None


class PlainRunnerLiveView(IRunnerLiveView):
    """Line-prefixed plain text view for non-TTY, CI, or plain mode.

    Each visible output block is prefixed with ``[issue #<n> status=<status>]``
    to provide attribution without relying on terminal capabilities.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._issue_status: dict[int, str] = {}

    def register_issue(self, issue_number: int, agent: str) -> None:
        """Record the Issue and print a start line."""
        with self._lock:
            self._issue_status[issue_number] = "running"
        print(f"[issue #{issue_number} agent={agent} status=running]", flush=True)

    def append(self, issue_number: int, chunk: str) -> None:
        """Print output with an Issue/status prefix."""
        with self._lock:
            status = self._issue_status.get(issue_number, "running")
        prefix = f"[issue #{issue_number} status={status}]"
        for line in chunk.splitlines():
            if line.strip():
                print(f"{prefix} {line}", flush=True)

    def update_status(self, issue_number: int, status: str) -> None:
        """Record and print a status change."""
        with self._lock:
            self._issue_status[issue_number] = status
        print(f"[issue #{issue_number} status={status}]", flush=True)

    def log(self, message: str) -> None:
        """Print a pass-level line."""
        print(message, flush=True)

    def close(self) -> None:
        """No-op close for plain output."""


__all__ = [
    "RichRunnerLiveView",
    "PlainRunnerLiveView",
    "NoOpRunnerLiveView",
    "create_runner_live_view",
]
