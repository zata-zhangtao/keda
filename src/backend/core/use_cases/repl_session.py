"""REPL (Read-Eval-Print Loop) use case for the ``iar`` no-arg entrypoint.

The REPL drives a multi-turn conversation between the user and a configured
agent, parsing ``<<IAR_EXEC>> ... <<END_IAR_EXEC>>`` markers in the agent
reply to execute whitelisted IAR subcommands and feed the captured output
back into the conversation history.

Architecture notes:
- Business logic lives in core/; the agent command builder is reused from
  ``backend.engines.agent_runner.factory``.
- I/O ports (process runner, content generator, GitHub client, command
  executor) are injected via :class:`ReplSessionDeps`, so unit tests can
  substitute fake implementations without spawning real subprocesses.
- Marker text is concentrated in module-level constants so that future
  translations or protocol revisions touch a single location.
"""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.shared.interfaces.agent_runner import (
    IarExecRequest,
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
    IReplCommandExecutor,
    ReplExecOutcome,
)
from backend.core.shared.models.agent_decision import ReplConfig
from backend.core.shared.models.agent_runner import (
    CommandResult,
    RepositoryRunContext,
)
from backend.core.use_cases.interactive_decision import (
    _build_issue_summary,
    _build_pending_prd_summary,
    _truncate_prompt,
)

_logger = logging.getLogger(__name__)

IAR_EXEC_OPEN_MARKER = "<<IAR_EXEC>>"
IAR_EXEC_CLOSE_MARKER = "<<END_IAR_EXEC>>"
IAR_EXEC_RESULT_OPEN_TAG = "[IAR_EXEC_RESULT]"
IAR_EXEC_RESULT_CLOSE_TAG = "[/IAR_EXEC_RESULT]"

EXIT_COMMAND = "/exit"
HELP_COMMAND = "/help"

DEFAULT_PROMPT_SUFFIX = "iar> "
MAX_TURNS = 64

_IAR_AVAILABLE_SUBCOMMAND_HINT = (
    "Available `iar` subcommands: "
    "init, labels, issue, run, daemon, review, review-daemon, recover, "
    "blocked-continue, ask, deliberate, takeover, worktree, registry, "
    "workflow, completion."
)


@dataclass(frozen=True)
class ReplTurn:
    """A single turn in the REPL conversation history."""

    role: str  # "user" | "assistant" | "system" | "tool"
    content: str


@dataclass(frozen=True)
class ReplSessionInputs:
    """Input boundary for :func:`run_repl_session`.

    Attributes:
        context: Resolved repository target. ``context.repo_path`` is the
            REPL's working directory and must already be initialized with
            ``iar init``.
        agent: The agent identifier to use. ``"auto"`` is rejected at the
            CLI layer; this use case trusts the caller.
        config: REPL configuration (defaults / timeouts / allowlist).
        output_dir: Optional override for the audit directory. Defaults
            to ``config.default_output_dir``.
        stdin: Optional replacement for :data:`sys.stdin` (used in tests).
        stdout: Optional replacement for the prompt writer (tests).
    """

    context: RepositoryRunContext
    agent: str
    config: ReplConfig
    output_dir: Path | None = None
    stdin: Any = None
    stdout: Any = None


@dataclass(frozen=True)
class ReplSessionDeps:
    """Dependency injection boundary for the REPL use case."""

    process_runner: IProcessRunner
    content_generator: IContentGenerator
    command_executor: IReplCommandExecutor
    github_client: IGitHubClient | None = None
    # Optional factory used by tests that want a per-call fake github client.
    github_client_factory: Callable[[Path], IGitHubClient] | None = None
    # Optional override for the user-input function (defaults to input()).
    input_fn: Callable[[str], str] | None = None
    # Optional override for the prompt writer (defaults to print).
    prompt_output_fn: Callable[[str], None] | None = None
    # Optional override for the per-turn assistant reply printer.
    reply_output_fn: Callable[[str], None] | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_session_id() -> str:
    return datetime.now(timezone.utc).strftime("repl-%Y%m%d-%H%M%S-%f")[:-3]


