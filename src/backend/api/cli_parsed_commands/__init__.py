"""Per-command handlers extracted from ``backend.api.cli._run_parsed_command``.

The original dispatcher in :mod:`backend.api.cli` was a single ~1060-line
function. The line-split refactor moved each ``if parsed.command == "..."``
block into a focused module here. Each module exposes one or more
``run_<command>_command(ctx: ParsedCommandContext) -> int`` functions.

:func:`dispatch_parsed_command` is the thin orchestrator that maps a
parsed ``argparse.Namespace`` to the matching handler.
"""

from __future__ import annotations

from backend.api.cli_parsed_commands.agent import (
    run_ask_command,
    run_deliberate_command,
    run_repl_command,
)
from backend.api.cli_parsed_commands.container import (
    run_container_auth_import_command,
    run_container_down_command,
    run_container_logs_command,
    run_container_up_command,
)
from backend.api.cli_parsed_commands.init_workflow_takeover import (
    run_init_command,
    run_takeover_command,
    run_workflow_install_command,
)
from backend.api.cli_parsed_commands.labels_issue import (
    run_issue_create_command,
    run_issue_list_command,
    run_labels_command,
)
from backend.api.cli_parsed_commands.logs import run_logs_command
from backend.api.cli_parsed_commands.loop import run_loop_command
from backend.api.cli_parsed_commands.registry import (
    run_registry_list_command,
    run_registry_reinit_command,
    run_registry_remove_command,
    run_registry_scan_command,
    run_registry_start_command,
    run_registry_stop_command,
    run_registry_sync_command,
)
from backend.api.cli_parsed_commands.runner import (
    run_blocked_continue_command,
    run_daemon_command,
    run_recover_command,
    run_review_command,
    run_review_daemon_command,
    run_run_command,
)
from backend.api.cli_parsed_commands.worktree import run_worktree_command
from backend.api.cli_parsed_context import ParsedCommandContext

__all__ = ["ParsedCommandContext", "dispatch_parsed_command"]


_DISPATCH_TABLE: dict[str, callable] = {
    "init": run_init_command,
    "workflow install": run_workflow_install_command,
    "takeover": run_takeover_command,
    "registry scan": run_registry_scan_command,
    "registry sync": run_registry_sync_command,
    "registry reinit": run_registry_reinit_command,
    "registry remove": run_registry_remove_command,
    "registry list": run_registry_list_command,
    "registry start": run_registry_start_command,
    "registry stop": run_registry_stop_command,
    "worktree": run_worktree_command,
    "labels": run_labels_command,
    "issue create": run_issue_create_command,
    "issue list": run_issue_list_command,
    "run": run_run_command,
    "daemon": run_daemon_command,
    "review": run_review_command,
    "review-daemon": run_review_daemon_command,
    "recover": run_recover_command,
    "blocked-continue": run_blocked_continue_command,
    "ask": run_ask_command,
    "repl": run_repl_command,
    "deliberate": run_deliberate_command,
    "logs": run_logs_command,
    "loop create": run_loop_command,
    "loop list": run_loop_command,
    "loop cancel": run_loop_command,
    "loop run": run_loop_command,
    "loop-daemon": run_loop_command,
    "container auth import": run_container_auth_import_command,
    "container up": run_container_up_command,
    "container down": run_container_down_command,
    "container logs": run_container_logs_command,
}


def dispatch_parsed_command(ctx: ParsedCommandContext) -> int | None:
    """Run the handler matching ``ctx.parsed.command``.

    Returns ``None`` when no handler is registered for the parsed command
    so the caller can fall back to its own default-deny behaviour.
    """
    handler = _DISPATCH_TABLE.get(ctx.parsed.command)
    if handler is None:
        return None
    return handler(ctx)
