"""``iar loop *`` / ``iar loop-daemon`` handlers.

The actual command bodies live in :mod:`backend.api.cli_loop`; this
module just routes the parsed ``argparse.Namespace`` through the
existing ``_run_loop_command`` shim so the new dispatcher in
:mod:`backend.api.cli` can treat loops the same way it treats every other
subcommand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.api.cli_parsed_context import ParsedCommandContext

if TYPE_CHECKING:
    pass


def run_loop_command(ctx: ParsedCommandContext) -> int:
    """``iar loop {create|list|cancel|run|daemon}`` dispatcher."""
    from backend.api.cli import _run_loop_command  # local import to break cycle

    return _run_loop_command(ctx.parsed, ctx.process_runner)


__all__ = ["run_loop_command"]
