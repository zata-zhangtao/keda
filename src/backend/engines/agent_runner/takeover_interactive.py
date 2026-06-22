"""Interactive repository selection for ``iar takeover``.

Implements a simple terminal checkbox UI using ``rich`` (already a project
dependency) so users can pick which GitHub repositories to take over.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from backend.engines.agent_runner.takeover import GitHubRepositoryCandidate


@dataclass
class _SelectableCandidate:
    """Internal wrapper that tracks selection state."""

    candidate: GitHubRepositoryCandidate
    selected: bool = False


def _render_menu(
    console: Console,
    items: list[_SelectableCandidate],
    title: str = "Select repositories to take over",
) -> None:
    """Render the current checkbox menu."""
    text = Text()
    text.append(f"{title}\n\n", style="bold underline")
    for index, item in enumerate(items, start=1):
        marker = "[x]" if item.selected else "[ ]"
        style = "green" if item.selected else "dim"
        line = f"  {marker} {index:3}. {item.candidate.full_name}"
        description = item.candidate.description
        if description:
            line += f" — {description}"
        text.append(line, style=style)
        text.append("\n")
    text.append("\n")
    text.append("Commands: ", style="bold")
    text.append("number", style="cyan")
    text.append(" toggle • ")
    text.append("all", style="cyan")
    text.append(" select all • ")
    text.append("none", style="cyan")
    text.append(" clear • ")
    text.append("done", style="cyan")
    text.append(" confirm • ")
    text.append("quit", style="cyan")
    text.append(" cancel")
    console.print(Panel(text, expand=False))


def select_repositories_interactive(
    candidates: list[GitHubRepositoryCandidate],
    console: Console | None = None,
) -> list[GitHubRepositoryCandidate]:
    """Run an interactive checkbox selection for repository candidates.

    Args:
        candidates: Repositories available for selection.
        console: Optional Rich console.

    Returns:
        Selected repositories. Returns an empty list if the user cancels.
    """
    if not candidates:
        return []

    console = console or Console()
    items = [_SelectableCandidate(candidate=c) for c in candidates]

    while True:
        console.clear()
        _render_menu(console, items)
        console.print()
        raw_input = console.input("Takeover> ").strip().lower()

        if raw_input in ("done", "d", ""):
            selected = [item.candidate for item in items if item.selected]
            if selected:
                console.print(
                    f"\n[green]Confirmed {len(selected)} selected "
                    f"repositor{'y' if len(selected) == 1 else 'ies'}.[/]"
                )
                return selected
            console.print(
                "[yellow]No repositories selected. Please select at least one.[/]"
            )
            console.print()
            console.input("Press Enter to continue...")
            continue

        if raw_input in ("quit", "q", "cancel", "c"):
            return []

        if raw_input == "all":
            for item in items:
                item.selected = True
            continue

        if raw_input == "none":
            for item in items:
                item.selected = False
            continue

        try:
            index = int(raw_input) - 1
            if 0 <= index < len(items):
                items[index].selected = not items[index].selected
            else:
                console.print(f"[red]Invalid number: {raw_input}[/]")
                console.print()
                console.input("Press Enter to continue...")
        except ValueError:
            console.print(f"[red]Unknown command: {raw_input}[/]")
            console.print()
            console.input("Press Enter to continue...")
