"""Agent Runner content generators.

Holds :class:`SubprocessContentGenerator`,
:class:`SafePlannerContentGenerator`, and the agent-command builders used
by both content generation and REPL flows. Extracted out of
:mod:`backend.engines.agent_runner.factory` so the content-side and
repository-side concerns can live in separate files.
"""

from __future__ import annotations

from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IContentGenerator
from backend.core.shared.models.agent_runner import CommandResult
from backend.infrastructure.process_runner import SubprocessRunner


class SubprocessContentGenerator(IContentGenerator):
    """Generate content via a read-only local agent subprocess.

    Implements ``IContentGenerator`` via duck typing.
    """

    def __init__(
        self,
        process_runner: SubprocessRunner,
        *,
        read_only: bool = True,
    ) -> None:
        self._process_runner = process_runner
        self._read_only = read_only

    def generate(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        timeout: int | None = None,
    ) -> CommandResult:
        """Run a content generator and return its output.

        When the instance was constructed with ``read_only=True`` (the
        default) the agent runs in its read-only sandbox. The REPL
        entrypoint constructs the generator with ``read_only=False`` so
        the agent can mutate files inside the user's confirmation model.
        """
        command = _build_content_generation_command(
            agent_name, prompt, cwd, read_only=self._read_only
        )
        return self._process_runner.run(
            command, cwd=cwd, capture_output=True, timeout=timeout, check=False
        )


def _build_content_generation_command(
    agent_name: str,
    prompt: str,
    cwd: Path,
    *,
    read_only: bool = True,
) -> list[str]:
    """Build the agent command for content generation / REPL use.

    Args:
        agent_name: ``claude`` / ``codex`` / ``kimi`` (or any value that
            should fall back to ``claude``).
        prompt: Full prompt text passed to the agent.
        cwd: Working directory for the agent subprocess.
        read_only: When ``True`` (default), ``codex`` is invoked with
            ``--sandbox read-only --ask-for-approval never`` so it cannot
            modify the filesystem. When ``False`` (used by the REPL
            entrypoint), the sandbox flag is dropped so the agent is free
            to write files within the user's confirmation model. ``claude``
            and ``kimi`` commands are unaffected by this flag because they
            already have a single canonical invocation shape.

    Returns:
        Command argv ready to be handed to a process runner.
    """
    # codex / kimi 需显式指定；其余（"claude"、已解析的 "auto"、或任何未识别值）
    # 一律构造 claude 命令，绝不静默落到 codex。
    if agent_name == "codex":
        if read_only:
            return [
                "codex",
                "--cd",
                str(cwd),
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "exec",
                prompt,
            ]
        return [
            "codex",
            "--cd",
            str(cwd),
            "exec",
            prompt,
        ]
    if agent_name == "kimi":
        return ["kimi", "--prompt", prompt]
    return [
        "claude",
        "--dangerously-skip-permissions",
        "-p",
        prompt,
    ]


def _build_repl_command(agent_name: str, prompt: str, cwd: Path) -> list[str]:
    """Build the agent command used by the ``iar`` REPL entrypoint.

    Delegates to :func:`_build_content_generation_command` with
    ``read_only=False`` so that REPL-managed sessions do not run inside
    ``codex``'s read-only sandbox. The REPL's own command executor
    provides the safety boundary for arbitrary IAR subcommands.
    """
    return _build_content_generation_command(agent_name, prompt, cwd, read_only=False)


class SafePlannerContentGenerator(IContentGenerator):
    """Generate decision plans via a local agent subprocess.

    The planner delegates to the same agent command builders used for content
    generation.  Callers are responsible for validating and sandboxing the
    resulting plan; this runner does not enforce read-only execution.
    """

    def __init__(self, process_runner: SubprocessRunner) -> None:
        self._process_runner = process_runner

    def generate(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        timeout: int | None = None,
    ) -> CommandResult:
        """Run a planner agent and return its output."""
        command = _build_planner_command(agent_name, prompt, cwd)
        return self._process_runner.run(
            command, cwd=cwd, capture_output=True, timeout=timeout, check=False
        )


def _build_planner_command(agent_name: str, prompt: str, cwd: Path) -> list[str]:
    """Return a command for the given planner agent.

    The planner reuses the content-generation command builders so that all
    supported agents can act as planners.  Planner output is still expected
    to be a JSON DecisionPlan and is validated by the core use case.

    Raises:
        ValueError: If the agent is not one of the supported planner agents.
    """
    if agent_name not in ("claude", "codex", "kimi"):
        raise ValueError(
            f"Agent '{agent_name}' does not have a command builder "
            f"for interactive decision planning. Use 'claude', 'codex', or 'kimi'."
        )
    return _build_content_generation_command(agent_name, prompt, cwd)


def create_planner_runner(
    process_runner: SubprocessRunner | None = None,
) -> SafePlannerContentGenerator:
    """Create a safe planner runner instance."""
    return SafePlannerContentGenerator(process_runner or SubprocessRunner())


def create_content_generator(
    process_runner: SubprocessRunner | None = None,
    *,
    read_only: bool = True,
) -> SubprocessContentGenerator:
    """Create a content generator instance."""
    return SubprocessContentGenerator(process_runner or SubprocessRunner(), read_only=read_only)


__all__ = [
    "SafePlannerContentGenerator",
    "SubprocessContentGenerator",
    "_build_content_generation_command",
    "_build_planner_command",
    "_build_repl_command",
    "create_content_generator",
    "create_planner_runner",
]
