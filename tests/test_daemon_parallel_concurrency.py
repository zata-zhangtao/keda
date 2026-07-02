"""Tests for parallel daemon execution plumbing: output routing and live views.

Covers the per-Issue output-routing layer (``agent_runner_output_routing``), the
process runner's ``output_sink`` forwarding, and the runner live views. The
end-to-end ``run_once`` parallel behavior is tested in
``test_agent_runner_orchestrate``.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from backend.core.shared.interfaces.runner_live_view import NoOpRunnerLiveView
from backend.core.use_cases.agent_runner_output_routing import (
    _OutputRoutedProcessRunner,
    issue_output_routing,
    per_issue_log_path,
)
from backend.engines.agent_runner.runner_live_view import (
    PlainRunnerLiveView,
    create_runner_live_view,
)
from backend.infrastructure import process_runner as process_runner_module
from backend.infrastructure.process_runner import SubprocessRunner
from tests.conftest import FakeProcessRunner


class _RecordingView(NoOpRunnerLiveView):
    """Live view that records the chunks appended per Issue."""

    def __init__(self) -> None:
        self.appended: list[tuple[int, str]] = []
        self.statuses: list[tuple[int, str]] = []

    def append(self, issue_number: int, chunk: str) -> None:
        self.appended.append((issue_number, chunk))

    def update_status(self, issue_number: int, status: str) -> None:
        self.statuses.append((issue_number, status))


# --- SubprocessRunner.output_sink forwarding -------------------------------


def test_subprocess_runner_routes_streamed_output_to_sink(tmp_path: Path) -> None:
    """A non-captured command's stdout is routed to output_sink, not printed."""
    chunks: list[str] = []
    SubprocessRunner().run(
        [sys.executable, "-c", "print('hello-sink')"],
        cwd=tmp_path,
        check=False,
        capture_output=False,
        output_sink=chunks.append,
    )
    assert any("hello-sink" in chunk for chunk in chunks)


def test_subprocess_runner_forwards_output_sink_to_claude_stream(
    monkeypatch, tmp_path: Path
) -> None:
    """Claude stream-json commands forward the sink to run_filtered_claude_stream."""
    captured: dict[str, object] = {}

    def _fake_stream(command, **kwargs):
        captured["output_sink"] = kwargs.get("output_sink")
        return subprocess.CompletedProcess(
            args=list(command), returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(
        process_runner_module, "run_filtered_claude_stream", _fake_stream
    )

    def sink(_chunk: str) -> None:
        return None

    SubprocessRunner().run(
        ["claude", "--output-format", "stream-json", "-p", "hi"],
        cwd=tmp_path,
        check=False,
        capture_output=False,
        output_sink=sink,
    )
    assert captured["output_sink"] is sink


# --- _OutputRoutedProcessRunner --------------------------------------------


def test_output_routed_runner_injects_default_sink(tmp_path: Path) -> None:
    """The wrapper defaults output_sink to the Issue's sink."""
    base = FakeProcessRunner()

    def issue_sink(_chunk: str) -> None:
        return None

    wrapped = _OutputRoutedProcessRunner(base, issue_sink)
    wrapped.run(["git", "status"], cwd=tmp_path)
    assert base.output_sinks[-1] is issue_sink


def test_output_routed_runner_respects_explicit_sink(tmp_path: Path) -> None:
    """An explicit output_sink overrides the wrapper's default."""
    base = FakeProcessRunner()
    wrapped = _OutputRoutedProcessRunner(base, lambda _c: None)

    def explicit_sink(_chunk: str) -> None:
        return None

    wrapped.run(["git", "status"], cwd=tmp_path, output_sink=explicit_sink)
    assert base.output_sinks[-1] is explicit_sink


# --- issue_output_routing ---------------------------------------------------


def test_issue_output_routing_writes_file_and_view(tmp_path: Path) -> None:
    """The sink writes each chunk to the per-Issue file and the live view."""
    view = _RecordingView()
    with issue_output_routing(
        repo_id="repo", issue_number=7, log_base=tmp_path, output_view=view
    ) as sink:
        sink("agent says hi\n")

    log_files = list(
        (tmp_path / "agent-runner" / "issues" / "repo").glob("issue-7-*.log")
    )
    assert len(log_files) == 1
    assert "agent says hi" in log_files[0].read_text(encoding="utf-8")
    assert (7, "agent says hi\n") in view.appended


def test_issue_output_routing_captures_worker_thread_logs(tmp_path: Path) -> None:
    """backend.* log records from the worker thread land in the Issue's file."""
    logger = logging.getLogger("backend.test_parallel_routing")
    logger.setLevel(logging.INFO)
    with issue_output_routing(
        repo_id="r", issue_number=9, log_base=tmp_path, output_view=NoOpRunnerLiveView()
    ):
        logger.info("worker-thread-line-xyz")

    log_file = next((tmp_path / "agent-runner" / "issues" / "r").glob("issue-9-*.log"))
    assert "worker-thread-line-xyz" in log_file.read_text(encoding="utf-8")


def test_per_issue_log_path_layout(tmp_path: Path) -> None:
    """The per-Issue log path follows the agreed layout."""
    path = per_issue_log_path(tmp_path, "my-repo", 42)
    assert path.parent == tmp_path / "agent-runner" / "issues" / "my-repo"
    assert path.name.startswith("issue-42-")
    assert path.suffix == ".log"


# --- runner live views ------------------------------------------------------


def test_create_runner_live_view_non_tty_returns_plain() -> None:
    """Outside an interactive TTY (pytest), the factory returns the plain view."""
    assert isinstance(create_runner_live_view(), PlainRunnerLiveView)
    assert isinstance(create_runner_live_view(plain=True), PlainRunnerLiveView)


def test_noop_runner_live_view_is_inert() -> None:
    """The no-op view accepts every call without error."""
    view = NoOpRunnerLiveView()
    view.register_issue(1, "claude")
    view.append(1, "x")
    view.update_status(1, "completed")
    view.log("pass-level")
    view.close()


def test_plain_runner_live_view_prefixes_output(capsys) -> None:
    """The plain view prefixes each line with its Issue number."""
    view = PlainRunnerLiveView()
    view.register_issue(5, "claude")
    view.append(5, "line one\n")
    view.update_status(5, "completed")
    view.close()
    out = capsys.readouterr().out
    assert "[issue #5" in out
    assert "line one" in out
    assert "status=completed" in out
