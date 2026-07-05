"""``iar logs`` handler.

Extracted from :mod:`backend.api.cli`'s monolithic ``_run_parsed_command``
dispatcher.
"""

from __future__ import annotations

from backend.api.cli_parsed_context import ParsedCommandContext
from backend.api.cli_registry import _run_logs_command


def run_logs_command(ctx: ParsedCommandContext) -> int:
    """``iar logs``: tail the most recent log lines for a managed daemon."""
    return _run_logs_command(
        parsed=ctx.parsed,
        process_runner=ctx.process_runner,
        runner_settings=ctx.runner_settings,
        repo_id=ctx.repo_id,
        repo_override=ctx.repo_override,
    )


__all__ = ["run_logs_command"]
