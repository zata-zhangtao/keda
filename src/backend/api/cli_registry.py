"""Implementation of the ``iar registry`` subcommands.

Provides ``scan``, ``sync``, ``reinit``, and ``remove`` for managing the
repository registry in ``config.toml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from backend.api.cli_console import console, error_console
from backend.core.shared.interfaces.runner_console import RunnerProcessKind
from backend.core.use_cases.console_processes import (
    start_runner_process,
    stop_runner_process,
)
from backend.engines.agent_runner.factory import (
    create_process_supervisor,
    create_registry_editor,
    load_fresh_agent_runner_settings,
    resolve_console_spawn_cwd,
    resolve_repository_targets_with_diagnostics,
)
from backend.engines.agent_runner.repository_local import (
    RepositoryInitOptions,
    initialize_repository_local_config,
)

# ``ProcessSupervisor`` stores ``kind`` as the string value of the enum member
# (``RunnerProcessKind.DAEMON.value`` / ``RunnerProcessKind.REVIEW_DAEMON.value``),
# not as the enum member itself, so registry commands compare against these
# literal values directly.
_DAEMON_KIND = RunnerProcessKind.DAEMON.value
_REVIEW_DAEMON_KIND = RunnerProcessKind.REVIEW_DAEMON.value

if TYPE_CHECKING:
    import argparse

    from backend.core.shared.interfaces.agent_runner import IProcessRunner


def _run_registry_scan_command(parsed: argparse.Namespace) -> int:
    """Run ``iar registry scan`` (stub kept for parser parity)."""
    raise NotImplementedError("Use backend.api.cli for scan/sync dispatch.")


def _run_registry_reinit_command(
    parsed: argparse.Namespace, process_runner: IProcessRunner
) -> int:
    """Re-initialize an already registered repository's local config."""
    editor = create_registry_editor()
    repo_id = parsed.repo_id

    entries = {entry.repo_id: entry for entry in editor.list_repositories()}
    if repo_id not in entries:
        error_console.print(f"[red]Repository '{repo_id}' not found in registry.[/]")
        return 1

    entry = entries[repo_id]
    repo_path = Path(entry.path).expanduser()
    if not repo_path.exists():
        error_console.print(f"[red]Repository path does not exist:[/] {repo_path}")
        return 1

    try:
        initialize_repository_local_config(
            RepositoryInitOptions(
                cwd=repo_path,
                repo_id_override=repo_id,
                display_name_override=entry.display_name or repo_id,
                remote_override=parsed.remote,
                base_branch_override=parsed.base_branch,
                force=True,
            ),
            process_runner,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
        error_console.print(f"[red]Failed to reinitialize repository:[/] {exc}")
        return 1

    console.print(
        f"[green]Reinitialized[/] {repo_id} "
        f"(remote={parsed.remote}, path={repo_path})"
    )

    if parsed.start_daemons:
        return _restart_daemons(repo_id, repo_path, process_runner)
    return 0


def _run_registry_remove_command(
    parsed: argparse.Namespace, process_runner: IProcessRunner
) -> int:
    """Remove a repository from the registry and stop its daemons."""
    editor = create_registry_editor()
    repo_id = parsed.repo_id

    entries = {entry.repo_id: entry for entry in editor.list_repositories()}
    if repo_id not in entries:
        error_console.print(f"[red]Repository '{repo_id}' not found in registry.[/]")
        return 1

    entry = entries[repo_id]
    repo_path = Path(entry.path).expanduser()

    supervisor = create_process_supervisor()
    for record in supervisor.list_processes():
        if record.repo_id == repo_id and record.kind in (
            _DAEMON_KIND,
            _REVIEW_DAEMON_KIND,
        ):
            try:
                stop_runner_process(
                    process_id=record.process_id,
                    supervisor=supervisor,
                    stop_timeout_seconds=30,
                )
                console.print(f"[green]Stopped[/] {record.kind} {record.process_id}")
            except Exception as exc:  # noqa: BLE001 - best effort stop.
                error_console.print(
                    f"[yellow]Failed to stop {record.kind} {record.process_id}:[/] {exc}"
                )

    try:
        editor.remove_repository(repo_id)
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
        error_console.print(f"[red]Failed to remove registry entry:[/] {exc}")
        return 1

    console.print(f"[green]Removed[/] {repo_id} from registry")

    if parsed.delete:
        if not repo_path.exists():
            console.print(f"[yellow]Repository path already missing:[/] {repo_path}")
            return 0
        registered_path = Path(entry.path).expanduser()
        if repo_path.resolve() != registered_path.resolve():
            error_console.print(
                f"[red]Refusing to delete path that does not match registry record:[/] {repo_path}"
            )
            return 1
        try:
            import shutil

            shutil.rmtree(repo_path)
            console.print(f"[green]Deleted[/] {repo_path}")
        except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
            error_console.print(f"[red]Failed to delete repository path:[/] {exc}")
            return 1

    return 0


def _run_registry_list_command(process_runner: IProcessRunner) -> int:
    """List all registered repositories and their daemon status."""
    from rich.table import Table

    editor = create_registry_editor()
    supervisor = create_process_supervisor()

    running: dict[str, dict[str, list[str]]] = {}
    for record in supervisor.list_processes():
        kind_name = record.kind
        if not isinstance(kind_name, str):
            kind_name = kind_name.value
        running.setdefault(record.repo_id, {}).setdefault(kind_name, []).append(
            record.process_id
        )

    table = Table(title="Registered repositories")
    table.add_column("repo_id", style="cyan")
    table.add_column("display_name")
    table.add_column("path", overflow="fold")
    table.add_column("daemon", style="green")
    table.add_column("review-daemon", style="green")

    for entry in editor.list_repositories():
        repo_running = running.get(entry.repo_id, {})
        daemon = _format_process_status(repo_running, _DAEMON_KIND)
        review_daemon = _format_process_status(repo_running, _REVIEW_DAEMON_KIND)
        table.add_row(
            entry.repo_id,
            entry.display_name or "",
            entry.path,
            daemon,
            review_daemon,
        )

    console.print(table)
    return 0


def _format_process_status(running: dict[str, list[str]], kind: str) -> str:
    """Return a human-readable status string for a daemon kind."""
    process_ids = running.get(kind, [])
    if not process_ids:
        return "[dim]stopped[/]"
    return f"[green]running[/] ({', '.join(process_ids)})"


def _restart_daemons(repo_id: str, repo_path: Path, process_runner) -> int:
    """Stop any existing daemons for a repo and start fresh ones."""
    settings = load_fresh_agent_runner_settings()
    contexts, _failures = resolve_repository_targets_with_diagnostics(settings)
    supervisor = create_process_supervisor()
    runner_command = settings.console.runner_command

    for record in supervisor.list_processes():
        if record.repo_id == repo_id and record.kind in (
            _DAEMON_KIND,
            _REVIEW_DAEMON_KIND,
        ):
            try:
                stop_runner_process(
                    process_id=record.process_id,
                    supervisor=supervisor,
                    stop_timeout_seconds=30,
                )
                console.print(
                    f"[green]Stopped old[/] {record.kind} {record.process_id}"
                )
            except Exception as exc:  # noqa: BLE001 - best effort stop.
                error_console.print(
                    f"[yellow]Failed to stop old {record.kind} {record.process_id}:[/] {exc}"
                )

    spawn_cwd = resolve_console_spawn_cwd()
    for kind in (RunnerProcessKind.DAEMON, RunnerProcessKind.REVIEW_DAEMON):
        try:
            record = start_runner_process(
                repo_id=repo_id,
                kind=kind,
                contexts=contexts,
                supervisor=supervisor,
                runner_command=runner_command,
                spawn_cwd=spawn_cwd,
            )
            console.print(
                f"[green]Started {kind.value}[/] for {repo_id} "
                f"(process {record.process_id})"
            )
        except Exception as exc:  # noqa: BLE001 - daemon start is best effort.
            error_console.print(
                f"[yellow]Failed to start {kind.value} for {repo_id}:[/] {exc}"
            )
            return 1
    return 0
