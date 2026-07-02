"""REPL command executor.

Implements :class:`IReplCommandExecutor` for the ``iar`` REPL entrypoint.
The executor owns the policy decisions that core use cases should not
have to know about: which IAR subcommands are allowed, which need
explicit confirmation, how to invoke the real ``iar`` CLI as a
subprocess, and how to truncate captured output before returning it to
the conversation history.

The executor never falls back to a shell interpreter: commands are
dispatched as argv vectors so there is no quoting / expansion attack
surface even when the agent emits unusual strings.
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IarExecRequest,
    IProcessRunner,
    IReplCommandExecutor,
    ReplExecOutcome,
)
from backend.core.shared.models.agent_decision import ReplConfig

_logger = logging.getLogger(__name__)

_DEFAULT_ALLOWLIST: tuple[str, ...] = (
    "init",
    "labels",
    "issue",
    "run",
    "daemon",
    "review",
    "review-daemon",
    "recover",
    "blocked-continue",
    "ask",
    "deliberate",
    "takeover",
    "worktree",
    "registry",
    "workflow",
    "completion",
    "--version",
    "version",
)

_DEFAULT_DENYLIST_PREFIXES: tuple[str, ...] = (
    "git push",
    "git merge",
    "git reset",
    "git clean",
    "gh ",
    "rm -rf",
    "rm -fr",
)

_DEFAULT_DENYLIST_SHELL_METACHARS: str = ";&|`$><\n\r"

_MAX_STDOUT_CHARS = 6000
_MAX_STDERR_CHARS = 3000


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... truncated {len(text) - max_chars} chars ..."


def _starts_with_any(text: str, prefixes: tuple[str, ...]) -> bool:
    return any(text == prefix or text.startswith(prefix + " ") for prefix in prefixes)


class ReplCommandExecutor(IReplCommandExecutor):
    """Validate and execute IAR subcommands requested by the REPL agent.

    Args:
        process_runner: Subprocess runner used to invoke ``iar``.
        config: REPL configuration (allowlist / confirm / audit settings).
        input_fn: Replacement for ``builtins.input`` so unit tests can
            drive the confirmation prompts deterministically. Defaults to
            the real :func:`input`.
        prompt_output_fn: Replacement for the side-channel used to print
            confirmation prompts. Defaults to :func:`print`.
    """

    def __init__(
        self,
        process_runner: IProcessRunner,
        config: ReplConfig,
        *,
        input_fn=None,
        prompt_output_fn=None,
    ) -> None:
        self._process_runner = process_runner
        self._config = config
        self._input_fn = input_fn if input_fn is not None else input
        self._prompt_output_fn = (
            prompt_output_fn if prompt_output_fn is not None else print
        )

    def execute(
        self,
        request: IarExecRequest,
        *,
        repo_path: Path,
    ) -> ReplExecOutcome:
        argv = list(request.argv)
        if not argv:
            return ReplExecOutcome(
                argv=tuple(argv),
                return_code=2,
                stdout="",
                stderr="",
                rejected=True,
                rejection_reason="Empty command.",
            )

        # Reject any shell metacharacters before doing anything else.
        joined = " ".join(argv)
        for bad_char in _DEFAULT_DENYLIST_SHELL_METACHARS:
            if bad_char in joined:
                return ReplExecOutcome(
                    argv=tuple(argv),
                    return_code=2,
                    stdout="",
                    stderr="",
                    rejected=True,
                    rejection_reason=(
                        "Command contains forbidden shell metacharacter "
                        f"{bad_char!r}; only IAR subcommands are allowed."
                    ),
                )

        # Denylist prefix check covers direct git / gh / rm escape hatches.
        if _starts_with_any(joined, _DEFAULT_DENYLIST_PREFIXES):
            return ReplExecOutcome(
                argv=tuple(argv),
                return_code=2,
                stdout="",
                stderr="",
                rejected=True,
                rejection_reason=(
                    f"Command {joined!r} is forbidden in the REPL sandbox."
                ),
            )

        # Allowlist: the first argv must match a known IAR subcommand.
        head = argv[0]
        if head not in _DEFAULT_ALLOWLIST:
            return ReplExecOutcome(
                argv=tuple(argv),
                return_code=2,
                stdout="",
                stderr="",
                rejected=True,
                rejection_reason=(f"Command {head!r} is not in the REPL allowlist."),
            )

        tail = " ".join(argv[1:])
        prefix = f"{head} {tail}".strip()

        needs_confirmation = _starts_with_any(prefix, self._config.confirm_commands)
        auto_confirm = _starts_with_any(prefix, self._config.auto_confirm_commands)

        confirmation_granted: bool | None = None
        if auto_confirm:
            confirmation_granted = True
        elif needs_confirmation:
            self._prompt_output_fn(
                f"\nThe agent wants to run a write/risky command:\n"
                f"  iar {prefix}\n"
                f"This command is in the confirm list. Execute? [y/N]: "
            )
            try:
                response = self._input_fn("")
            except EOFError:
                return ReplExecOutcome(
                    argv=tuple(argv),
                    return_code=2,
                    stdout="",
                    stderr="",
                    rejected=True,
                    rejection_reason=(
                        "No interactive input available for confirmation; "
                        "command refused."
                    ),
                    confirmation_prompted=True,
                    confirmation_granted=False,
                )
            confirmation_granted = response.strip().lower() in ("y", "yes")
            if not confirmation_granted:
                return ReplExecOutcome(
                    argv=tuple(argv),
                    return_code=2,
                    stdout="",
                    stderr="",
                    rejected=True,
                    rejection_reason="User did not confirm the command.",
                    confirmation_prompted=True,
                    confirmation_granted=False,
                )

        _logger.info("REPL executing: iar %s", prefix)
        result = self._process_runner.run(
            ["iar", *argv],
            cwd=repo_path,
            capture_output=True,
            check=False,
        )
        return ReplExecOutcome(
            argv=tuple(argv),
            return_code=result.return_code,
            stdout=_truncate(result.stdout, _MAX_STDOUT_CHARS),
            stderr=_truncate(result.stderr, _MAX_STDERR_CHARS),
            confirmation_prompted=needs_confirmation,
            confirmation_granted=confirmation_granted,
        )


__all__ = ["ReplCommandExecutor"]