def parse_iar_exec_markers(text: str) -> tuple[IarExecRequest, ...]:
    """Extract ``<<IAR_EXEC>>`` command requests from an agent reply.

    The protocol accepts multiple markers per reply. Each marker body is
    parsed via :func:`shlex.split` so that quoted arguments stay intact
    while stray newlines or extra whitespace are tolerated. Anything that
    fails to parse is logged and skipped — the rest of the reply still
    reaches the conversation history.
    """
    if not text:
        return ()
    requests: list[IarExecRequest] = []
    cursor = 0
    while cursor < len(text):
        start_idx = text.find(IAR_EXEC_OPEN_MARKER, cursor)
        if start_idx == -1:
            break
        end_idx = text.find(IAR_EXEC_CLOSE_MARKER, start_idx)
        if end_idx == -1:
            break
        body = text[start_idx + len(IAR_EXEC_OPEN_MARKER) : end_idx].strip()
        cursor = end_idx + len(IAR_EXEC_CLOSE_MARKER)
        if not body:
            continue
        try:
            argv = shlex.split(body)
        except ValueError as exc:
            _logger.warning("Skipping malformed IAR_EXEC body %r: %s", body, exc)
            continue
        if not argv:
            continue
        # Strip a leading `iar` token so that the executor never sees a
        # duplicate ``["iar", "iar", ...]``. The protocol is documented
        # to include `iar` in the marker body, but it is redundant from
        # the executor's perspective.
        if argv[0] == "iar":
            argv = argv[1:]
        if not argv:
            continue
        requests.append(IarExecRequest(argv=tuple(argv), raw_text=body))
    return tuple(requests)


def _strip_exec_markers(text: str) -> str:
    """Remove ``<<IAR_EXEC>>...<<END_IAR_EXEC>>`` markers from assistant text.

    Used for the visible reply printed to the terminal so that the
    conversation transcript shown to the user does not include raw
    protocol markers. The full marker text is still preserved in the
    conversation history passed back to the agent.
    """
    if not text:
        return text
    result: list[str] = []
    cursor = 0
    while cursor < len(text):
        start_idx = text.find(IAR_EXEC_OPEN_MARKER, cursor)
        if start_idx == -1:
            result.append(text[cursor:])
            break
        result.append(text[cursor:start_idx])
        end_idx = text.find(IAR_EXEC_CLOSE_MARKER, start_idx)
        if end_idx == -1:
            break
        cursor = end_idx + len(IAR_EXEC_CLOSE_MARKER)
    return "".join(result).strip()


def _build_system_prompt(
    *,
    context: RepositoryRunContext,
    config: ReplConfig,
    session_id: str,
    github_client: IGitHubClient | None,
) -> str:
    """Assemble the first system prompt for the REPL.

    Reuses the planning-aware blocks from ``iar ask`` (pending PRD
    summary, GitHub Issue summary) and layers REPL-only fields
    (``.iar.toml`` summary, command-execution protocol, available
    subcommand hint). When ``github_client`` is ``None`` (e.g. in unit
    tests or non-interactive auth-disabled mode), the Issue summary
    falls back to a friendly placeholder.
    """
    repo_config = context.config
    pending_prd_summary = _build_pending_prd_summary(context.repo_path)
    if github_client is not None:
        issue_summary = _build_issue_summary(context.repo_path, repo_config, github_client)
    else:
        issue_summary = "GitHub Issue summary unavailable (no client)."

    config_summary = (
        f"repository.id={context.repo_id}, "
        f"git.base_branch={repo_config.git.base_branch}, "
        f"runner.default_agent={repo_config.runner.default_agent}, "
        f"verification_commands={list(repo_config.runner.verification_commands)}, "
        f"labels.ready={repo_config.labels.ready}, "
        f"labels.failed={repo_config.labels.failed}"
    )

    protocol = (
        "Command execution protocol:\n"
        f"- To ask me to run an `iar` subcommand, wrap the full command "
        f"(including `iar`) inside markers like:\n"
        f"  {IAR_EXEC_OPEN_MARKER} iar labels sync {IAR_EXEC_CLOSE_MARKER}\n"
        f"- Only whitelisted `iar` subcommands are allowed. Direct shell "
        f"commands, `git push` / `git merge` / `git reset`, and arbitrary "
        f"`rm` invocations are refused.\n"
        f"- I will execute read-only/dry-run commands automatically. "
        f"Write or risky commands require explicit user confirmation.\n"
        f"- Each execution result is appended back to our conversation as a "
        f"`{IAR_EXEC_RESULT_OPEN_TAG}` block.\n"
        "- You do not need to confirm successful commands; just react to "
        "their output."
    )

    parts: list[str] = []
    parts.append(
        "You are the agent assistant for an interactive IAR REPL session.\n"
        f"Session ID: {session_id}\n"
        f"Repository: {context.repo_id} ({context.display_name})\n"
        f"Path: {context.repo_path}\n\n"
        "The user will type natural-language requests. Use the context "
        "below and your shell tooling to assist them. When you need to "
        "invoke an `iar` subcommand on the user's behalf, follow the "
        "command-execution protocol strictly.\n\n"
        f"{protocol}\n\n"
        f"{_IAR_AVAILABLE_SUBCOMMAND_HINT}\n"
    )
    parts.append(f"## Repository .iar.toml summary\n{config_summary}\n")
    parts.append(
        "## Decision context (carried over from `iar ask`)\n"
        f"- Pending PRDs:\n{pending_prd_summary}\n"
        f"- Relevant Issues:\n{issue_summary}\n"
    )

    return _truncate_prompt("\n".join(parts), config.max_context_chars)


