"""Subprocess runner implementation."""

from __future__ import annotations

import codecs
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from backend.infrastructure.logging.logger import logger

try:
    import pty
except ImportError:  # pragma: no cover - pty is POSIX-only (absent on Windows).
    pty = None  # type: ignore[assignment]

# Streaming agents (kimi / codex) block-buffer stdout when it is a pipe, hiding
# their progress until exit. A pseudo-terminal makes them line-buffer again.
_PTY_AVAILABLE = pty is not None and hasattr(pty, "openpty")

_MAX_BUFFER_SIZE = 4096
_MAX_ERROR_DETAIL_LEN = 4096
_COMMAND_HEARTBEAT_SECONDS = 60

# 带超时的子进程都放进**自己的进程组**，超时时才能整组回收（见
# :func:`_terminate_process_tree`）。用 ``process_group=0``（setpgid）而不是
# ``start_new_session=True``（setsid）：只换进程组、保留控制终端，避免改变
# kimi/codex 这类依赖 tty 行为的 agent 的运行环境。
_OWN_PROCESS_GROUP_KWARGS: dict[str, Any] = {"process_group": 0} if hasattr(os, "setpgid") else {}


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    """终止超时子进程**及其整个进程组**，而不是只终止直接子进程。

    ``Popen.kill()`` 只向直接子进程发信号。对 ``bash -lc "script.sh | tee log"``
    这类命令，管道里的 ``tee`` 和脚本自己拉起的后台进程会活下来，并继续持有它们
    继承到的 stdout 写端；于是 :func:`_run_captured_process` 的 ``communicate()``
    永远读不到 EOF，超时后整个 runner 无限期阻塞——被 kill 的子进程连僵尸都没被
    回收，daemon 也不再轮询任何 Issue。

    子进程由 ``_OWN_PROCESS_GROUP_KWARGS`` 放进独立进程组（组 id 等于其 pid），
    向组发信号即可覆盖所有派生进程。若平台不支持而子进程仍留在 runner 自己的组
    里，则退回只杀直接子进程，避免把 runner 自己一起杀掉。

    Args:
        process: 需要终止的子进程句柄。
    """
    killable_group_id: int | None = None
    if hasattr(os, "killpg"):
        try:
            child_group_id = os.getpgid(process.pid)
        except OSError:  # 子进程已退出或已被回收。
            child_group_id = None
        if child_group_id is not None and child_group_id != os.getpgid(0):
            killable_group_id = child_group_id
    if killable_group_id is not None:
        try:
            os.killpg(killable_group_id, signal.SIGKILL)
            return
        except OSError:  # 组已消失，退回直接终止。
            pass
    try:
        process.kill()
    except OSError:  # 子进程已退出。
        pass


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


class _TimestampedStreamFormatter:
    """Prefix non-empty output lines while preserving streaming chunks."""

    def __init__(self) -> None:
        self._at_line_start = True

    def format_chunk(self, text: str) -> str:
        """Return ``text`` with timestamps only at physical line starts."""
        if not text:
            return ""
        result: list[str] = []
        for character in text:
            if self._at_line_start and character != "\n":
                result.append(f"[{datetime.now().strftime('%H:%M:%S')}] ")
                self._at_line_start = False
            result.append(character)
            if character == "\n":
                self._at_line_start = True
        return "".join(result)


@dataclass(frozen=True)
class CommandResult:
    """Captured subprocess result.

    ``duration_seconds`` 让调用方能把"这一步花了多久"记进 attempt 历史 /
    日志——没有它，卡住的到底是哪条命令只能靠翻日志时间戳倒推。字段与
    ``backend.core.shared.models.agent_runner.CommandResult`` 保持一致。
    """

    command: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float = 0.0


