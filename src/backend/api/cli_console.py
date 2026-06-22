"""Shared Rich console instances for the IAR CLI.

Centralising ``Console`` instances avoids circular imports when CLI command
implementations are split into separate modules.
"""

from __future__ import annotations

from rich.console import Console

console = Console()
error_console = Console(stderr=True)
