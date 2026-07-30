"""Tests for invoking the coding agent process.

Covers agent selection, ``run_agent_with_prompt`` argv/timeout/logging
behavior, agent stdout extraction, and the Level 1 transient-failure
retry wrapper ``run_agent_with_prompt_resilient``."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
)
from backend.core.use_cases.run_agent_once import (
    AgentUnavailableError,
    choose_agent,
    extract_agent_response_text,
    format_command,
    run_agent_with_prompt,
    run_agent_with_prompt_resilient,
)
from backend.infrastructure.process_runner import CommandFailedError
from tests.conftest import FakeProcessRunner
from tests.support.agent_runner import (
    transient_command_error,
)


def test_format_command_substitutes_issue_number() -> None:
    """Command templates should have {issue_number} replaced."""
    result = format_command("echo {issue_number}", issue_number=42)
    assert result == ["echo", "42"]


def test_choose_agent_override() -> None:
    """CLI override should take precedence."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    config = AppConfig()
    assert choose_agent(issue, config, "claude") == "claude"


def test_choose_agent_from_labels() -> None:
    """Issue labels should determine agent when override is auto."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=("agent/claude",))
    config = AppConfig()
    assert choose_agent(issue, config, "auto") == "claude"


def test_choose_agent_defaults_to_claude() -> None:
    """Default agent should be claude when no signals are present."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    config = AppConfig()
    assert choose_agent(issue, config, "auto") == "claude"


def test_run_agent_with_prompt_uses_claude_yolo_mode(tmp_path: Path) -> None:
    """Claude runner should bypass permission prompts for unattended execution."""
    fake_runner = FakeProcessRunner()

    run_agent_with_prompt("claude", "Implement the issue.", tmp_path, fake_runner)

    assert fake_runner.calls == [
        [
            "claude",
            "--dangerously-skip-permissions",
            "--verbose",
            "-p",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "Implement the issue.",
        ]
    ]


def test_run_agent_with_prompt_can_capture_output(tmp_path: Path) -> None:
    """Prepared agent runs should opt into captured stdout when needed."""
    command = (
        "codex",
        "--cd",
        str(tmp_path),
        "--sandbox",
        "workspace-write",
        "--config",
        "sandbox_workspace_write.network_access=true",
        "--ask-for-approval",
        "never",
        "exec",
        "Review.",
    )
    fake_runner = FakeProcessRunner(
        responses={
            command: CommandResult(
                command=command,
                return_code=0,
                stdout='{"verdict": "approved"}',
                stderr="",
            )
        }
    )

    uncaptured = run_agent_with_prompt("codex", "Review.", tmp_path, fake_runner)
    captured = run_agent_with_prompt(
        "codex",
        "Review.",
        tmp_path,
        fake_runner,
        capture_output=True,
    )

    assert uncaptured.stdout == ""
    assert captured.stdout == '{"verdict": "approved"}'