class CommandFailedError(subprocess.CalledProcessError):
    """CalledProcessError with captured stderr/stdout included in the message."""

    def __str__(self) -> str:
        base = super().__str__()
        detail = self.stderr or self.output or ""
        if not detail:
            return base
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        detail = detail.strip()
        if not detail:
            return base
        if len(detail) > _MAX_ERROR_DETAIL_LEN:
            detail = detail[:_MAX_ERROR_DETAIL_LEN] + "\n... (truncated)"
        return f"{base}\n\n--- stderr/stdout ---\n{detail}"


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
        inactivity_timeout: int | None = None,
        capture_output: bool = True,
        input_text: str | None = None,
        label: str | None = None,
        output_sink: Callable[[str], None] | None = None,
    ) -> CommandResult:
        """Run a subprocess and capture output.

        Args:
            command: Command and arguments to execute.
            cwd: Working directory for the subprocess.
            check: Raise CommandFailedError when return code is non-zero.
            timeout: Optional wall-clock timeout in seconds.
            inactivity_timeout: Optional timeout in seconds since the last
                stdout/stderr output. Useful for detecting hung agents that
                keep the process alive without producing data.
            capture_output: Capture stdout/stderr instead of streaming.
            input_text: Optional text to feed via stdin.
            label: Optional label for heartbeat/timeout logs.
            output_sink: Optional callback for streamed output chunks. When
                provided for a streaming command (Claude ``stream-json`` or any
                non-captured command), rendered text is routed to the sink
                instead of the shared stdout, so parallel Issue runs can keep
                each agent's output in its own panel/log without interleaving.
        """
        started_mono: float = time.monotonic()
        if input_text is not None:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                input=input_text,
            )
            stdout = completed.stdout
            stderr = completed.stderr
        elif should_filter_claude_stream(command):
            completed = run_filtered_claude_stream(
                command,
                cwd=cwd,
                timeout=timeout,
                inactivity_timeout=inactivity_timeout,
                collect_stdout=True,
                label=label,
                output_sink=output_sink,
            )
            stdout = completed.stdout
            stderr = completed.stderr
        elif capture_output and timeout is not None:
            completed = _run_captured_process(
                command,
                cwd=cwd,
                timeout=timeout,
                inactivity_timeout=inactivity_timeout,
                label=label,
            )
            stdout = completed.stdout
            stderr = completed.stderr
        elif capture_output:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            stdout = completed.stdout
            stderr = completed.stderr
        elif _PTY_AVAILABLE:
            # Stream a non-Claude command (kimi / codex) under a PTY so it
            # line-buffers and shows live progress instead of going silent.
            completed = _run_pty_stream(
                command,
                cwd=cwd,
                timeout=timeout,
                inactivity_timeout=inactivity_timeout,
                label=label,
                output_sink=output_sink,
            )
            stdout = completed.stdout
            stderr = completed.stderr
        else:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_OWN_PROCESS_GROUP_KWARGS,
            )
            watchdog = _ProcessWatchdog(
                process,
                command,
                timeout=timeout,
                inactivity_timeout_seconds=inactivity_timeout,
                heartbeat_seconds=_COMMAND_HEARTBEAT_SECONDS,
                base_label="Command",
                context_label=label,
            )
            watchdog.start()
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            try:
                if process.stdout is not None:
                    for line in process.stdout:
                        watchdog.note_output()
                        if output_sink is not None:
                            output_sink(line)
                        else:
                            timestamped = _format_timestamped_line(line)
                            print(timestamped, end="", flush=True)
                        logger.info("%s", line.rstrip("\n"))
                        stdout_lines.append(line)
                if process.stderr is not None:
                    for line in process.stderr:
                        watchdog.note_output()
                        if output_sink is not None:
                            output_sink(line)
                        else:
                            timestamped = _format_timestamped_line(line)
                            print(timestamped, end="", file=sys.stderr, flush=True)
                        logger.warning("%s", line.rstrip("\n"))
                        stderr_lines.append(line)
                return_code = process.wait(timeout=timeout)
                watchdog.raise_if_timed_out(
                    partial_stdout="".join(stdout_lines),
                    partial_stderr="".join(stderr_lines),
                )
            except BaseException:
                # BaseException 而不是 Exception：Ctrl-C（KeyboardInterrupt）也必须
                # 拆掉整个进程组，否则子进程会变成孤儿继续跑。
                _terminate_process_tree(process)
                process.wait()
                raise
            finally:
                watchdog.stop()
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
            duration_seconds=round(time.monotonic() - started_mono, 3),
        )
        if check and completed.returncode != 0:
            raise CommandFailedError(
                completed.returncode,
                list(command),
                output=stdout,
                stderr=stderr,
            )
        return result


