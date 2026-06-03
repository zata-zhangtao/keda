"""Subprocess runner implementation."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from backend.infrastructure.logging.logger import logger

_MAX_BUFFER_SIZE = 4096


def _format_timestamped_line(text: str) -> str:
    """Prefix each line with HH:MM:SS timestamp.

    Args:
        text: The text to prefix with timestamps.

    Returns:
        Text with each line prefixed by [HH:MM:SS].
    """
    ts = datetime.now().strftime("%H:%M:%S")
    lines = text.split("\n")
    result: list[str] = []
    for idx, line in enumerate(lines):
        prefix = f"[{ts}] " if line else ""
        if idx == len(lines) - 1:
            result.append(f"{prefix}{line}")
        else:
            result.append(f"{prefix}{line}\n")
    return "".join(result)


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
        elif should_filter_claude_stream(command):
            completed = run_filtered_claude_stream(
                command, cwd=cwd, timeout=timeout, collect_stdout=True
            )
            stdout = completed.stdout
            stderr = ""
        else:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            try:
                if process.stdout is not None:
                    for line in process.stdout:
                        timestamped = _format_timestamped_line(line)
                        print(timestamped, end="", flush=True)
                        logger.info("%s", line.rstrip("\n"))
                        stdout_lines.append(line)
                if process.stderr is not None:
                    for line in process.stderr:
                        timestamped = _format_timestamped_line(line)
                        print(timestamped, end="", file=sys.stderr, flush=True)
                        logger.warning("%s", line.rstrip("\n"))
                        stderr_lines.append(line)
                return_code = process.wait(timeout=timeout)
            except Exception:
                process.kill()
                process.wait()
                raise
            stdout = "".join(stdout_lines)
            stderr = "".join(stderr_lines)
            completed = subprocess.CompletedProcess(
                args=list(command),
                returncode=return_code,
                stdout=stdout,
                stderr=stderr,
            )
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


class ClaudeStreamRenderer:
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


def should_filter_claude_stream(command: Sequence[str]) -> bool:
    """Return whether this command is Claude stream-json output."""
    command_parts = list(command)
    return (
        bool(command_parts)
        and command_parts[0] == "claude"
        and "--output-format" in command_parts
        and "stream-json" in command_parts
    )


def run_filtered_claude_stream(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int | None,
    collect_stdout: bool = False,
    prompt_text: str | None = None,
    output_sink: Callable[[str], None] | None = None,
    display_sink: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Claude stream-json and print a filtered live view.

    Args:
        command: Command to run.
        cwd: Working directory.
        timeout: Optional timeout in seconds.
        collect_stdout: Whether to collect rendered output.
        prompt_text: Optional prompt to pass via stdin.
        output_sink: Optional callback for rendered text chunks.
        display_sink: Optional callback for stderr lines (display only).
            When provided, stderr is drained on a background thread and
            routed here instead of leaking raw onto the terminal.

    Returns:
        CompletedProcess with collected stdout if requested.
    """
    import threading

    renderer = ClaudeStreamRenderer()
    capture_stderr = display_sink is not None
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if capture_stderr else None,
        stdin=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )

    def _pump_stderr() -> None:
        if process.stderr is None:
            return
        for stderr_line in process.stderr:
            display_sink(stderr_line.rstrip("\n"))

    stderr_thread: threading.Thread | None = None
    if capture_stderr:
        stderr_thread = threading.Thread(target=_pump_stderr, daemon=True)
        stderr_thread.start()
    if prompt_text is not None:
        # Write stdin in a background thread to avoid deadlock
        # when the pipe buffer fills up before the child reads.
        def _write_stdin() -> None:
            if process.stdin is not None:
                process.stdin.write(prompt_text)
                process.stdin.close()

        threading.Thread(target=_write_stdin, daemon=True).start()
    else:
        process.stdin.close()
    stdout_lines: list[str] = []
    text_buffer: list[str] = []
    try:
        if process.stdout is not None:
            for output_line in process.stdout:
                rendered_text = renderer.render_line(output_line)
                if collect_stdout and rendered_text:
                    stdout_lines.append(rendered_text)
                if rendered_text:
                    if output_sink is not None:
                        # The sink drives the live view and the workspace file;
                        # skip stdout/logger writes that would corrupt the
                        # live region.
                        output_sink(rendered_text.rstrip("\n"))
                        continue
                    timestamped = _format_timestamped_line(rendered_text)
                    print(timestamped, end="", flush=True)

                    # Structured events go straight to logger
                    if (
                        "[agent tool]" in rendered_text
                        or "[agent result]" in rendered_text
                        or "[agent error]" in rendered_text
                    ):
                        logger.info("%s", rendered_text.strip())
                    else:
                        text_buffer.append(rendered_text)
                        buffered_text = "".join(text_buffer)
                        if (
                            rendered_text.endswith("\n")
                            or len(buffered_text) >= _MAX_BUFFER_SIZE
                        ):
                            stripped = buffered_text.strip()
                            if stripped:
                                logger.info("Agent output: %s", stripped)
                            text_buffer.clear()
        if text_buffer:
            buffered = "".join(text_buffer).strip()
            if buffered:
                logger.info("Agent output: %s", buffered)
        return_code = process.wait(timeout=timeout)
    except Exception:
        process.kill()
        process.wait()
        raise
    if stderr_thread is not None:
        stderr_thread.join(timeout=5)
    return subprocess.CompletedProcess(
        args=list(command),
        returncode=return_code,
        stdout="".join(stdout_lines),
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
