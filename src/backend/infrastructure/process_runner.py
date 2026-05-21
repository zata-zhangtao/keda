"""Subprocess runner implementation."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class CommandResult:
    """Captured subprocess result."""

    command: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str


class SubprocessRunner:
    """Run commands using the subprocess module.

    Implements the ``IProcessRunner`` interface from
    ``backend.core.shared.interfaces.agent_runner`` via duck typing.
    """

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout: int | None = None,
        capture_output: bool = True,
    ) -> CommandResult:
        """Run a subprocess and capture output."""
        if capture_output:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
            stdout = completed.stdout
            stderr = completed.stderr
        elif _should_filter_claude_stream(command):
            completed = _run_filtered_claude_stream(command, cwd=cwd, timeout=timeout)
            stdout = ""
            stderr = ""
        else:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                check=False,
                stdout=None,
                stderr=None,
                encoding="utf-8",
                timeout=timeout,
            )
            stdout = ""
            stderr = ""
        result = CommandResult(
            command=tuple(command),
            return_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if check and completed.returncode != 0:
            raise subprocess.CalledProcessError(
                completed.returncode,
                list(command),
                output=stdout,
                stderr=stderr,
            )
        return result


class _ClaudeStreamRenderer:
    """Render Claude stream-json output into concise terminal messages."""

    def __init__(self) -> None:
        self._tool_use_ids: set[str] = set()
        self._saw_text_delta = False
        self._printed_text_content = False

    def render_line(self, line: str) -> str:
        """Return display text for one stream-json line."""
        try:
            event_payload = json.loads(line)
        except json.JSONDecodeError:
            return line
        if not isinstance(event_payload, dict):
            return ""
        event_type = event_payload.get("type")
        if event_type == "stream_event":
            return self._render_stream_event(event_payload.get("event"))
        if event_type == "assistant":
            return self._render_assistant_message(event_payload.get("message"))
        if event_type == "result":
            return self._render_result(event_payload)
        return ""

    def _render_stream_event(self, event_payload: object) -> str:
        if not isinstance(event_payload, dict):
            return ""
        if event_payload.get("type") == "message_stop" and self._saw_text_delta:
            self._saw_text_delta = False
            return "\n"
        delta_payload = event_payload.get("delta")
        if not isinstance(delta_payload, dict):
            return ""
        if delta_payload.get("type") == "text_delta":
            self._saw_text_delta = True
            self._printed_text_content = True
            return str(delta_payload.get("text", ""))
        return ""

    def _render_assistant_message(self, message_payload: object) -> str:
        if not isinstance(message_payload, dict):
            return ""
        content_blocks = message_payload.get("content", [])
        if not isinstance(content_blocks, list):
            return ""
        rendered_blocks: list[str] = []
        for content_block in content_blocks:
            if not isinstance(content_block, dict):
                continue
            if content_block.get("type") != "tool_use":
                continue
            tool_use_id = str(content_block.get("id", ""))
            if tool_use_id in self._tool_use_ids:
                continue
            self._tool_use_ids.add(tool_use_id)
            rendered_blocks.append(_format_tool_use(content_block))
        return "".join(rendered_blocks)

    def _render_result(self, event_payload: dict[str, Any]) -> str:
        result_text = str(event_payload.get("result") or "").strip()
        is_error = bool(event_payload.get("is_error"))
        if not result_text or (not is_error and self._printed_text_content):
            return ""
        prefix = "[agent error] " if is_error else "[agent result] "
        return f"\n{prefix}{result_text}\n"


def _should_filter_claude_stream(command: Sequence[str]) -> bool:
    """Return whether this command is Claude stream-json output."""
    command_parts = list(command)
    return (
        bool(command_parts)
        and command_parts[0] == "claude"
        and "--output-format" in command_parts
        and "stream-json" in command_parts
    )


def _run_filtered_claude_stream(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int | None,
) -> subprocess.CompletedProcess[str]:
    """Run Claude stream-json and print a filtered live view."""
    renderer = _ClaudeStreamRenderer()
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        if process.stdout is not None:
            for output_line in process.stdout:
                rendered_text = renderer.render_line(output_line)
                if rendered_text:
                    print(rendered_text, end="", flush=True)
        return_code = process.wait(timeout=timeout)
    except Exception:
        process.kill()
        process.wait()
        raise
    return subprocess.CompletedProcess(
        args=list(command),
        returncode=return_code,
        stdout="",
        stderr="",
    )


def _format_tool_use(content_block: dict[str, Any]) -> str:
    """Format one tool call without dumping large JSON payloads."""
    tool_name = str(content_block.get("name") or "tool")
    input_payload = content_block.get("input")
    if not isinstance(input_payload, dict):
        return f"\n[agent tool] {tool_name}\n"
    detail_parts: list[str] = []
    for field_name in ("file_path", "path", "command"):
        field_value = input_payload.get(field_name)
        if field_value:
            detail_parts.append(str(field_value))
            break
    if "offset" in input_payload:
        detail_parts.append(f"offset={input_payload['offset']}")
    if "limit" in input_payload:
        detail_parts.append(f"limit={input_payload['limit']}")
    details = f": {' '.join(detail_parts)}" if detail_parts else ""
    return f"\n[agent tool] {tool_name}{details}\n"
