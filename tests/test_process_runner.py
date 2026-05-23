"""Tests for subprocess runner output handling."""

from __future__ import annotations

import json

from backend.infrastructure.process_runner import (
    ClaudeStreamRenderer,
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
