"""Transcript runner implementation for agent deliberation.

This module provides the subprocess-based transcript runner used to execute
agent commands during deliberation sessions and stream their output back to
the caller.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from backend.core.shared.models.agent_deliberation import DeliberationEvent
from backend.core.shared.models.agent_runner import CommandResult
from backend.infrastructure.logging.logger import logger
from backend.infrastructure.process_runner import (
    SubprocessRunner,
    _format_timestamped_line,
    run_filtered_claude_stream,
    should_filter_claude_stream,
)


class SubprocessTranscriptRunner:
    """Run agents and emit deliberation events.

    Implements ``IAgentTranscriptRunner`` via duck typing.
    """

    def __init__(self, process_runner: SubprocessRunner) -> None:
        self._process_runner = process_runner

    def run(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        event_sink: "Callable[[DeliberationEvent], None]",
        output_sink: "Callable[[str], None] | None" = None,
        display_sink: "Callable[[str], None] | None" = None,
    ) -> "CommandResult":
        """Run an agent and emit events.

        Streams agent stdout to the terminal in real time while
        collecting it for the deliberation transcript. When
        ``output_sink`` is provided, rendered text chunks are passed
        to it as they arrive. When ``display_sink`` is provided, the
        agent's stderr (its human-readable reasoning/tool log) is routed
        to it for live display only, without being collected into the
        transcript.
        """
        command = _build_deliberation_command(agent_name, prompt, cwd)
        _ = event_sink
        if should_filter_claude_stream(command):
            # Pass the prompt via stdin to avoid "Argument list too long"
            # when the transcript grows across rounds.
            command_no_prompt = [arg for arg in command if arg != "-p"]
            if command_no_prompt and command_no_prompt[-1] == prompt:
                command_no_prompt = command_no_prompt[:-1]
            completed = run_filtered_claude_stream(
                command_no_prompt,
                cwd=cwd,
                timeout=None,
                collect_stdout=True,
                prompt_text=prompt,
                output_sink=output_sink,
                display_sink=display_sink,
            )
            return CommandResult(
                command=tuple(command_no_prompt),
                return_code=completed.returncode,
                stdout=completed.stdout,
                stderr="",
            )
        if agent_name in ("kimi", "codex"):
            # Pass the prompt via stdin to avoid "Argument list too long"
            # when the transcript grows across rounds.
            return _run_agent_with_stdin_prompt(
                command, prompt, cwd, output_sink=output_sink, display_sink=display_sink
            )
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        return_code, stdout_text = _relay_process_stdout(
            process, output_sink=output_sink, display_sink=display_sink
        )
        return CommandResult(
            command=tuple(command),
            return_code=return_code,
            stdout=stdout_text,
            stderr="",
        )


def _run_agent_with_stdin_prompt(
    command: list[str],
    prompt: str,
    cwd: Path,
    output_sink: "Callable[[str], None] | None" = None,
    display_sink: "Callable[[str], None] | None" = None,
) -> CommandResult:
    """Run an agent subprocess, passing the prompt via stdin."""
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def _write_stdin() -> None:
        if process.stdin is not None:
            try:
                process.stdin.write(prompt)
            except BrokenPipeError:
                pass
            process.stdin.close()

    threading.Thread(target=_write_stdin, daemon=True).start()
    return_code, stdout_text = _relay_process_stdout(
        process, output_sink=output_sink, display_sink=display_sink
    )
    return CommandResult(
        command=tuple(command),
        return_code=return_code,
        stdout=stdout_text,
        stderr="",
    )


def _pump_stderr(
    process: subprocess.Popen[str],
    display_sink: "Callable[[str], None] | None",
) -> None:
    """Drain subprocess stderr, routing each line to the display sink.

    Agents such as ``codex`` write their human-readable reasoning/tool log
    to stderr. When a ``display_sink`` is present it shows those lines live
    without collecting them into the transcript. With no sink we preserve
    the prior behaviour of echoing stderr to the terminal.
    """
    if process.stderr is None:
        return
    for line in process.stderr:
        if display_sink is not None:
            display_sink(line)
        else:
            print(_format_timestamped_line(line), end="", file=sys.stderr)


def _relay_process_stdout(
    process: subprocess.Popen[str],
    output_sink: "Callable[[str], None] | None" = None,
    display_sink: "Callable[[str], None] | None" = None,
) -> tuple[int, str]:
    """Relay subprocess stdout to terminal and logger.

    Stderr is drained on a background thread so the agent's reasoning/tool
    log reaches ``display_sink`` (live view) without blocking stdout or
    leaking raw onto the terminal and corrupting the live region.
    """
    stderr_thread: threading.Thread | None = None
    if process.stderr is not None:
        stderr_thread = threading.Thread(
            target=_pump_stderr, args=(process, display_sink), daemon=True
        )
        stderr_thread.start()
    stdout_lines: list[str] = []
    try:
        if process.stdout is not None:
            for line in process.stdout:
                stdout_lines.append(line)
                if output_sink is not None:
                    # The sink drives the live view and the workspace file;
                    # avoid writing to stdout (would corrupt the live region).
                    output_sink(line)
                else:
                    logger.info("%s", line.rstrip("\n"))
                    timestamped = _format_timestamped_line(line)
                    print(timestamped, end="")
        return_code = process.wait(timeout=None)
    except Exception:
        process.kill()
        process.wait()
        raise
    if stderr_thread is not None:
        stderr_thread.join(timeout=5)
    return return_code, "".join(stdout_lines)


def _build_deliberation_command(agent_name: str, prompt: str, cwd: Path) -> list[str]:
    if agent_name == "claude":
        return [
            "claude",
            "--dangerously-skip-permissions",
            "--verbose",
            "-p",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            prompt,
        ]
    if agent_name == "kimi":
        return ["kimi", "--quiet", "--input-format", "text"]
    return [
        "codex",
        "--cd",
        str(cwd.resolve()),
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "exec",
    ]


def create_transcript_runner(
    process_runner: SubprocessRunner | None = None,
) -> SubprocessTranscriptRunner:
    """Create a transcript runner instance."""
    return SubprocessTranscriptRunner(process_runner or SubprocessRunner())
