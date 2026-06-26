"""Implementation of the ``iar registry`` and ``iar logs`` subcommands.

Provides ``scan``, ``sync``, ``reinit``, ``remove``, ``logs``, and ``daemon
status`` for managing the repository registry in ``config.toml`` and viewing
managed process logs.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.api.cli_console import console, error_console
from backend.core.shared.interfaces.runner_console import (
    RunnerProcessKind,
    RunnerProcessRecord,
)
from backend.core.use_cases.console_processes import (
    _DEFAULT_LOG_CHUNK_BYTES,
    start_runner_process,
    stop_runner_process,
    tail_runner_log,
)
from backend.engines.agent_runner.factory import (
    create_process_supervisor,
    create_registry_editor,
    load_fresh_agent_runner_settings,
    resolve_config_toml_path,
    resolve_project_root_path,
    resolve_repository_targets,
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

    registry_entries = editor.list_repositories()

    # managed_running[repo_id][kind] = list[(process_id, is_managed)]
    running: dict[str, dict[str, list[tuple[str, bool]]]] = {}
    for record in supervisor.list_processes():
        if record.status != "running":
            continue
        kind_name = record.kind
        if not isinstance(kind_name, str):
            kind_name = kind_name.value
        running.setdefault(record.repo_id, {}).setdefault(kind_name, []).append(
            (record.process_id, True)
        )

    for record in supervisor.list_unmanaged_processes(registry_entries):
        if record.status != "running":
            continue
        kind_name = record.kind
        if not isinstance(kind_name, str):
            kind_name = kind_name.value
        running.setdefault(record.repo_id, {}).setdefault(kind_name, []).append(
            (record.process_id, False)
        )

    table = Table(title="Registered repositories")
    table.add_column("repo_id", style="cyan")
    table.add_column("display_name")
    table.add_column("path", overflow="fold")
    table.add_column("daemon", style="green")
    table.add_column("review-daemon", style="green")

    for entry in registry_entries:
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


def _run_registry_start_command(
    parsed: argparse.Namespace, process_runner: IProcessRunner
) -> int:
    """Start daemon and review-daemon for registered repositories."""
    settings = load_fresh_agent_runner_settings()
    supervisor = create_process_supervisor()
    runner_command = settings.console.runner_command

    repo_ids: list[str]
    if parsed.all:
        repo_ids = [
            repo_id
            for repo_id, repo_settings in settings.repositories.items()
            if repo_settings.enabled
        ]
    else:
        repo_id = parsed.repo_id
        if repo_id not in settings.repositories:
            error_console.print(
                f"[red]Repository '{repo_id}' not found in registry.[/]"
            )
            return 1
        repo_entry = settings.repositories[repo_id]
        if not repo_entry.enabled:
            error_console.print(f"[red]Repository '{repo_id}' is disabled.[/]")
            return 1
        repo_ids = [repo_id]

    if not repo_ids:
        console.print("[yellow]No enabled repositories to start.[/]")
        return 0

    contexts, failures = resolve_repository_targets_with_diagnostics(settings)
    if failures:
        for failure in failures:
            error_console.print(
                f"[yellow]Skipping unresolvable repository '{failure.repo_id}': "
                f"{failure.error}[/]"
            )

    kinds = [RunnerProcessKind.DAEMON]
    if not parsed.no_review_daemon:
        kinds.append(RunnerProcessKind.REVIEW_DAEMON)

    exit_code = 0
    spawn_cwd = resolve_config_toml_path().parent
    for repo_id in repo_ids:
        repo_entry = settings.repositories[repo_id]
        repo_path = Path(repo_entry.path).expanduser()
        if not repo_path.exists():
            error_console.print(f"[red]Repository path does not exist:[/] {repo_path}")
            exit_code = 1
            continue

        repo_success = True
        for kind in kinds:
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
            except Exception as exc:  # noqa: BLE001 - best effort start.
                repo_success = False
                error_console.print(
                    f"[yellow]Failed to start {kind.value} for {repo_id}:[/] {exc}"
                )
        if not repo_success:
            exit_code = 1

    return exit_code


def _run_registry_stop_command(
    parsed: argparse.Namespace, process_runner: IProcessRunner
) -> int:
    """Stop daemon and review-daemon for registered repositories."""
    supervisor = create_process_supervisor()
    records = supervisor.list_processes()

    target_kinds = {_DAEMON_KIND}
    if not parsed.no_review_daemon:
        target_kinds.add(_REVIEW_DAEMON_KIND)

    matched_records = []
    for record in records:
        kind = record.kind if isinstance(record.kind, str) else record.kind.value
        if kind not in target_kinds:
            continue
        if parsed.all or record.repo_id == parsed.repo_id:
            matched_records.append(record)

    if not matched_records:
        console.print("[yellow]No running daemon processes to stop.[/]")
        return 0

    exit_code = 0
    for record in matched_records:
        if record.status != "running":
            console.print(
                f"[dim]Skipped[/] {record.kind} {record.process_id} for {record.repo_id} "
                f"(not running)"
            )
            continue
        try:
            stop_runner_process(
                process_id=record.process_id,
                supervisor=supervisor,
                stop_timeout_seconds=30,
            )
            console.print(
                f"[green]Stopped[/] {record.kind} {record.process_id} for {record.repo_id}"
            )
        except Exception as exc:  # noqa: BLE001 - best effort stop.
            error_console.print(
                f"[yellow]Failed to stop {record.kind} {record.process_id} "
                f"for {record.repo_id}:[/] {exc}"
            )
            exit_code = 1

    return exit_code


def _format_process_status(
    running: dict[str, list[tuple[str, bool]]], kind: str
) -> str:
    """Return a human-readable status string for a daemon kind.

    Managed running processes are shown with their process IDs. Unmanaged
    running processes are shown as ``running (unmanaged)`` so users can tell
    which daemons were started outside of ``iar registry start`` / console.
    """
    entries = running.get(kind, [])
    if not entries:
        return "[dim]stopped[/]"

    managed_ids = [process_id for process_id, is_managed in entries if is_managed]
    if managed_ids:
        return f"[green]running[/] ({', '.join(managed_ids)})"

    unmanaged_count = sum(1 for _, is_managed in entries if not is_managed)
    if unmanaged_count == 1:
        return "[yellow]running[/] (unmanaged)"
    return f"[yellow]running[/] (unmanaged x{unmanaged_count})"


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

    spawn_cwd = resolve_config_toml_path().parent
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


def _resolve_executable_from_command(command: tuple[str, ...]) -> str:
    """Return the executable script path from a process command line.

    For direct invocations such as ``/path/bin/iar daemon ...`` the first
    argument is returned. For Python wrapper invocations such as
    ``/path/python /path/bin/iar daemon ...`` the ``iar`` script path is
    returned.
    """
    if not command:
        return ""
    first = command[0]
    if len(command) >= 2 and ("python" in Path(first).name.lower()):
        second = command[1]
        if Path(second).name == "iar" or second.endswith("/iar"):
            return second
    return first


def _run_daemon_status_command(
    parsed: argparse.Namespace,
    process_runner: IProcessRunner,
    runner_settings: Any,
    repo_id: str | None,
    repo_override: str | None,
) -> int:
    """Show running daemon and review-daemon processes for selected repos."""
    from rich.table import Table

    contexts = resolve_repository_targets(
        runner_settings,
        repo_id=repo_id,
        repo_path_override=repo_override,
        all_repositories=getattr(parsed, "all_repositories", False),
    )
    if not contexts:
        console.print("[yellow]No repositories selected.[/]")
        return 0

    target_repo_ids = {context.repo_id for context in contexts}
    registry_entries = list(create_registry_editor().list_repositories())
    supervisor = create_process_supervisor()

    running_records: list[tuple[RunnerProcessRecord, bool]] = []
    for record in supervisor.list_processes():
        if (
            record.status == "running"
            and record.repo_id in target_repo_ids
            and record.kind in (_DAEMON_KIND, _REVIEW_DAEMON_KIND)
        ):
            running_records.append((record, True))
    for record in supervisor.list_unmanaged_processes(registry_entries):
        if (
            record.status == "running"
            and record.repo_id in target_repo_ids
            and record.kind in (_DAEMON_KIND, _REVIEW_DAEMON_KIND)
        ):
            running_records.append((record, False))

    if not running_records:
        console.print(
            "[yellow]No running daemon processes for the selected repositories.[/]"
        )
        return 0

    table = Table(title="Daemon status")
    table.add_column("repo_id", style="cyan")
    table.add_column("kind", style="green")
    table.add_column("status")
    table.add_column("pid", justify="right")
    table.add_column("process_id")
    table.add_column("started_at")
    table.add_column("log_path", overflow="fold")
    table.add_column("executable", overflow="fold")
    table.add_column("command", overflow="fold")

    for record, is_managed in running_records:
        status_text = (
            "[green]managed running[/]"
            if is_managed
            else "[yellow]unmanaged running[/]"
        )
        executable = _resolve_executable_from_command(record.command)
        log_path_display = record.log_path or "-"
        table.add_row(
            record.repo_id,
            record.kind,
            status_text,
            str(record.pid),
            record.process_id,
            record.started_at,
            log_path_display,
            executable,
            " ".join(record.command),
        )

    console.print(table)
    return 0


_LOGS_DEFAULT_LINES = 200
_LOGS_POLL_INTERVAL_SECONDS = 1.0


def _normalize_process_kind(record_kind: RunnerProcessKind | str) -> str:
    """Return the string value of a process kind, accepting enum or string."""
    if isinstance(record_kind, str):
        return record_kind
    return record_kind.value


def _select_logs_record(
    records: list[RunnerProcessRecord], repo_id: str, kind: str
) -> RunnerProcessRecord | None:
    """Select the most relevant record for ``iar logs`` display.

    Preference order:

    1. The newest running record for ``(repo_id, kind)``.
    2. Otherwise the newest historical record with a non-empty ``log_path``
       (so the user can still inspect past output and ``-f`` to EOF).
    3. Otherwise ``None`` — caller renders the fallback hint.
    """
    matching = [
        record
        for record in records
        if record.repo_id == repo_id and _normalize_process_kind(record.kind) == kind
    ]
    if not matching:
        return None
    running = [record for record in matching if record.status == "running"]
    if running:
        return running[-1]
    with_log = [record for record in matching if record.log_path]
    if with_log:
        return with_log[-1]
    return None


def _compute_tail_window_offset(log_path: str, lines: int) -> int:
    """Return the byte offset where the tail reader should start reading.

    Reads ``Path(log_path).stat().st_size`` only (no file open), so the CLI
    stays inside the api→infra boundary while still honouring very large
    logs that exceed the default 64 KiB chunk window.
    """
    if lines <= 0:
        return 0
    try:
        file_size = Path(log_path).stat().st_size
    except OSError:
        return 0
    return max(0, file_size - _DEFAULT_LOG_CHUNK_BYTES)


def _trim_to_last_lines(content: str, lines: int) -> str:
    """Return ``content`` keeping only its trailing ``lines`` lines."""
    if lines <= 0 or not content:
        return content
    split_lines = content.splitlines()
    if len(split_lines) <= lines:
        return content
    return "\n".join(split_lines[-lines:]) + "\n"


def _format_logs_initial_payload(
    *,
    initial_chunk: Any,
    log_path: str,
    requested_start_offset: int,
    lines: int,
) -> str:
    """Render the initial tail payload, prefixing a truncation notice when needed."""
    pieces: list[str] = []
    if requested_start_offset > 0 and initial_chunk.content:
        pieces.append(f"[dim](earlier log lines omitted; see {log_path})[/]\n")
    trimmed = _trim_to_last_lines(initial_chunk.content, lines)
    if trimmed and not trimmed.endswith("\n"):
        trimmed = trimmed + "\n"
    pieces.append(trimmed)
    return "".join(pieces)


def _print_logs_fallback(repo_id: str, kind: str) -> int:
    """Print a fallback hint when no running or historical log is available."""
    supervisor = create_process_supervisor()
    all_records = supervisor.list_processes()
    kind_records = sorted(
        [
            r
            for r in all_records
            if r.repo_id == repo_id and _normalize_process_kind(r.kind) == kind
        ],
        key=lambda r: r.started_at or "",
        reverse=True,
    )
    recent_with_log = next(
        (r for r in kind_records if r.log_path and Path(r.log_path).exists()),
        None,
    )
    if recent_with_log:
        console.print(
            f"[yellow]No running {kind} process for '{repo_id}'.[/]\n"
            f"Most recent process log: [cyan]{recent_with_log.log_path}[/]"
        )
    else:
        # Resolve the daily app-log path through the engines layer
        # (resolve_project_root_path keeps api→core→engines→infra direction
        # intact). The format mirrors infrastructure/logging/logger.py:
        # ``<log_dir>/app-YYYY-MM-DD.log`` where ``log_dir`` defaults to
        # ``<project_root>/logs``.
        today_str = datetime.now().strftime("%Y-%m-%d")
        app_log_path = resolve_project_root_path() / "logs" / f"app-{today_str}.log"
        console.print(
            f"[yellow]No running {kind} process for '{repo_id}' "
            "and no process log records found.[/]\n"
            f"Global app log: [cyan]{app_log_path}[/]"
        )
    return 0


def _run_logs_command(
    parsed: argparse.Namespace,
    process_runner: IProcessRunner,
    runner_settings: Any,
    repo_id: str,
    repo_override: str | None,
) -> int:
    """Print the recent log of a managed daemon / review-daemon process."""
    del (
        process_runner
    )  # Unused; kept for dispatch signature parity with sibling handlers.

    supervisor = create_process_supervisor()

    contexts = resolve_repository_targets(
        runner_settings,
        repo_id=repo_id,
        repo_path_override=repo_override,
        all_repositories=getattr(parsed, "all_repositories", False),
    )
    if len(contexts) != 1:
        error_console.print(
            "[red]iar logs requires exactly one target repository. "
            "Use --repo or --repo-id to specify.[/]"
        )
        return 1
    context = contexts[0]

    kind = getattr(parsed, "kind", _DAEMON_KIND) or _DAEMON_KIND
    if kind not in (_DAEMON_KIND, _REVIEW_DAEMON_KIND):
        error_console.print(
            f"[red]Unsupported --kind value:[/] {kind!r}. "
            f"Use '{_DAEMON_KIND}' or '{_REVIEW_DAEMON_KIND}'."
        )
        return 1

    lines = int(getattr(parsed, "lines", _LOGS_DEFAULT_LINES) or _LOGS_DEFAULT_LINES)
    follow = bool(getattr(parsed, "follow", False))

    records = supervisor.list_processes()
    selected = _select_logs_record(records, context.repo_id, kind)

    if (
        selected is None
        or not selected.log_path
        or not Path(selected.log_path).exists()
    ):
        return _print_logs_fallback(context.repo_id, kind)

    start_offset = _compute_tail_window_offset(selected.log_path, lines)
    initial_chunk = tail_runner_log(
        process_id=selected.process_id,
        offset=start_offset,
        supervisor=supervisor,
        max_bytes=_DEFAULT_LOG_CHUNK_BYTES,
    )

    print(
        _format_logs_initial_payload(
            initial_chunk=initial_chunk,
            log_path=selected.log_path,
            requested_start_offset=start_offset,
            lines=lines,
        ),
        end="",
    )

    next_offset = initial_chunk.next_offset
    if not follow:
        return 0

    while True:
        try:
            time.sleep(_LOGS_POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            return 0
        try:
            latest = supervisor.get_process(selected.process_id)
        except KeyError:
            error_console.print(
                f"[yellow]Process {selected.process_id} is no longer registered.[/]"
            )
            return 0
        if latest is None:
            error_console.print(
                f"[yellow]Process {selected.process_id} is no longer registered.[/]"
            )
            return 0
        chunk = tail_runner_log(
            process_id=selected.process_id,
            offset=next_offset,
            supervisor=supervisor,
            max_bytes=_DEFAULT_LOG_CHUNK_BYTES,
        )
        if chunk.content:
            print(chunk.content, end="")
        next_offset = chunk.next_offset
        if latest.status != "running" and chunk.eof:
            print(
                f"\n[dim](process exited; status={latest.status}, "
                f"exit_code={latest.exit_code}; tail ends here)[/]"
            )
            return 0