def _run_captured_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    inactivity_timeout: int | None = None,
    label: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a captured subprocess with heartbeat and optional inactivity logging."""
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_OWN_PROCESS_GROUP_KWARGS,
    )
    watchdog = _ProcessWatchdog(
        process,
        command,
        timeout=timeout,
        inactivity_timeout_seconds=inactivity_timeout,
        heartbeat_seconds=_COMMAND_HEARTBEAT_SECONDS,
        base_label="Command",
        context_label=label,
    )
    watchdog.start()
    try:
        if inactivity_timeout is None:
            stdout, stderr = process.communicate()
        else:
            stdout, stderr = _communicate_with_activity_tracking(process, watchdog)
            process.wait()
        watchdog.raise_if_timed_out(partial_stdout=stdout, partial_stderr=stderr)
    except BaseException:
        _terminate_process_tree(process)
        process.wait()
        raise
    finally:
        watchdog.stop()
    return subprocess.CompletedProcess(
        args=list(command),
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _communicate_with_activity_tracking(
    process: subprocess.Popen[str],
    watchdog: "_ProcessWatchdog",
) -> tuple[str, str]:
    """Read stdout/stderr while resetting the inactivity timeout on each chunk."""
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _pump_stdout() -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            watchdog.note_output()
            stdout_lines.append(line)

    def _pump_stderr() -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            watchdog.note_output()
            stderr_lines.append(line)

    stdout_thread = threading.Thread(target=_pump_stdout, daemon=True)
    stderr_thread = threading.Thread(target=_pump_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    return "".join(stdout_lines), "".join(stderr_lines)


class _ProcessWatchdog:
    """Log long-running subprocess heartbeats and enforce timeouts.

    Supports both a wall-clock timeout and an inactivity (no-output)
    timeout. The inactivity timeout resets whenever the watched process
    produces stdout or stderr data.
    """

    def __init__(
        self,
        process: subprocess.Popen[str],
        command: Sequence[str],
        *,
        timeout: int | None,
        inactivity_timeout_seconds: int | None = None,
        heartbeat_seconds: int,
        base_label: str,
        context_label: str | None = None,
    ) -> None:
        self._process = process
        self._command = tuple(command)
        self._timeout = timeout
        self._inactivity_timeout = inactivity_timeout_seconds
        self._effective_timeout: int | None = timeout
        self._heartbeat_seconds = heartbeat_seconds
        self._base_label = base_label
        self._context_label = context_label
        self._started_at = time.monotonic()
        self._last_output_at = self._started_at
        self._output_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._timed_out = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        """Start the watchdog background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop the watchdog and wait briefly for it to exit."""
        self._stop_event.set()
        self._thread.join(timeout=1)

    def note_output(self) -> None:
        """Reset the inactivity timeout clock after observing output."""
        with self._output_lock:
            self._last_output_at = time.monotonic()

    def raise_if_timed_out(
        self,
        *,
        partial_stdout: str | None = None,
        partial_stderr: str | None = None,
    ) -> None:
        """Raise TimeoutExpired when the watchdog killed the process.

        杀进程之前读到的输出必须跟着异常一起往上走。捕获模式下调用方拿不到
        任何流式输出，如果这里把已收集的 stdout/stderr 丢掉，一次超时就等于
        "什么都没发生"——比如 verifier agent 跑了半小时被杀，操作者只能看到
        一条 "timed out"，看不到它当时判到哪一步。
        """
        if self._timed_out:
            raise subprocess.TimeoutExpired(
                cmd=list(self._command),
                timeout=self._effective_timeout,
                output=partial_stdout,
                stderr=partial_stderr,
            )

    def _format_label(self) -> str:
        """Return the log label, optionally appending the context label."""
        if self._context_label:
            return f"{self._base_label} ({self._context_label})"
        return self._base_label

    def _check_timeouts(self, elapsed_seconds: int) -> bool:
        """Return True if a timeout fired and the process was killed."""
        if self._timeout is not None and elapsed_seconds >= self._timeout:
            self._timed_out = True
            self._effective_timeout = self._timeout
            label = self._format_label()
            logger.error(
                "%s timed out after %ds; terminating: %s",
                label,
                elapsed_seconds,
                _summarize_command(self._command),
            )
            _terminate_process_tree(self._process)
            return True
        if self._inactivity_timeout is not None:
            with self._output_lock:
                inactive_seconds = int(time.monotonic() - self._last_output_at)
            if inactive_seconds >= self._inactivity_timeout:
                self._timed_out = True
                self._effective_timeout = self._inactivity_timeout
                label = self._format_label()
                logger.error(
                    "%s inactive for %ds; terminating: %s",
                    label,
                    inactive_seconds,
                    _summarize_command(self._command),
                )
                _terminate_process_tree(self._process)
                return True
        return False

    def _run(self) -> None:
        next_heartbeat_at = self._heartbeat_seconds
        while not self._stop_event.wait(timeout=1):
            if self._process.poll() is not None:
                return
            elapsed_seconds = int(time.monotonic() - self._started_at)
            if elapsed_seconds >= next_heartbeat_at:
                label = self._format_label()
                logger.info(
                    "%s still running after %ds: %s",
                    label,
                    elapsed_seconds,
                    _summarize_command(self._command),
                )
                next_heartbeat_at += self._heartbeat_seconds
            if self._check_timeouts(elapsed_seconds):
                return


def _summarize_command(command: Sequence[str]) -> str:
    """Return a compact command string safe for logs."""
    command_text = " ".join(str(part) for part in command)
    if len(command_text) <= 240:
        return command_text
    return f"{command_text[:237]}..."


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
    inactivity_timeout: int | None = None,
    collect_stdout: bool = False,
    prompt_text: str | None = None,
    output_sink: Callable[[str], None] | None = None,
    display_sink: Callable[[str], None] | None = None,
    label: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Claude stream-json and print a filtered live view.

    Args:
        command: Command to run.
        cwd: Working directory.
        timeout: Optional wall-clock timeout in seconds.
        inactivity_timeout: Optional timeout in seconds since the last
            stdout/stderr output.
        collect_stdout: Whether to collect rendered output.
        prompt_text: Optional prompt to pass via stdin.
        output_sink: Optional callback for rendered text chunks.
        display_sink: Optional callback for stderr lines (display only).
            When provided, stderr is drained on a background thread and
            routed here instead of leaking raw onto the terminal.

    Returns:
        CompletedProcess with collected stdout if requested.
    """
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
        errors="replace",
        bufsize=1,
        **_OWN_PROCESS_GROUP_KWARGS,
    )
    watchdog = _ProcessWatchdog(
        process,
        command,
        timeout=timeout,
        inactivity_timeout_seconds=inactivity_timeout,
        heartbeat_seconds=_COMMAND_HEARTBEAT_SECONDS,
        base_label="Claude stream",
        context_label=label,
    )
    watchdog.start()

    def _pump_stderr() -> None:
        if process.stderr is None:
            return
        for stderr_line in process.stderr:
            watchdog.note_output()
            display_sink(stderr_line)

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
    stream_formatter = _TimestampedStreamFormatter()
    try:
        if process.stdout is not None:
            for output_line in process.stdout:
                watchdog.note_output()
                rendered_text = renderer.render_line(output_line)
                if collect_stdout and rendered_text:
                    stdout_lines.append(rendered_text)
                if rendered_text:
                    if output_sink is not None:
                        # The sink drives the live view and the workspace file;
                        # skip stdout/logger writes that would corrupt the
                        # live region.
                        output_sink(rendered_text)
                        continue
                    timestamped = stream_formatter.format_chunk(rendered_text)
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
                        if rendered_text.endswith("\n") or len(buffered_text) >= _MAX_BUFFER_SIZE:
                            stripped = buffered_text.strip()
                            if stripped:
                                logger.info("Agent output: %s", stripped)
                            text_buffer.clear()
        if text_buffer:
            buffered = "".join(text_buffer).strip()
            if buffered:
                logger.info("Agent output: %s", buffered)
        return_code = process.wait(timeout=timeout)
        watchdog.raise_if_timed_out()
    except BaseException:
        _terminate_process_tree(process)
        process.wait()
        raise
    finally:
        watchdog.stop()
    if stderr_thread is not None:
        stderr_thread.join(timeout=5)
    return subprocess.CompletedProcess(
        args=list(command),
        returncode=return_code,
        stdout="".join(stdout_lines),
        stderr="",
    )


