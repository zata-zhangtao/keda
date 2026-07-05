"""``iar init``, ``iar workflow install``, and ``iar takeover`` handlers.

Extracted from :mod:`backend.api.cli`'s monolithic ``_run_parsed_command``
dispatcher. Each handler takes a :class:`ParsedCommandContext` and
returns an int exit code.
"""

from __future__ import annotations

from pathlib import Path

from backend.api.cli_console import console
from backend.api.cli_helpers import _handle_not_initialized_error
from backend.api.cli_init import (
    _print_workflow_config_plan,
    _run_init_command,
)
from backend.api.cli_parsed_context import ParsedCommandContext
from backend.api.cli_takeover import _run_takeover_command
from backend.engines.agent_runner.factory import logger
from backend.engines.agent_runner.repository_local import (
    IARRepositoryNotInitializedError,
)
from backend.engines.agent_runner.workflow_install import (
    ExistingFileRefusedError,
    UnknownWorkflowError,
    WorkflowInstallOptions,
    install_workflow,
)


def run_init_command(ctx: ParsedCommandContext) -> int:
    """``iar init``: create repository-local .iar.toml config."""
    if ctx.repo_id is not None or ctx.repo_override is not None:
        logger.error("iar init uses the current Git repository; omit --repo/--repo-id.")
        return 1
    return _run_init_command(ctx.parsed, ctx.process_runner)


def run_workflow_install_command(ctx: ParsedCommandContext) -> int:
    """``iar workflow install``: bundle a workflow template into the repo."""
    if ctx.repo_id is not None or ctx.repo_override is not None or ctx.parsed.config is not None:
        logger.error(
            "iar workflow install uses the current Git repository; omit --repo/--repo-id/--config."
        )
        return 1
    try:
        install_result = install_workflow(
            WorkflowInstallOptions(
                cwd=Path.cwd(),
                name=ctx.parsed.name,
                force=ctx.parsed.force,
                dry_run=ctx.parsed.dry_run,
            ),
            ctx.process_runner,
        )
    except UnknownWorkflowError as exc:
        logger.error("%s", exc)
        return 1
    except ExistingFileRefusedError as exc:
        logger.error("%s", exc)
        return 1
    except IARRepositoryNotInitializedError as exc:
        return _handle_not_initialized_error(exc)
    except ValueError as exc:
        logger.error("iar workflow install failed: %s", exc)
        return 1
    if ctx.parsed.dry_run:
        console.print("[cyan]Would install workflow:[/] %s" % install_result.name)
        for plan in install_result.template_file_plans:
            marker = (
                "[yellow]would overwrite[/]" if plan.exists_on_disk else "[green]would write[/]"
            )
            console.print("  %s %s (%d bytes)" % (marker, plan.target_path, plan.bytes_to_write))
        _print_workflow_config_plan(install_result.config_toml_plan, dry_run=True)
        return 0

    for plan in install_result.template_file_plans:
        if plan.exists_on_disk and install_result.refused_template_paths:
            continue
        console.print(
            "%s %s"
            % (
                "[green]Wrote[/]" if not plan.exists_on_disk else "[yellow]Overwrote[/]",
                plan.target_path,
            )
        )
    _print_workflow_config_plan(install_result.config_toml_plan, dry_run=False)
    return 0


def run_takeover_command(ctx: ParsedCommandContext) -> int:
    """``iar takeover``: bulk import + register GitHub repositories."""
    return _run_takeover_command(ctx.parsed, ctx.process_runner)


__all__ = ["run_init_command", "run_takeover_command", "run_workflow_install_command"]