def _serialize_history(history: Sequence[ReplTurn]) -> str:
    """Serialize the conversation history as a single prompt for the agent.

    The agent CLI sees one ``-p`` / ``--prompt`` argument; we therefore
    flatten the structured turns into a labelled, deterministic text.
    """
    blocks: list[str] = []
    for turn in history:
        role_label = {
            "system": "[SYSTEM]",
            "user": "[USER]",
            "assistant": "[ASSISTANT]",
            "tool": "[TOOL]",
        }.get(turn.role, f"[{turn.role.upper()}]")
        blocks.append(f"{role_label}\n{turn.content}")
    return "\n\n".join(blocks)


def _format_exec_result_for_agent(outcome: ReplExecOutcome) -> str:
    """Render a :class:`ReplExecOutcome` as a tool-result block."""
    cmd_text = "iar " + " ".join(outcome.argv)
    if outcome.rejected:
        body = (
            f"{IAR_EXEC_RESULT_OPEN_TAG}\n"
            f"command: {cmd_text}\n"
            f"status: rejected\n"
            f"reason: {outcome.rejection_reason}\n"
            f"{IAR_EXEC_RESULT_CLOSE_TAG}"
        )
        return body
    status_label = "ok" if outcome.return_code == 0 else "failed"
    body_lines = [
        IAR_EXEC_RESULT_OPEN_TAG,
        f"command: {cmd_text}",
        f"status: {status_label}",
        f"exit_code: {outcome.return_code}",
    ]
    if outcome.stdout:
        body_lines.append(f"stdout:\n{outcome.stdout}")
    if outcome.stderr:
        body_lines.append(f"stderr:\n{outcome.stderr}")
    body_lines.append(IAR_EXEC_RESULT_CLOSE_TAG)
    return "\n".join(body_lines)


