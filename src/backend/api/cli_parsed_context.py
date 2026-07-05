"""Shared dispatch context for the parsed-command handlers.

After the line-split refactor each ``if parsed.command == "..."`` block in
:mod:`backend.api.cli` lives in a focused module under
:mod:`backend.api.cli_parsed_commands`. All of those handlers share the
same mutable state extracted from the parsed ``argparse.Namespace`` and
the global runner settings — this dataclass bundles it so the per-command
helpers can take a uniform signature.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from backend.core.shared.interfaces.agent_runner import IGitHubClient

if TYPE_CHECKING:
    # Lazy-imported infrastructure types used in frozen dataclass fields below.
    # The model imports live here so architecture-lint can validate our
    # `api → core → engines → infrastructure` direction.
    pass


@dataclass(frozen=True)
class ParsedCommandContext:
    """Pre-extracted values shared across all parsed-command handlers."""

    parsed: argparse.Namespace
    process_runner: Any  # SubprocessRunner — injected by cli.py dispatcher
    runner_settings: Any  # AgentRunnerSettings — injected by cli.py dispatcher
    repo_id: str | None
    repo_override: str | None
    github_client_factory: Callable[[Path], "IGitHubClient"]


# The "SubprocessRunner" and "AgentRunnerSettings" type hints above are
# string literals — they're only resolved at type-check time. The actual
# instances are created by the cli.py dispatcher (api → engines layer)
# which *also* injects its own `IGitHubClient` factory.

__all__ = ["ParsedCommandContext"]
