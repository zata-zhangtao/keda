"""Tests for the ``iar`` REPL use case and command executor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


from backend.core.shared.interfaces.agent_runner import (
    IarExecRequest,
)
from backend.core.shared.models.agent_decision import ReplConfig
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    GitConfig,
    LabelConfig,
    RepositoryRunContext,
    RunnerConfig,
)
from backend.core.use_cases.repl_session import (
    EXIT_COMMAND,
    HELP_COMMAND,
    IAR_EXEC_CLOSE_MARKER,
    IAR_EXEC_OPEN_MARKER,
    ReplSessionDeps,
    ReplSessionInputs,
    parse_iar_exec_markers,
    run_repl_session,
)
from backend.engines.agent_runner.repl_command_executor import ReplCommandExecutor
from tests.conftest import FakeContentGenerator, FakeGitHubClient, FakeProcessRunner


def _make_context(tmp_path: Path) -> RepositoryRunContext:
    """Build a minimal RepositoryRunContext for unit tests."""
    return RepositoryRunContext(
        repo_id="test-repo",
        display_name="Test Repo",
        repo_path=tmp_path,
        config=AppConfig(
            git=GitConfig(remote="origin", base_branch="main"),
            labels=LabelConfig(),
            runner=RunnerConfig(default_agent="claude"),
            repl=ReplConfig(default_agent="claude"),
        ),
    )


def _make_config(
    *,
    auto_confirm: tuple[str, ...] = ("labels sync --dry-run",),
    confirm: tuple[str, ...] = ("run", "daemon"),
    enabled: bool = True,
    agent_timeout_seconds: int = 60,
) -> ReplConfig:
    return ReplConfig(
        enabled=enabled,
        default_agent="claude",
        default_output_dir=str(Path("logs") / "agent-runner" / "repl-test"),
        max_context_chars=20000,
        agent_timeout_seconds=agent_timeout_seconds,
        auto_confirm_commands=auto_confirm,
        confirm_commands=confirm,
    )


# ---------------------------------------------------------------------------
# parse_iar_exec_markers
# ---------------------------------------------------------------------------


def test_parse_markers_extracts_single_command() -> None:
    text = (
        "Sure, I'll do that.\n"
        f"{IAR_EXEC_OPEN_MARKER} iar labels sync --dry-run {IAR_EXEC_CLOSE_MARKER}\n"
        "Done."
    )
    requests = parse_iar_exec_markers(text)
    assert len(requests) == 1
    assert requests[0].argv == ("labels", "sync", "--dry-run")


def test_parse_markers_extracts_multiple_commands() -> None:
    text = (
        f"{IAR_EXEC_OPEN_MARKER} iar labels sync {IAR_EXEC_CLOSE_MARKER} and "
        f"{IAR_EXEC_OPEN_MARKER} iar status {IAR_EXEC_CLOSE_MARKER}"
    )
    requests = parse_iar_exec_markers(text)
    assert [r.argv for r in requests] == [
        ("labels", "sync"),
        ("status",),
    ]


def test_parse_markers_preserves_quoted_args() -> None:
    text = (
        f'{IAR_EXEC_OPEN_MARKER} iar issue create --title "Hello World" '
        f"{IAR_EXEC_CLOSE_MARKER}"
    )
    requests = parse_iar_exec_markers(text)
    assert len(requests) == 1
    assert requests[0].argv == (
        "issue",
        "create",
        "--title",
        "Hello World",
    )


def test_parse_markers_returns_empty_when_no_markers() -> None:
    assert parse_iar_exec_markers("nothing here") == ()


def test_parse_markers_skips_malformed_body() -> None:
    text = f"{IAR_EXEC_OPEN_MARKER} not a valid shell line " f"{IAR_EXEC_CLOSE_MARKER}"
    # shlex.split can split the body, but the first token "not" is fine.
    # Truly malformed bodies (unterminated quotes) should be skipped.
    text2 = (
        f'{IAR_EXEC_OPEN_MARKER} iar issue create --title "oops '
        f"{IAR_EXEC_CLOSE_MARKER}"
    )
    requests = parse_iar_exec_markers(text)
    # First one parses fine; the malformed second one should be skipped.
    assert all(not r.raw_text.endswith("oops ") for r in requests)
    requests2 = parse_iar_exec_markers(text2)
    assert requests2 == ()


# ---------------------------------------------------------------------------
# ReplCommandExecutor
# ---------------------------------------------------------------------------


def _make_process_runner() -> FakeProcessRunner:
    return FakeProcessRunner()


def test_executor_rejects_shell_metachar() -> None:
    runner = _make_process_runner()
    executor = ReplCommandExecutor(runner, _make_config())
    outcome = executor.execute(
        IarExecRequest(
            argv=("labels", "sync", ";", "rm", "-rf", "/"),
            raw_text="labels sync ; rm -rf /",
        ),
        repo_path=Path("/tmp"),
    )
    assert outcome.rejected
    assert "metacharacter" in outcome.rejection_reason
    assert runner.calls == []


def test_executor_rejects_unknown_command() -> None:
    runner = _make_process_runner()
    executor = ReplCommandExecutor(runner, _make_config())
    outcome = executor.execute(
        IarExecRequest(argv=("evil", "command"), raw_text="evil command"),
        repo_path=Path("/tmp"),
    )
    assert outcome.rejected
    assert "not in the REPL allowlist" in outcome.rejection_reason
    assert runner.calls == []


def test_executor_rejects_git_push() -> None:
    runner = _make_process_runner()
    executor = ReplCommandExecutor(runner, _make_config())
    outcome = executor.execute(
        IarExecRequest(
            argv=("git", "push", "origin", "main"),
            raw_text="git push origin main",
        ),
        repo_path=Path("/tmp"),
    )
    assert outcome.rejected
    assert "forbidden" in outcome.rejection_reason.lower()


def test_executor_runs_auto_confirm_command() -> None:
    runner = _make_process_runner()
    runner.responses[("iar", "labels", "sync", "--dry-run")] = CommandResult(
        command=("iar", "labels", "sync", "--dry-run"),
        return_code=0,
        stdout="Labels synced",
        stderr="",
    )
    executor = ReplCommandExecutor(runner, _make_config())
    outcome = executor.execute(
        IarExecRequest(
            argv=("labels", "sync", "--dry-run"),
            raw_text="labels sync --dry-run",
        ),
        repo_path=Path("/tmp"),
    )
    assert outcome.return_code == 0
    assert outcome.stdout == "Labels synced"
    assert outcome.confirmation_prompted is False
    assert runner.calls == [["iar", "labels", "sync", "--dry-run"]]


def test_executor_prompts_for_confirm_command_and_user_says_yes() -> None:
    runner = _make_process_runner()
    runner.responses[("iar", "run")] = CommandResult(
        command=("iar", "run"), return_code=0, stdout="ok", stderr=""
    )
    prompts: list[str] = []
    responses = iter(["y"])
    executor = ReplCommandExecutor(
        runner,
        _make_config(),
        input_fn=lambda _prompt: next(responses),
        prompt_output_fn=prompts.append,
    )
    outcome = executor.execute(
        IarExecRequest(argv=("run",), raw_text="run"),
        repo_path=Path("/tmp"),
    )
    assert outcome.confirmation_prompted
    assert outcome.confirmation_granted is True
    assert outcome.return_code == 0
    assert any("confirm" in line.lower() for line in prompts)


def test_executor_prompts_for_confirm_command_and_user_says_no() -> None:
    runner = _make_process_runner()
    prompts: list[str] = []
    executor = ReplCommandExecutor(
        runner,
        _make_config(),
        input_fn=lambda _prompt: "n",
        prompt_output_fn=prompts.append,
    )
    outcome = executor.execute(
        IarExecRequest(argv=("daemon",), raw_text="daemon"),
        repo_path=Path("/tmp"),
    )
    assert outcome.confirmation_prompted
    assert outcome.confirmation_granted is False
    assert outcome.rejected
    assert runner.calls == []


def test_executor_eoferror_during_confirm_is_rejected() -> None:
    runner = _make_process_runner()

    def _raise_eof(_prompt: str) -> str:
        raise EOFError

    executor = ReplCommandExecutor(runner, _make_config(), input_fn=_raise_eof)
    outcome = executor.execute(
        IarExecRequest(argv=("run",), raw_text="run"),
        repo_path=Path("/tmp"),
    )
    assert outcome.rejected
    assert outcome.confirmation_prompted
    assert outcome.confirmation_granted is False


# ---------------------------------------------------------------------------
# run_repl_session — single-turn happy path with command execution
# ---------------------------------------------------------------------------


def test_run_repl_executes_iar_command_from_agent_reply(
    tmp_path: Path,
) -> None:
    context = _make_context(tmp_path)
    process_runner = _make_process_runner()
    process_runner.responses[("iar", "labels", "sync", "--dry-run")] = CommandResult(
        command=("iar", "labels", "sync", "--dry-run"),
        return_code=0,
        stdout="Labels synced.",
        stderr="",
    )
    content_generator = FakeContentGenerator(
        response=(
            "I'll sync the labels.\n"
            f"{IAR_EXEC_OPEN_MARKER} iar labels sync --dry-run "
            f"{IAR_EXEC_CLOSE_MARKER}"
        )
    )
    command_executor = ReplCommandExecutor(
        process_runner,
        _make_config(),
        input_fn=lambda _p: "y",
    )
    inputs = _make_inputs(context)
    deps = _make_deps(
        process_runner=process_runner,
        content_generator=content_generator,
        command_executor=command_executor,
        inputs=inputs,
        user_inputs=iter(["sync labels", EXIT_COMMAND]),
    )
    exit_code = run_repl_session(inputs, deps)
    assert exit_code == 0

    # The agent subprocess was called exactly once.
    assert len(content_generator.calls) == 1
    assert content_generator.calls[0][0] == "claude"

    # The iar labels sync command was actually executed by the process runner.
    assert [
        call
        for call in process_runner.calls
        if call[0] == "iar" and call[1] == "labels"
    ] == [["iar", "labels", "sync", "--dry-run"]]

    # Audit directory contains the session files.
    audit_root = tmp_path / "logs" / "agent-runner" / "repl-test"
    session_dirs = list(audit_root.iterdir())
    assert len(session_dirs) == 1
    session_dir = session_dirs[0]
    assert (session_dir / "session.json").is_file()
    assert (session_dir / "transcript.md").is_file()
    session_metadata = json.loads(
        (session_dir / "session.json").read_text(encoding="utf-8")
    )
    assert session_metadata["agent"] == "claude"
    assert session_metadata["commands_executed"] == 1


def test_run_repl_exits_on_exit_command(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    process_runner = _make_process_runner()
    content_generator = FakeContentGenerator(response="hello")
    command_executor = ReplCommandExecutor(
        process_runner, _make_config(), input_fn=lambda _p: "y"
    )
    inputs = _make_inputs(context)
    deps = _make_deps(
        process_runner=process_runner,
        content_generator=content_generator,
        command_executor=command_executor,
        inputs=inputs,
        user_inputs=iter([EXIT_COMMAND]),
    )
    exit_code = run_repl_session(inputs, deps)
    assert exit_code == 0
    # The agent should never have been invoked when the user only typed /exit.
    assert content_generator.calls == []


def test_run_repl_exits_on_eof(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    process_runner = _make_process_runner()
    content_generator = FakeContentGenerator(response="hello")
    command_executor = ReplCommandExecutor(
        process_runner, _make_config(), input_fn=lambda _p: "y"
    )
    inputs = _make_inputs(context)
    deps = _make_deps(
        process_runner=process_runner,
        content_generator=content_generator,
        command_executor=command_executor,
        inputs=inputs,
        user_inputs=iter([]),
    )
    exit_code = run_repl_session(inputs, deps)
    assert exit_code == 0
    assert content_generator.calls == []


def test_run_repl_records_agent_failure_in_history(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    process_runner = _make_process_runner()

    class _FailingContentGenerator(FakeContentGenerator):
        def generate(self, *args, **kwargs):  # type: ignore[override]
            return CommandResult(
                command=("generate",),
                return_code=124,
                stdout="",
                stderr="timeout exceeded",
            )

    command_executor = ReplCommandExecutor(
        process_runner, _make_config(), input_fn=lambda _p: "y"
    )
    inputs = _make_inputs(context)
    deps = _make_deps(
        process_runner=process_runner,
        content_generator=_FailingContentGenerator(),
        command_executor=command_executor,
        inputs=inputs,
        user_inputs=iter(["do something"]),
    )
    exit_code = run_repl_session(inputs, deps)
    assert exit_code == 0

    audit_root = tmp_path / "logs" / "agent-runner" / "repl-test"
    session_dir = next(iter(audit_root.iterdir()))
    transcript = (session_dir / "transcript.md").read_text(encoding="utf-8")
    assert "timeout exceeded" in transcript
    assert "exit_code=124" in transcript


def test_run_repl_handles_help_command_without_calling_agent(
    tmp_path: Path,
) -> None:
    context = _make_context(tmp_path)
    process_runner = _make_process_runner()
    content_generator = FakeContentGenerator(response="hello")
    command_executor = ReplCommandExecutor(
        process_runner, _make_config(), input_fn=lambda _p: "y"
    )
    inputs = _make_inputs(context)
    deps = _make_deps(
        process_runner=process_runner,
        content_generator=content_generator,
        command_executor=command_executor,
        inputs=inputs,
        user_inputs=iter([HELP_COMMAND, EXIT_COMMAND]),
    )
    exit_code = run_repl_session(inputs, deps)
    assert exit_code == 0
    assert content_generator.calls == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_inputs(
    context: RepositoryRunContext,
    *,
    output_dir: Path | None = None,
    config: ReplConfig | None = None,
) -> ReplSessionInputs:
    return ReplSessionInputs(
        context=context,
        agent="claude",
        config=config or _make_config(),
        output_dir=output_dir,
    )


def _make_deps(
    *,
    process_runner: FakeProcessRunner,
    content_generator: FakeContentGenerator,
    command_executor: ReplCommandExecutor,
    inputs: ReplSessionInputs,
    user_inputs: Iterator[str] | None = None,
) -> ReplSessionDeps:
    user_inputs = user_inputs if user_inputs is not None else iter([])
    return ReplSessionDeps(
        process_runner=process_runner,
        content_generator=content_generator,
        command_executor=command_executor,
        github_client=FakeGitHubClient(),
        input_fn=lambda _prompt: next(user_inputs),
    )
