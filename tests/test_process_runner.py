"""Tests for subprocess runner output handling."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.infrastructure.process_runner import (
    ClaudeStreamRenderer,
    CommandFailedError,
    SubprocessRunner,
    _TimestampedStreamFormatter,
    _format_timestamped_line,
    should_filter_claude_stream,
)


def _json_line(payload: dict) -> str:
    return json.dumps(payload) + "\n"


def test_should_filter_claude_stream_only_for_stream_json() -> None:
    """Claude stream-json commands should use the concise renderer."""
    assert should_filter_claude_stream(["claude", "-p", "--output-format", "stream-json", "prompt"])
    assert not should_filter_claude_stream(["claude", "-p", "prompt"])
    assert not should_filter_claude_stream(["codex", "exec", "prompt"])


def test_claude_stream_renderer_suppresses_noisy_events() -> None:
    """Renderer should avoid dumping thinking, signatures, and tool results."""
    renderer = ClaudeStreamRenderer()

    thinking_line = _json_line(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "hidden"},
            },
        }
    )
    signature_line = _json_line(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "signature_delta", "signature": "secret"},
            },
        }
    )
    tool_result_line = _json_line(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": "large file content",
                    }
                ]
            },
        }
    )

    assert renderer.render_line(thinking_line) == ""
    assert renderer.render_line(signature_line) == ""
    assert renderer.render_line(tool_result_line) == ""


def test_claude_stream_renderer_prints_concise_progress() -> None:
    """Renderer should keep useful tool, text, and error progress."""
    renderer = ClaudeStreamRenderer()
    tool_line = _json_line(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/example.py", "limit": 20},
                    }
                ]
            },
        }
    )
    text_line = _json_line(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "done"},
            },
        }
    )
    stop_line = _json_line({"type": "stream_event", "event": {"type": "message_stop"}})
    error_line = _json_line({"type": "result", "is_error": True, "result": "API Error: 400"})

    assert renderer.render_line(tool_line) == ("\n[agent tool] Read: /tmp/example.py limit=20\n")
    assert renderer.render_line(tool_line) == ""
    assert renderer.render_line(text_line) == "done"
    assert renderer.render_line(stop_line) == "\n"
    assert renderer.render_line(error_line) == "\n[agent error] API Error: 400\n"


def test_claude_stream_renderer_output_is_transcript_safe() -> None:
    """Collected Claude output should not include raw stream-json noise."""
    renderer = ClaudeStreamRenderer()
    lines = [
        _json_line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "hidden"},
                },
            }
        ),
        _json_line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "visible"},
                },
            }
        ),
        _json_line({"type": "stream_event", "event": {"type": "message_stop"}}),
    ]

    transcript = "".join(rendered for line in lines if (rendered := renderer.render_line(line)))

    assert transcript == "visible\n"


def test_transcript_runner_builds_claude_command() -> None:
    """Transcript runner should build claude deliberation command."""
    from pathlib import Path

    from backend.engines.agent_runner.transcript_runner import (
        _build_deliberation_command,
    )

    cmd = _build_deliberation_command("claude", "hello", Path("/tmp"))
    assert cmd[0] == "claude"
    assert "--dangerously-skip-permissions" in cmd
    assert "hello" in cmd


def test_transcript_runner_builds_kimi_command() -> None:
    """Transcript runner should build kimi deliberation command."""
    from pathlib import Path

    from backend.engines.agent_runner.transcript_runner import (
        _build_deliberation_command,
    )

    cmd = _build_deliberation_command("kimi", "hello", Path("/tmp"))
    assert cmd == ["kimi", "--input-format", "text"]
    assert "--quiet" not in cmd


def test_transcript_runner_builds_codex_command() -> None:
    """Transcript runner should build codex deliberation command."""
    from pathlib import Path

    from backend.engines.agent_runner.transcript_runner import (
        _build_deliberation_command,
    )

    cmd = _build_deliberation_command("codex", "hello", Path("relative/workspace"))
    assert cmd[0] == "codex"
    assert "--cd" in cmd
    assert "read-only" in cmd
    assert "hello" not in cmd
    assert Path(cmd[cmd.index("--cd") + 1]).is_absolute()


def test_format_timestamped_line_adds_timestamp_prefix() -> None:
    """_format_timestamped_line should add [HH:MM:SS] prefix to each line."""
    result = _format_timestamped_line("test output\n")
    assert result.startswith("[")
    assert "] " in result
    assert "test output" in result


def test_format_timestamped_line_handles_leading_newline() -> None:
    """_format_timestamped_line should handle leading newlines correctly."""
    result = _format_timestamped_line("\n[agent tool] Read\n")
    # Should have timestamp on the second line (after the empty line)
    assert result.startswith("\n[")
    assert "[agent tool] Read" in result


def test_format_timestamped_line_empty_string() -> None:
    """_format_timestamped_line should handle empty string."""
    result = _format_timestamped_line("")
    assert result == ""


def test_timestamped_stream_formatter_keeps_chunks_on_same_line() -> None:
    """Streaming chunks should not receive timestamps inside one physical line."""
    formatter = _TimestampedStreamFormatter()

    first_line = "".join(
        formatter.format_chunk(chunk) for chunk in ("{", '"action"', ": true", "\n")
    )
    second_line = formatter.format_chunk('"next"')

    assert first_line.count("[") == 1
    assert first_line.endswith('{"action": true\n')
    assert second_line.count("[") == 1
    assert second_line.endswith('"next"')


def test_run_filtered_claude_stream_logs_structured_events(tmp_path: Path) -> None:
    """run_filtered_claude_stream should log tool/result/error events."""
    from backend.infrastructure.process_runner import run_filtered_claude_stream

    # Create mock process that yields structured events
    tool_event = _json_line(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.py"},
                    }
                ]
            },
        }
    )
    result_event = _json_line({"type": "result", "is_error": False, "result": "done"})

    mock_process = MagicMock()
    mock_process.stdout = iter([tool_event, result_event])
    mock_process.wait.return_value = 0
    mock_process.stdin = MagicMock()

    log_records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = lambda record: log_records.append(record)

    logger = logging.getLogger("app")
    logger.addHandler(handler)
    original_level = logger.level
    logger.setLevel(logging.INFO)

    try:
        with patch("subprocess.Popen", return_value=mock_process):
            run_filtered_claude_stream(
                ["claude", "--output-format", "stream-json"],
                cwd=tmp_path,
                timeout=None,
                collect_stdout=True,
            )

        # Check that structured events were logged
        logged_messages = [r.getMessage() for r in log_records]
        assert any("[agent tool]" in msg for msg in logged_messages)
        assert any("[agent result]" in msg for msg in logged_messages)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def test_run_filtered_claude_stream_stdout_timestamp_does_not_split_text_delta(
    tmp_path: Path,
    capsys,
) -> None:
    """Live Claude output should prefix lines, not every text_delta chunk."""
    from backend.infrastructure.process_runner import run_filtered_claude_stream

    text_events = [
        _json_line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text_chunk},
                },
            }
        )
        for text_chunk in ("{", '"action"', ": true", "\n")
    ]
    stop_event = _json_line({"type": "stream_event", "event": {"type": "message_stop"}})

    mock_process = MagicMock()
    mock_process.stdout = iter([*text_events, stop_event])
    mock_process.wait.return_value = 0
    mock_process.stdin = MagicMock()

    with patch("subprocess.Popen", return_value=mock_process):
        completed_process = run_filtered_claude_stream(
            ["claude", "--output-format", "stream-json"],
            cwd=tmp_path,
            timeout=None,
            collect_stdout=True,
        )

    captured_output = capsys.readouterr().out
    assert captured_output.count("[") == 1
    assert captured_output.endswith('{"action": true\n\n')
    assert completed_process.stdout == '{"action": true\n\n'


def test_run_filtered_claude_stream_buffers_text_delta(tmp_path: Path) -> None:
    """run_filtered_claude_stream should buffer text_delta and log on message_stop."""
    from backend.infrastructure.process_runner import run_filtered_claude_stream

    text_event = _json_line(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello "},
            },
        }
    )
    text_event2 = _json_line(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "world"},
            },
        }
    )
    stop_event = _json_line({"type": "stream_event", "event": {"type": "message_stop"}})

    mock_process = MagicMock()
    mock_process.stdout = iter([text_event, text_event2, stop_event])
    mock_process.wait.return_value = 0
    mock_process.stdin = MagicMock()

    log_records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = lambda record: log_records.append(record)

    logger = logging.getLogger("app")
    logger.addHandler(handler)
    original_level = logger.level
    logger.setLevel(logging.INFO)

    try:
        with patch("subprocess.Popen", return_value=mock_process):
            run_filtered_claude_stream(
                ["claude", "--output-format", "stream-json"],
                cwd=tmp_path,
                timeout=None,
                collect_stdout=True,
            )

        # Check that text was logged after message_stop
        logged_messages = [r.getMessage() for r in log_records]
        assert any("hello world" in msg for msg in logged_messages)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def test_run_filtered_claude_stream_output_sink_preserves_newlines(
    tmp_path: Path,
) -> None:
    """Rendered Claude newlines should reach the live output sink."""
    from backend.infrastructure.process_runner import run_filtered_claude_stream

    text_event = _json_line(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            },
        }
    )
    stop_event = _json_line({"type": "stream_event", "event": {"type": "message_stop"}})

    mock_process = MagicMock()
    mock_process.stdout = iter([text_event, stop_event])
    mock_process.wait.return_value = 0
    mock_process.stdin = MagicMock()
    streamed_output_chunks: list[str] = []

    with patch("subprocess.Popen", return_value=mock_process):
        completed_process = run_filtered_claude_stream(
            ["claude", "--output-format", "stream-json"],
            cwd=tmp_path,
            timeout=None,
            collect_stdout=True,
            output_sink=streamed_output_chunks.append,
        )

    assert streamed_output_chunks == ["hello", "\n"]
    assert completed_process.stdout == "hello\n"


def test_relay_process_stdout_output_sink_preserves_line_boundaries() -> None:
    """Non-Claude transcript streaming should keep stdout line endings."""
    from backend.engines.agent_runner.transcript_runner import _relay_process_stdout

    mock_process = MagicMock()
    mock_process.stdout = iter(["first\n", "second\n"])
    mock_process.stderr = iter([])
    mock_process.wait.return_value = 0
    streamed_output_chunks: list[str] = []

    return_code, stdout_text = _relay_process_stdout(
        mock_process,
        output_sink=streamed_output_chunks.append,
    )

    assert return_code == 0
    assert stdout_text == "first\nsecond\n"
    assert streamed_output_chunks == ["first\n", "second\n"]


def test_subprocess_runner_non_claude_path_streams_via_pty(tmp_path: Path) -> None:
    """Non-Claude streaming runs under a PTY: output is collected and logged.

    The PTY makes the child line-buffer (a pipe would block-buffer and hide
    progress), so a real subprocess is used here rather than mocking Popen.
    """
    import sys

    from backend.infrastructure.process_runner import SubprocessRunner

    runner = SubprocessRunner()

    log_records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = lambda record: log_records.append(record)

    logger = logging.getLogger("app")
    logger.addHandler(handler)
    original_level = logger.level
    logger.setLevel(logging.INFO)

    try:
        result = runner.run(
            [sys.executable, "-c", "print('output line 1'); print('output line 2')"],
            cwd=tmp_path,
            capture_output=False,
            check=False,
        )

        # Output is collected from the PTY master.
        assert "output line 1" in result.stdout
        assert "output line 2" in result.stdout

        # And mirrored to the logger (line-buffered) when there is no sink.
        logged_messages = [r.getMessage() for r in log_records]
        assert any("output line 1" in msg for msg in logged_messages)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def test_subprocess_runner_pty_routes_output_to_sink(tmp_path: Path) -> None:
    """When a sink is provided, PTY chunks go to the sink (for per-Issue panels)."""
    import sys

    from backend.infrastructure.process_runner import SubprocessRunner

    chunks: list[str] = []
    SubprocessRunner().run(
        [sys.executable, "-c", "print('via-sink')"],
        cwd=tmp_path,
        capture_output=False,
        check=False,
        output_sink=chunks.append,
    )
    assert any("via-sink" in chunk for chunk in chunks)


def test_subprocess_runner_claude_capture_uses_filtered_stream(
    tmp_path: Path,
) -> None:
    """Claude capture mode should return rendered output instead of raw JSON."""
    from backend.infrastructure.process_runner import SubprocessRunner

    text_event = _json_line(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "approved"},
            },
        }
    )
    stop_event = _json_line({"type": "stream_event", "event": {"type": "message_stop"}})
    mock_process = MagicMock()
    mock_process.stdout = iter([text_event, stop_event])
    mock_process.stdin = MagicMock()
    mock_process.wait.return_value = 0
    mock_process.returncode = 0
    mock_process.poll.return_value = None

    runner = SubprocessRunner()
    with (
        patch("subprocess.Popen", return_value=mock_process) as popen_mock,
        patch("subprocess.run") as run_mock,
    ):
        result = runner.run(
            ["claude", "--output-format", "stream-json", "-p", "Review."],
            cwd=tmp_path,
            capture_output=True,
            timeout=900,
        )

    assert result.stdout == "approved\n"
    assert result.stderr == ""
    assert popen_mock.called
    assert not run_mock.called


def test_subprocess_runner_captured_timeout_path_uses_popen(
    tmp_path: Path,
) -> None:
    """Captured non-Claude commands with timeout should use watchdog Popen path."""
    from backend.infrastructure.process_runner import SubprocessRunner

    mock_process = MagicMock()
    mock_process.communicate.return_value = ("raw output\n", "raw error\n")
    mock_process.returncode = 0
    mock_process.poll.return_value = None

    runner = SubprocessRunner()
    with (
        patch("subprocess.Popen", return_value=mock_process) as popen_mock,
        patch("subprocess.run") as run_mock,
    ):
        result = runner.run(
            ["some", "command"],
            cwd=tmp_path,
            capture_output=True,
            check=False,
            timeout=900,
        )

    assert result.stdout == "raw output\n"
    assert result.stderr == "raw error\n"
    assert popen_mock.called
    assert not run_mock.called


def test_process_watchdog_logs_heartbeat_and_kills_on_timeout() -> None:
    """Process watchdog should log progress and terminate after timeout."""
    from backend.infrastructure import process_runner

    class _NeverStoppedEvent:
        def wait(self, timeout: float) -> bool:  # noqa: ARG002
            return False

    mock_process = MagicMock()
    mock_process.poll.return_value = None
    watchdog = process_runner._ProcessWatchdog(
        mock_process,
        ["slow", "command"],
        timeout=2,
        heartbeat_seconds=1,
        base_label="Command",
    )
    watchdog._started_at = 0
    watchdog._stop_event = _NeverStoppedEvent()

    with (
        patch.object(process_runner.time, "monotonic", side_effect=[1.1, 2.1]),
        patch.object(process_runner, "logger") as logger_mock,
    ):
        watchdog._run()

    logger_mock.info.assert_any_call(
        "%s still running after %ds: %s",
        "Command",
        1,
        "slow command",
    )
    logger_mock.error.assert_called_once_with(
        "%s timed out after %ds; terminating: %s",
        "Command",
        2,
        "slow command",
    )
    mock_process.kill.assert_called_once_with()


def test_process_watchdog_includes_context_label_in_logs() -> None:
    """Process watchdog should include the context label in heartbeat logs."""
    from backend.infrastructure import process_runner

    class _NeverStoppedEvent:
        def wait(self, timeout: float) -> bool:  # noqa: ARG002
            return False

    mock_process = MagicMock()
    mock_process.poll.return_value = None
    watchdog = process_runner._ProcessWatchdog(
        mock_process,
        ["slow", "command"],
        timeout=2,
        heartbeat_seconds=1,
        base_label="Claude stream",
        context_label="Issue #23: https://github.com/example/repo/issues/23",
    )
    watchdog._started_at = 0
    watchdog._stop_event = _NeverStoppedEvent()

    with (
        patch.object(process_runner.time, "monotonic", side_effect=[1.1, 2.1]),
        patch.object(process_runner, "logger") as logger_mock,
    ):
        watchdog._run()

    logger_mock.info.assert_any_call(
        "%s still running after %ds: %s",
        "Claude stream (Issue #23: https://github.com/example/repo/issues/23)",
        1,
        "slow command",
    )
    logger_mock.error.assert_called_once_with(
        "%s timed out after %ds; terminating: %s",
        "Claude stream (Issue #23: https://github.com/example/repo/issues/23)",
        2,
        "slow command",
    )


def test_process_watchdog_kills_on_inactivity_timeout() -> None:
    """Watchdog should kill a process that produces no output."""
    from backend.infrastructure import process_runner

    class _NeverStoppedEvent:
        def wait(self, timeout: float) -> bool:  # noqa: ARG002
            return False

    mock_process = MagicMock()
    mock_process.poll.return_value = None
    watchdog = process_runner._ProcessWatchdog(
        mock_process,
        ["slow", "command"],
        timeout=None,
        inactivity_timeout_seconds=2,
        heartbeat_seconds=10,
        base_label="Command",
    )
    watchdog._started_at = 0
    watchdog._last_output_at = 0
    watchdog._stop_event = _NeverStoppedEvent()

    with patch.object(process_runner.time, "monotonic", side_effect=[3.0, 3.0]):
        watchdog._run()

    mock_process.kill.assert_called_once_with()


def test_process_watchdog_does_not_kill_on_inactivity_when_output_is_active() -> None:
    """Watchdog should not kill a process that keeps producing output."""
    from backend.infrastructure import process_runner

    mock_process = MagicMock()
    mock_process.poll.return_value = None
    watchdog = process_runner._ProcessWatchdog(
        mock_process,
        ["slow", "command"],
        timeout=None,
        inactivity_timeout_seconds=2,
        heartbeat_seconds=10,
        base_label="Command",
    )
    watchdog._started_at = 0
    watchdog._last_output_at = 0
    stop_event = MagicMock()
    stop_event.wait.side_effect = [False, True]
    watchdog._stop_event = stop_event

    with patch.object(process_runner.time, "monotonic", side_effect=[1.5, 1.5, 1.5]):
        watchdog.note_output()
        watchdog._run()

    mock_process.kill.assert_not_called()


def test_subprocess_runner_kills_silent_process_on_inactivity_timeout(
    tmp_path: Path,
) -> None:
    """A silent process should be killed by inactivity timeout."""
    from backend.infrastructure.process_runner import SubprocessRunner

    runner = SubprocessRunner()

    with pytest.raises(subprocess.TimeoutExpired):
        runner.run(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            capture_output=True,
            timeout=3600,
            inactivity_timeout=1,
        )


def test_subprocess_runner_keeps_active_process_alive(
    tmp_path: Path,
) -> None:
    """A process that keeps printing should not be killed by inactivity timeout."""
    from backend.infrastructure.process_runner import SubprocessRunner

    runner = SubprocessRunner()
    script = "import time\n" "for _ in range(5):\n" "    print('tick')\n" "    time.sleep(0.1)\n"

    result = runner.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
        inactivity_timeout=1,
    )

    assert result.return_code == 0
    assert result.stdout.count("tick") == 5


def test_subprocess_runner_replaces_invalid_utf8_in_captured_output(
    tmp_path: Path,
) -> None:
    """Binary bytes in stdout (e.g. PDF content in git diff) must not crash decoding."""
    import sys

    from backend.infrastructure.process_runner import SubprocessRunner

    emit_binary_stdout = (
        "import sys; sys.stdout.buffer.write(b'diff --git a/source.pdf\\n\\x78\\xda\\xff\\n')"
    )
    runner = SubprocessRunner()

    captured_result = runner.run(
        [sys.executable, "-c", emit_binary_stdout],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert captured_result.return_code == 0
    assert "diff --git a/source.pdf" in captured_result.stdout
    assert "�" in captured_result.stdout

    watchdog_result = runner.run(
        [sys.executable, "-c", emit_binary_stdout],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert watchdog_result.return_code == 0
    assert "�" in watchdog_result.stdout


def test_capture_output_true_not_polluted(tmp_path: Path) -> None:
    """capture_output=True should return raw stdout without timestamp prefix."""
    from backend.infrastructure.process_runner import SubprocessRunner

    runner = SubprocessRunner()

    mock_completed = MagicMock()
    mock_completed.returncode = 0
    mock_completed.stdout = "raw output\n"
    mock_completed.stderr = ""

    with patch("subprocess.run", return_value=mock_completed):
        result = runner.run(
            ["some", "command"],
            cwd=tmp_path,
            capture_output=True,
            check=False,
        )

    # stdout should be raw, not timestamped
    assert result.stdout == "raw output\n"
    assert "[HH:MM:SS]" not in result.stdout


def test_command_failed_error_includes_stderr(tmp_path: Path) -> None:
    """stderr must appear in the string representation of the raised exception."""
    runner = SubprocessRunner()

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        runner.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('failure detail'); sys.exit(1)",
            ],
            cwd=tmp_path,
            capture_output=True,
        )

    assert isinstance(exc_info.value, subprocess.CalledProcessError)
    message = str(exc_info.value)
    assert "failure detail" in message
    assert "--- stderr/stdout ---" in message


def test_command_failed_error_falls_back_to_stdout(tmp_path: Path) -> None:
    """stdout must be used when stderr is empty."""
    runner = SubprocessRunner()

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        runner.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('stdout failure'); sys.exit(1)",
            ],
            cwd=tmp_path,
            capture_output=True,
        )

    message = str(exc_info.value)
    assert "stdout failure" in message
    assert "--- stderr/stdout ---" in message


def test_command_failed_error_truncates_long_output(tmp_path: Path) -> None:
    """Output longer than 4 KB must be truncated and annotated."""
    runner = SubprocessRunner()

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        runner.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('x' * 5000); sys.exit(1)",
            ],
            cwd=tmp_path,
            capture_output=True,
        )

    message = str(exc_info.value)
    assert "... (truncated)" in message
    assert len(message) < 5500


def test_command_failed_error_no_output(tmp_path: Path) -> None:
    """The exception message must keep command and return code when no output exists."""
    runner = SubprocessRunner()

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        runner.run(
            [sys.executable, "-c", "import sys; sys.exit(2)"],
            cwd=tmp_path,
            capture_output=True,
        )

    message = str(exc_info.value)
    assert "--- stderr/stdout ---" not in message
    assert "2" in message


def test_command_failed_error_preserves_attributes() -> None:
    """CommandFailedError must expose the standard CalledProcessError attributes."""
    exc = CommandFailedError(1, ["cmd"], output="out", stderr="err")

    assert exc.returncode == 1
    assert exc.cmd == ["cmd"]
    assert exc.output == "out"
    assert exc.stderr == "err"