def test_run_agent_with_prompt_grants_codex_worktree_git_metadata(
    tmp_path: Path,
) -> None:
    """Codex 在 linked worktree 里必须能写主仓的 git 元数据目录。

    否则 lint flag 之类的 git 侧写入会因沙箱可写根不含 `.git/worktrees/<name>/`
    而报 `Operation not permitted`。
    """
    main_git_dir = tmp_path / "repo" / ".git"
    worktree_git_dir = main_git_dir / "worktrees" / "issue-1"
    worktree_git_dir.mkdir(parents=True)
    (worktree_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    worktree_path = tmp_path / "repo-worktrees" / "issue-1"
    worktree_path.mkdir(parents=True)
    (worktree_path / ".git").write_text(f"gitdir: {worktree_git_dir}\n", encoding="utf-8")
    fake_runner = FakeProcessRunner()

    run_agent_with_prompt("codex", "Implement.", worktree_path, fake_runner)

    command = fake_runner.calls[0]
    assert command[command.index("--add-dir") + 1] == str(worktree_git_dir.resolve())
    assert str(main_git_dir.resolve()) in command
    assert command.index("--add-dir") < command.index("exec")


def test_run_agent_with_prompt_omits_add_dir_for_plain_checkout(
    tmp_path: Path,
) -> None:
    """普通 checkout 的 `.git` 已在可写根内，不应追加多余的 `--add-dir`。"""
    (tmp_path / ".git").mkdir()
    fake_runner = FakeProcessRunner()

    run_agent_with_prompt("codex", "Implement.", tmp_path, fake_runner)

    assert "--add-dir" not in fake_runner.calls[0]


def test_run_agent_with_prompt_passes_timeout(tmp_path: Path) -> None:
    """Prepared agent runs should pass timeout through to the process runner."""
    fake_runner = FakeProcessRunner()

    run_agent_with_prompt(
        "codex",
        "Review.",
        tmp_path,
        fake_runner,
        capture_output=True,
        timeout_seconds=123,
        inactivity_timeout_seconds=45,
    )

    assert fake_runner.timeouts == [123]
    assert fake_runner.inactivity_timeouts == [45]


def test_run_agent_with_prompt_logs_issue_context(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When an issue is provided, run_agent_with_prompt logs the full URL and passes it as label."""
    issue = IssueSummary(
        number=23,
        title="Schema-Aware End-to-End Evaluation Framework",
        url="https://github.com/zata-zhangtao/fsense/issues/23",
        body="Body",
        labels=(),
    )
    fake_runner = FakeProcessRunner()

    with caplog.at_level(logging.INFO, logger="backend.core.use_cases.run_agent_once"):
        result = run_agent_with_prompt("claude", "Implement.", tmp_path, fake_runner, issue=issue)

    assert result.return_code == 0
    assert fake_runner.labels == ["Issue #23: https://github.com/zata-zhangtao/fsense/issues/23"]
    assert (
        "Starting agent for Issue #23: https://github.com/zata-zhangtao/fsense/issues/23"
        in caplog.text
    )
    assert (
        "Agent finished for Issue #23: https://github.com/zata-zhangtao/fsense/issues/23 (exit_code=0)"
        in caplog.text
    )


def test_extract_agent_response_text_from_claude_stream_json() -> None:
    """Captured Claude stream-json should be reduced to assistant text."""
    result = CommandResult(
        command=("claude", "--output-format", "stream-json", "-p", "Review."),
        return_code=0,
        stdout=(
            '{"type":"stream_event","event":{"delta":'
            '{"type":"text_delta","text":"```json\\n"}}}\n'
            '{"type":"stream_event","event":{"delta":'
            '{"type":"text_delta","text":"{\\"verdict\\": '
            '\\"approved\\"}\\n```"}}}\n'
        ),
        stderr="",
    )

    assert extract_agent_response_text(result) == ('```json\n{"verdict": "approved"}\n```')


def test_extract_agent_response_text_keeps_rendered_stream_output() -> None:
    """Already-rendered stream output must be returned verbatim.

    Process runner 会把 Claude stream-json 渲染成纯文本后返回；若再按事件流
    逐行重解析，数组末尾不带逗号的字符串元素恰好是合法 JSON 标量，会被静默
    丢弃并破坏 reviewer verdict JSON（回归自 Issue #53 pre-PR review 失败）。
    """
    rendered_stdout = (
        "```json\n"
        "{\n"
        '"verdict": "approved",\n'
        '"summary": "ok",\n'
        '"findings_medium": [\n'
        '"first finding with trailing comma",\n'
        '"last finding is a valid standalone JSON string"\n'
        "]\n"
        "}\n"
        "```\n"
    )
    result = CommandResult(
        command=("claude", "--output-format", "stream-json", "-p", "Review."),
        return_code=0,
        stdout=rendered_stdout,
        stderr="",
    )

    assert extract_agent_response_text(result) == rendered_stdout


class _SequencedAgentRunner(FakeProcessRunner):
    """Runner that yields a scripted sequence of outcomes per ``run`` call.

    Each outcome is either a :class:`CommandResult` to return or an exception to
    raise. The last outcome repeats once the sequence is exhausted.
    """

    def __init__(self, outcomes: list[object]) -> None:
        super().__init__()
        self._outcomes = list(outcomes)
        self._index = 0

    def run(
        self,
        command,
        *,
        cwd,
        check=True,
        timeout=None,
        capture_output=True,
        input_text=None,
        label=None,
    ):
        self.calls.append(list(command))
        outcome = self._outcomes[min(self._index, len(self._outcomes) - 1)]
        self._index += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_run_agent_with_prompt_resilient_retries_transient_then_succeeds(
    tmp_path: Path,
) -> None:
    """A transient agent error should be retried in place and then succeed."""
    success = CommandResult(("claude",), 0, "ok", "")
    runner = _SequencedAgentRunner([transient_command_error(), success])

    result = run_agent_with_prompt_resilient(
        "claude",
        "prompt",
        tmp_path,
        runner,
        transient_retry_attempts=2,
        transient_retry_delay_seconds=0,
    )

    assert result.return_code == 0
    assert len(runner.calls) == 2


def test_run_agent_with_prompt_resilient_raises_agent_unavailable(
    tmp_path: Path,
) -> None:
    """A missing agent CLI should surface as AgentUnavailableError without retry."""
    runner = _SequencedAgentRunner([FileNotFoundError("claude: command not found")])

    with pytest.raises(AgentUnavailableError):
        run_agent_with_prompt_resilient(
            "claude",
            "prompt",
            tmp_path,
            runner,
            transient_retry_attempts=2,
            transient_retry_delay_seconds=0,
        )

    assert len(runner.calls) == 1


def test_run_agent_with_prompt_resilient_propagates_non_transient(
    tmp_path: Path,
) -> None:
    """Non-transient errors must propagate immediately without retry."""
    error = CommandFailedError(1, ["claude"], output="some ordinary failure", stderr="")
    runner = _SequencedAgentRunner([error, CommandResult(("claude",), 0, "", "")])

    with pytest.raises(CommandFailedError):
        run_agent_with_prompt_resilient(
            "claude",
            "prompt",
            tmp_path,
            runner,
            transient_retry_attempts=2,
            transient_retry_delay_seconds=0,
        )

    assert len(runner.calls) == 1


def test_run_agent_with_prompt_resilient_does_not_retry_provider_capacity(
    tmp_path: Path,
) -> None:
    """Provider-capacity errors must not be retried in place (they escalate)."""
    error = CommandFailedError(
        1, ["claude"], output="Request rejected (429) usage limit reached", stderr=""
    )
    runner = _SequencedAgentRunner([error, CommandResult(("claude",), 0, "", "")])

    with pytest.raises(CommandFailedError):
        run_agent_with_prompt_resilient(
            "claude",
            "prompt",
            tmp_path,
            runner,
            transient_retry_attempts=2,
            transient_retry_delay_seconds=0,
        )

    assert len(runner.calls) == 1


def test_run_agent_with_prompt_resilient_reraises_after_exhausting_retries(
    tmp_path: Path,
) -> None:
    """A persistent transient error re-raises after exhausting retries."""
    runner = _SequencedAgentRunner([transient_command_error()] * 5)

    with pytest.raises(CommandFailedError):
        run_agent_with_prompt_resilient(
            "claude",
            "prompt",
            tmp_path,
            runner,
            transient_retry_attempts=2,
            transient_retry_delay_seconds=0,
        )

    # 1 initial attempt + 2 retries.
    assert len(runner.calls) == 3
