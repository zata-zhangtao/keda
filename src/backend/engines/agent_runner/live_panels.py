"""Shared Rich live-panel rendering for multi-stream terminal views.

Both the deliberation output view (one column per agent profile) and the runner
live view (one column per concurrently processed Issue) display a set of live
panels sized to the terminal. The panel buffer state and the width-adaptive
grid/stack layout live here so neither view duplicates the rendering — keeping
the two views visually consistent and jscpd quiet.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Lines kept in memory per panel. The workspace/log file remains the source of
# truth for full history; the panel only needs enough to fill the screen.
PANEL_BUFFER_LINES = 200

# Minimum readable text width per side-by-side column. Below this, columns are
# too narrow to read, so the view falls back to full-width vertical stacking.
MIN_COLUMN_WIDTH = 40


class PanelState:
    """Track per-stream display state for a single live panel."""

    __slots__ = ("panel_id", "provider", "status", "lines")

    def __init__(self, panel_id: str, provider: str) -> None:
        self.panel_id = panel_id
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
        if len(self.lines) > PANEL_BUFFER_LINES:
            self.lines = self.lines[-PANEL_BUFFER_LINES:]


def render_panel_grid(
    console: Console,
    states: list[PanelState],
    make_panel: Callable[[PanelState, int], Panel],
) -> object:
    """Build a Rich renderable for ``states``, adapting to terminal width.

    Wide terminals show one equal-width column per panel; narrow terminals
    (where each column would fall below :data:`MIN_COLUMN_WIDTH`) stack
    full-width panels vertically so text stays readable.

    Args:
        console: Console whose ``size`` drives the layout decision.
        states: Panels to render, left to right / top to bottom.
        make_panel: Builds one ``Panel`` given its state and the number of
            body lines it may show.

    Returns:
        A Rich renderable (grid or vertical group).
    """
    if not states:
        return Text("")
    count = len(states)
    size = console.size

    if size.width // count >= MIN_COLUMN_WIDTH:
        # Force exactly one equal-width column per panel. Table.grid is used
        # instead of rich.Columns because Columns auto-fits by each panel's
        # minimum width and would collapse to fewer columns when titles are
        # long.
        body_lines = max(3, size.height - 6)
        grid = Table.grid(expand=True)
        for _ in states:
            grid.add_column(ratio=1)
        grid.add_row(*(make_panel(state, body_lines) for state in states))
        return grid

    # Full-width vertical stack; split the available height across panels.
    body_lines = max(2, (size.height - 2) // count - 2)
    return Group(*(make_panel(state, body_lines) for state in states))