def _run_pty_stream(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int | None,
    inactivity_timeout: int | None,
    label: str | None,
    output_sink: Callable[[str], None] | None,
) -> subprocess.CompletedProcess[str]:
    """Run a streaming command under a pseudo-terminal so it line-buffers.

    Agents such as ``kimi`` / ``codex`` switch stdout to block buffering when it
    is a pipe, so their progress stays invisible until they exit. Allocating a
    PTY makes them believe stdout is a terminal, restoring live incremental
    output. stdout and stderr are merged onto the PTY (natural ordering, no
    second-pipe deadlock). Rendered chunks go to ``output_sink`` when provided,
    otherwise to this process's stdout with line-buffered logging — mirroring
    :func:`run_filtered_claude_stream`.

    Args:
        command: Command and arguments to execute.
        cwd: Working directory for the subprocess.
        timeout: Optional wall-clock timeout in seconds.
        inactivity_timeout: Optional no-output timeout in seconds.
        label: Optional label for heartbeat/timeout logs.
        output_sink: Optional callback for rendered text chunks.

    Returns:
        CompletedProcess with the collected stdout (stderr merged into it).
    """
    master_fd, slave_fd = pty.openpty()
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            **_OWN_PROCESS_GROUP_KWARGS,
        )
    finally:
        os.close(slave_fd)
    watchdog = _ProcessWatchdog(
        process,
        command,
        timeout=timeout,
        inactivity_timeout_seconds=inactivity_timeout,
        heartbeat_seconds=_COMMAND_HEARTBEAT_SECONDS,
        base_label="Command",
        context_label=label,
    )
    watchdog.start()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    stream_formatter = _TimestampedStreamFormatter()
    collected: list[str] = []
    line_buffer: list[str] = []

    def _flush_log_lines(*, final: bool = False) -> None:
        joined = "".join(line_buffer)
        line_buffer.clear()
        if not joined:
            return
        segments = joined.split("\n")
        remainder = segments.pop()
        for segment in segments:
            stripped = segment.rstrip("\r").strip()
            if stripped:
                logger.info("Agent output: %s", stripped)
        if final:
            stripped_remainder = remainder.rstrip("\r").strip()
            if stripped_remainder:
                logger.info("Agent output: %s", stripped_remainder)
        elif remainder:
            line_buffer.append(remainder)

    def _emit(text: str) -> None:
        if not text:
            return
        collected.append(text)
        if output_sink is not None:
            output_sink(text)
            return
        print(stream_formatter.format_chunk(text), end="", flush=True)
        line_buffer.append(text)
        if "\n" in text:
            _flush_log_lines()

    try:
        while True:
            try:
                ready, _, _ = select.select([master_fd], [], [], 1.0)
            except (OSError, ValueError):
                break
            if not ready:
                if process.poll() is not None:
                    break
                continue
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                break  # EIO once the child closes the slave end == EOF.
            if not data:
                break
            watchdog.note_output()
            _emit(decoder.decode(data))
        _emit(decoder.decode(b"", final=True))
        if output_sink is None:
            _flush_log_lines(final=True)
        return_code = process.wait(timeout=timeout)
        watchdog.raise_if_timed_out()
    except BaseException:
        _terminate_process_tree(process)
        process.wait()
        raise
    finally:
        watchdog.stop()
        try:
            os.close(master_fd)
        except OSError:
            pass
    return subprocess.CompletedProcess(
        args=list(command),
        returncode=return_code,
        stdout="".join(collected),
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
