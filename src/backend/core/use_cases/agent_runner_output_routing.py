"""Per-Issue output routing for parallel daemon passes.

When ``iar daemon`` processes Issues concurrently, each Issue's agent stream and
log lines must be attributable instead of interleaving on one stdout. This
module provides the core-side plumbing, depending only on ``core/shared``
interfaces and the standard library (no ``engines`` / ``infrastructure``
imports), so the layering rule ``core -> engines -> infrastructure`` holds:

- :class:`_OutputRoutedProcessRunner` wraps any :class:`IProcessRunner` and
  injects a per-Issue ``output_sink`` into every ``run`` call. Because the
  process runner is already threaded through the whole processing pipeline, this
  routes the agent stream without adding a parameter to every function.
- :func:`issue_output_routing` opens the per-Issue log file, builds the sink
  (file + live-view panel) and installs a thread-scoped logging handler so the
  worker thread's ``_logger`` lines also land in that Issue's file.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Sequence

from backend.core.shared.interfaces.runner_live_view import IRunnerLiveView
from backend.core.shared.models.agent_runner import CommandResult

# Logger namespace the per-Issue handler attaches to. All backend modules log
# under this root (e.g. ``backend.core.use_cases.agent_runner_orchestrate``), so
# attaching here captures the worker thread's narrative via propagation.
_BACKEND_LOGGER_NAME = "backend"


class _OutputRoutedProcessRunner:
    """Wrap an ``IProcessRunner`` and inject a per-Issue ``output_sink``.

    Implements the ``IProcessRunner`` contract via duck typing. Every ``run``
    call is delegated to the wrapped runner with ``output_sink`` defaulted to
    this Issue's sink, unless the caller passed an explicit sink.
    """

    def __init__(self, wrapped: object, sink: Callable[[str], None]) -> None:
        self._wrapped = wrapped
        self._sink = sink

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout: int | None = None,
        inactivity_timeout: int | None = None,
        capture_output: bool = True,
        input_text: str | None = None,
        label: str | None = None,
        output_sink: Callable[[str], None] | None = None,
    ) -> CommandResult:
        """Delegate to the wrapped runner, defaulting ``output_sink`` per Issue."""
        return self._wrapped.run(
            command,
            cwd=cwd,
            check=check,
            timeout=timeout,
            inactivity_timeout=inactivity_timeout,
            capture_output=capture_output,
            input_text=input_text,
            label=label,
            output_sink=output_sink if output_sink is not None else self._sink,
        )


class _IssueLogWriter:
    """Thread-safe append writer used by both the sink and the log handler."""

    def __init__(self, file_path: Path) -> None:
        self._lock = threading.Lock()
        self._file = file_path.open("a", encoding="utf-8")

    def write(self, text: str) -> None:
        """Append ``text`` and flush so live ``tail -f`` sees it promptly."""
        with self._lock:
            self._file.write(text)
            self._file.flush()

    def flush(self) -> None:
        """Flush the underlying file (used by the logging handler)."""
        with self._lock:
            if not self._file.closed:
                self._file.flush()

    def close(self) -> None:
        """Close the underlying file."""
        with self._lock:
            if not self._file.closed:
                self._file.close()


class _ThreadLogFilter(logging.Filter):
    """Only pass log records emitted from a specific thread."""

    def __init__(self, thread_ident: int) -> None:
        super().__init__()
        self._thread_ident = thread_ident

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True only for records from the registered thread."""
        return record.thread == self._thread_ident


def per_issue_log_path(log_base: Path, repo_id: str, issue_number: int) -> Path:
    """Return the per-Issue log file path under ``log_base``.

    Layout: ``<log_base>/agent-runner/issues/<repo_id>/issue-<n>-<ts>.log``.
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return (
        log_base
        / "agent-runner"
        / "issues"
        / repo_id
        / f"issue-{issue_number}-{timestamp}.log"
    )


@contextmanager
def issue_output_routing(
    *,
    repo_id: str,
    issue_number: int,
    log_base: Path,
    output_view: IRunnerLiveView,
) -> Iterator[Callable[[str], None]]:
    """Route one Issue's output to its own log file and live-view panel.

    Yields a ``sink(chunk)`` callable to hand to
    :class:`_OutputRoutedProcessRunner`. While active, the calling thread's
    ``backend.*`` log records are also written to the Issue's file (scoped by
    thread id), so the file holds both the agent stream and the worker
    narrative. The handler and file are torn down on exit.

    Args:
        repo_id: Repository identifier (log subdirectory).
        issue_number: Issue number (log filename + panel key).
        log_base: Base directory for logs (typically ``<repo_path>/logs``).
        output_view: Live view receiving each chunk for the Issue's panel.
    """
    file_path = per_issue_log_path(log_base, repo_id, issue_number)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    writer = _IssueLogWriter(file_path)

    def sink(chunk: str) -> None:
        writer.write(chunk)
        output_view.append(issue_number, chunk)

    handler = logging.StreamHandler(stream=writer)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(_ThreadLogFilter(threading.get_ident()))
    backend_logger = logging.getLogger(_BACKEND_LOGGER_NAME)
    backend_logger.addHandler(handler)
    try:
        yield sink
    finally:
        backend_logger.removeHandler(handler)
        writer.close()
