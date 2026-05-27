"""Tests for subprocess runner output handling."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.infrastructure.process_runner import (
    ClaudeStreamRenderer,
    _format_timestamped_line,
    should_filter_claude_stream,
)


def _json_line(payload: dict) -> str:
    return json.dumps(payload) + "\n"


def test_should_filter_claude_stream_only_for_stream_json() -> None:
    """Claude stream-json commands should use the concise renderer."""
    assert should_filter_claude_stream(
        ["claude", "-p", "--output-format", "stream-json", "prompt"]
    )
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
    error_line = _json_line(
        {"type": "result", "is_error": True, "result": "API Error: 400"}
    )

    assert renderer.render_line(tool_line) == (
        "\n[agent tool] Read: /tmp/example.py limit=20\n"
    )
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

    transcript = "".join(
        rendered for line in lines if (rendered := renderer.render_line(line))
    )

    assert transcript == "visible\n"


def test_transcript_runner_builds_claude_command() -> None:
    """Transcript runner should build claude deliberation command."""
    from pathlib import Path

    from backend.engines.agent_runner.factory import _build_deliberation_command

    cmd = _build_deliberation_command("claude", "hello", Path("/tmp"))
    assert cmd[0] == "claude"
    assert "--dangerously-skip-permissions" in cmd
    assert "hello" in cmd


def test_transcript_runner_builds_kimi_command() -> None:
    """Transcript runner should build kimi deliberation command."""
    from pathlib import Path

    from backend.engines.agent_runner.factory import _build_deliberation_command

    cmd = _build_deliberation_command("kimi", "hello", Path("/tmp"))
    assert cmd == ["kimi", "--quiet", "--input-format", "text"]


def test_transcript_runner_builds_codex_command() -> None:
    """Transcript runner should build codex deliberation command."""
    from pathlib import Path

    from backend.engines.agent_runner.factory import _build_deliberation_command

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


def test_subprocess_runner_non_claude_path_uses_pipe(tmp_path: Path) -> None:
    """SubprocessRunner.run() should use PIPE for non-Claude path."""
    from backend.infrastructure.process_runner import SubprocessRunner

    runner = SubprocessRunner()

    mock_process = MagicMock()
    mock_process.stdout = iter(["output line 1\n", "output line 2\n"])
    mock_process.stderr = iter([])
    mock_process.wait.return_value = 0

    log_records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = lambda record: log_records.append(record)

    logger = logging.getLogger("app")
    logger.addHandler(handler)
    original_level = logger.level
    logger.setLevel(logging.INFO)

    try:
        with patch("subprocess.Popen", return_value=mock_process):
            result = runner.run(
                ["codex", "exec", "test"],
                cwd=tmp_path,
                capture_output=False,
                check=False,
            )

        # Check that output was captured via PIPE
        assert "output line 1" in result.stdout
        assert "output line 2" in result.stdout

        # Check that output was logged
        logged_messages = [r.getMessage() for r in log_records]
        assert any("output line 1" in msg for msg in logged_messages)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


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