def _write_audit(
    *,
    session_dir: Path,
    history: Sequence[ReplTurn],
    executed_commands: Sequence[dict[str, Any]],
    metadata: dict[str, Any],
) -> None:
    """Write the REPL session audit files."""
    session_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = session_dir / "transcript.md"
    transcript_lines: list[str] = [
        f"# REPL session {metadata.get('session_id', '')}",
        "",
        f"- repository: {metadata.get('repo_id', '')}",
        f"- repo_path: {metadata.get('repo_path', '')}",
        f"- agent: {metadata.get('agent', '')}",
        f"- started_at: {metadata.get('started_at', '')}",
        f"- finished_at: {metadata.get('finished_at', '')}",
        "",
        "## Conversation",
        "",
    ]
    for turn in history:
        title = {
            "system": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool result",
        }.get(turn.role, turn.role.capitalize())
        transcript_lines.append(f"### {title}")
        transcript_lines.append("")
        transcript_lines.append(turn.content)
        transcript_lines.append("")
    transcript_path.write_text("\n".join(transcript_lines), encoding="utf-8")

    (session_dir / "commands.json").write_text(
        json.dumps(list(executed_commands), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (session_dir / "session.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _read_user_input(
    input_fn: Callable[[str], str] | None,
    prompt: str,
) -> str | None:
    """Read a single line of user input.

    Returns ``None`` on EOF (so the REPL exits gracefully). Both
    :class:`EOFError` and a raised :class:`StopIteration` (typical of
    empty iterators in unit tests) are treated as EOF.
    """
    call = input_fn if input_fn is not None else input
    try:
        return call(prompt)
    except (EOFError, StopIteration):
        return None


def run_repl_session(inputs: ReplSessionInputs, deps: ReplSessionDeps) -> int:
    """Run the interactive REPL session.

    Returns the process exit code (0 on normal ``/exit``/EOF, 1 on a
    configuration / initialization failure). The function is blocking;
    call sites that need to spawn it should run it on the main thread
    or wrap it in :class:`concurrent.futures.ThreadPoolExecutor`.
    """
    config = inputs.config
    repo_path = inputs.context.repo_path

    if not config.enabled:
        _logger.error("REPL is disabled in configuration.")
        return 1

    session_id = _generate_session_id()
    output_root = inputs.output_dir or Path(config.default_output_dir)
    output_root = output_root.expanduser()
    if not output_root.is_absolute():
        output_root = (repo_path / output_root).resolve()
    session_dir = output_root / session_id

    history: list[ReplTurn] = []
    executed_commands: list[dict[str, Any]] = []

    started_at = _now_iso()
    prompt_output = deps.prompt_output_fn if deps.prompt_output_fn is not None else print
    reply_output = deps.reply_output_fn if deps.reply_output_fn is not None else print

    system_prompt = _build_system_prompt(
        context=inputs.context,
        config=config,
        session_id=session_id,
        github_client=deps.github_client,
    )
    history.append(ReplTurn(role="system", content=system_prompt))

    prompt_output(
        f"REPL session started (id={session_id}, agent={inputs.agent}). "
        "Type '/exit' to quit, '/help' for available commands."
    )

    turn_count = 0
    try:
        while turn_count < MAX_TURNS:
            user_line = _read_user_input(deps.input_fn, DEFAULT_PROMPT_SUFFIX)
            if user_line is None:
                prompt_output("\n[EOF received; exiting REPL.]")
                break
            user_line = user_line.strip()
            if not user_line:
                continue
            if user_line == EXIT_COMMAND:
                prompt_output("[Exiting REPL.]")
                break
            if user_line == HELP_COMMAND:
                prompt_output(
                    "REPL commands:\n"
                    f"  {EXIT_COMMAND}    quit the session\n"
                    f"  {HELP_COMMAND}    show this help\n"
                    "  anything else     forwarded to the agent\n\n"
                    f"{_IAR_AVAILABLE_SUBCOMMAND_HINT}"
                )
                continue

            turn_count += 1
            history.append(ReplTurn(role="user", content=user_line))

            agent_result: CommandResult = deps.content_generator.generate(
                inputs.agent,
                _serialize_history(history),
                cwd=repo_path,
                timeout=config.agent_timeout_seconds,
            )

            if agent_result.return_code != 0:
                error_block = (
                    f"[AGENT_ERROR]\n"
                    f"exit_code={agent_result.return_code}\n"
                    f"stderr:\n{agent_result.stderr}"
                )
                history.append(ReplTurn(role="tool", content=error_block))
                prompt_output(f"[agent error: exit_code={agent_result.return_code}]")
                if agent_result.stderr:
                    prompt_output(agent_result.stderr)
                continue

            assistant_text = agent_result.stdout
            history.append(ReplTurn(role="assistant", content=assistant_text))

            marker_requests: Sequence[IarExecRequest] = parse_iar_exec_markers(assistant_text)
            for exec_request in marker_requests:
                outcome: ReplExecOutcome = deps.command_executor.execute(
                    exec_request, repo_path=repo_path
                )
                result_block = _format_exec_result_for_agent(outcome)
                history.append(ReplTurn(role="tool", content=result_block))
                executed_commands.append(
                    {
                        "argv": list(outcome.argv),
                        "raw_text": exec_request.raw_text,
                        "return_code": outcome.return_code,
                        "rejected": outcome.rejected,
                        "rejection_reason": outcome.rejection_reason,
                        "confirmation_prompted": outcome.confirmation_prompted,
                        "confirmation_granted": outcome.confirmation_granted,
                    }
                )

            visible_reply = _strip_exec_markers(assistant_text)
            if visible_reply:
                reply_output(visible_reply)

        if turn_count >= MAX_TURNS:
            prompt_output(f"[REPL hit the {MAX_TURNS}-turn safety cap; exiting.]")
    except KeyboardInterrupt:
        prompt_output("\n[Interrupted; exiting REPL.]")

    finished_at = _now_iso()
    metadata = {
        "session_id": session_id,
        "repo_id": inputs.context.repo_id,
        "repo_path": str(repo_path),
        "display_name": inputs.context.display_name,
        "agent": inputs.agent,
        "started_at": started_at,
        "finished_at": finished_at,
        "turns": turn_count,
        "commands_executed": len(executed_commands),
        "history_length": len(history),
    }
    try:
        _write_audit(
            session_dir=session_dir,
            history=tuple(history),
            executed_commands=tuple(executed_commands),
            metadata=metadata,
        )
    except OSError as exc:
        _logger.warning("Failed to write REPL audit to %s: %s", session_dir, exc)

    return 0


__all__ = [
    "ReplSessionInputs",
    "ReplSessionDeps",
    "ReplTurn",
    "IAR_EXEC_OPEN_MARKER",
    "IAR_EXEC_CLOSE_MARKER",
    "IAR_EXEC_RESULT_OPEN_TAG",
    "IAR_EXEC_RESULT_CLOSE_TAG",
    "EXIT_COMMAND",
    "HELP_COMMAND",
    "parse_iar_exec_markers",
    "run_repl_session",
]
