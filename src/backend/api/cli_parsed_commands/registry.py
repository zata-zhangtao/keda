"""``iar registry *`` handlers.

Extracted from :mod:`backend.api.cli`'s monolithic ``_run_parsed_command``
dispatcher.
"""

from __future__ import annotations

from pathlib import Path

from backend.api.cli_console import console
from backend.api.cli_parsed_context import ParsedCommandContext
from backend.api.cli_registry import (
    _run_registry_list_command,
    _run_registry_reinit_command,
    _run_registry_remove_command,
    _run_registry_start_command,
    _run_registry_stop_command,
)
from backend.engines.agent_runner.factory import logger
from backend.engines.agent_runner.factory import create_registry_editor
from backend.engines.agent_runner.repository_local import discover_iar_repositories


def run_registry_scan_command(ctx: ParsedCommandContext) -> int:
    """``iar registry scan``: discover IAR-initialized repos under a path."""
    try:
        entries = discover_iar_repositories(
            scan_root=Path(ctx.parsed.scan_root),
            editor=create_registry_editor(),
        )
    except ValueError as exc:
        logger.error("iar registry scan failed: %s", exc)
        return 1
    if not entries:
        console.print("[yellow]No IAR repositories found.[/]")
        return 0
    for entry in entries:
        status = "registered" if entry.already_registered else "new"
        print(f"[{entry.repo_id}] {entry.path} ({status})")
    return 0


def run_registry_sync_command(ctx: ParsedCommandContext) -> int:
    """``iar registry sync``: discover and register all IAR repositories."""
    try:
        entries = discover_iar_repositories(
            scan_root=Path(ctx.parsed.scan_root),
            editor=create_registry_editor(),
        )
    except ValueError as exc:
        logger.error("iar registry sync failed: %s", exc)
        return 1
    new_entries = [entry for entry in entries if not entry.already_registered]
    if not new_entries:
        console.print("[green]No new IAR repositories to register.[/]")
        return 0
    if ctx.parsed.dry_run:
        console.print("[cyan]Would register:[/]")
        for entry in new_entries:
            console.print(f"  {entry.repo_id}: {entry.path}")
        return 0
    editor = create_registry_editor()
    added = 0
    for entry in new_entries:
        try:
            editor.add_repository(
                repo_id=entry.repo_id,
                path=entry.path,
                display_name=entry.display_name,
            )
        except ValueError as exc:
            logger.warning("Skipping %s: %s", entry.repo_id, exc)
            continue
        added += 1
        console.print(f"[green]Registered:[/] {entry.repo_id}")
    console.print(f"[green]Registered {added} repository(s).[/]")
    return 0


def run_registry_reinit_command(ctx: ParsedCommandContext) -> int:
    return _run_registry_reinit_command(ctx.parsed, ctx.process_runner)


def run_registry_remove_command(ctx: ParsedCommandContext) -> int:
    return _run_registry_remove_command(ctx.parsed, ctx.process_runner)


def run_registry_list_command(ctx: ParsedCommandContext) -> int:
    return _run_registry_list_command(ctx.process_runner)


def run_registry_start_command(ctx: ParsedCommandContext) -> int:
    return _run_registry_start_command(ctx.parsed, ctx.process_runner)


def run_registry_stop_command(ctx: ParsedCommandContext) -> int:
    return _run_registry_stop_command(ctx.parsed, ctx.process_runner)


__all__ = [
    "run_registry_list_command",
    "run_registry_reinit_command",
    "run_registry_remove_command",
    "run_registry_scan_command",
    "run_registry_start_command",
    "run_registry_stop_command",
    "run_registry_sync_command",
]
