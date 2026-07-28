"""Tests for the agent runner commit proxy."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    RunnerConfig,
)
from backend.core.use_cases.agent_runner_commit import commit_requested_changes
from backend.core.use_cases.agent_runner_feedback import VerificationFailedError
from tests.conftest import FakeProcessRunner


def _make_issue(number: int = 123) -> IssueSummary:
    return IssueSummary(
        number=number,
        title="Example",
        url=f"https://github.com/example/repo/issues/{number}",
        body="Example body",
        labels=(),
    )


def _write_commit_request(worktree_path: Path, commit_message: str) -> None:
    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(f'{{"commit_message": "{commit_message}"}}\n', encoding="utf-8")


def test_commit_requested_changes_raises_on_verification_failure(
    tmp_path: Path,
) -> None:
    """Verification failures should raise VerificationFailedError."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                ("git", "branch", "--show-current"), 0, "issue-123\n", ""
            ),
            ("git", "status", "--porcelain"): CommandResult(
                ("git", "status", "--porcelain"), 0, " M src/example.py\n", ""
            ),
            ("ruff", "check"): CommandResult(
                ("ruff", "check"),
                1,
                "src/example.py:1:1: E501 Line too long\n",
                "",
            ),
        }
    )
    config = AppConfig(runner=RunnerConfig(verification_commands=("ruff check",)))

    with pytest.raises(VerificationFailedError):
        commit_requested_changes(
            _make_issue(),
            worktree_path,
            config,
            fake_runner,
            expected_branch="issue-123",
        )


def test_commit_requested_changes_runs_pre_commit_verification_command(
    tmp_path: Path,
) -> None:
    """Configured pre-commit verification command runs after staging and before commit."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                ("git", "branch", "--show-current"), 0, "issue-123\n", ""
            ),
            ("git", "status", "--porcelain"): CommandResult(
                ("git", "status", "--porcelain"), 0, " M src/example.py\n", ""
            ),
            ("pre-commit", "run", "--all-files"): CommandResult(
                ("pre-commit", "run", "--all-files"), 0, "All checks passed\n", ""
            ),
        }
    )
    config = AppConfig(
        runner=RunnerConfig(
            verification_commands=(),
            pre_commit_verification_command="pre-commit run --all-files",
        )
    )

    commit_requested_changes(
        _make_issue(),
        worktree_path,
        config,
        fake_runner,
        expected_branch="issue-123",
    )

    assert ["pre-commit", "run", "--all-files"] in fake_runner.calls


def test_commit_requested_changes_raises_when_pre_commit_verification_fails(
    tmp_path: Path,
) -> None:
    """A real check failure (no autofix rewrite) still raises VerificationFailedError.

    ``git diff --quiet`` reports a clean tree after the non-zero pre-commit run,
    so there is nothing to re-stage: the failure is a genuine lint/check error
    and must surface to the Fix Agent rather than being retried.
    """
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                ("git", "branch", "--show-current"), 0, "issue-123\n", ""
            ),
            ("git", "status", "--porcelain"): CommandResult(
                ("git", "status", "--porcelain"), 0, " M src/example.py\n", ""
            ),
            ("git", "diff", "--quiet"): CommandResult(("git", "diff", "--quiet"), 0, "", ""),
            ("pre-commit", "run", "--all-files"): CommandResult(
                ("pre-commit", "run", "--all-files"),
                1,
                "",
                "check-test-flag failed\n",
            ),
        }
    )
    config = AppConfig(
        runner=RunnerConfig(
            verification_commands=(),
            pre_commit_verification_command="pre-commit run --all-files",
        )
    )

    with pytest.raises(VerificationFailedError) as exc_info:
        commit_requested_changes(
            _make_issue(),
            worktree_path,
            config,
            fake_runner,
            expected_branch="issue-123",
        )

    failed_results = exc_info.value.verification_results
    assert len(failed_results) == 1
    assert failed_results[0].return_code == 1
    assert "check-test-flag failed" in failed_results[0].stderr


class _SequencedGateRunner(FakeProcessRunner):
    """Fake runner returning a scripted return-code sequence for one gate command.

    Drives the commit gate autofix retry: the first run of the matching command
    exits non-zero (a hook rewrote files), the runner re-stages, and a later run
    passes or keeps failing. All other commands fall back to ``responses``.
    Both commit gates（``verification_commands`` 与
    ``pre_commit_verification_command``）走同一条 ``bash -lc`` 包装路径，故用
    命令片段匹配复用同一个假 runner。
    """

    def __init__(
        self,
        *,
        gate_command_fragment: str,
        gate_return_codes: list[int],
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._gate_command_fragment = gate_command_fragment
        self._gate_return_codes = gate_return_codes
        self._gate_attempts = 0

    def run(self, command, **kwargs):  # type: ignore[override]
        command_list = list(command)
        is_gate_command = (
            len(command_list) == 3
            and command_list[:2] == ["bash", "-lc"]
            and self._gate_command_fragment in command_list[2]
        )
        if not is_gate_command:
            return super().run(command, **kwargs)
        # 记录调用（返回值丢弃），再按序列覆盖退出码模拟 autofix 钩子。
        super().run(command, **kwargs)
        index = min(self._gate_attempts, len(self._gate_return_codes) - 1)
        return_code = self._gate_return_codes[index]
        self._gate_attempts += 1
        return CommandResult(
            command=tuple(command),
            return_code=return_code,
            stdout="",
            stderr="1 file reformatted\n" if return_code != 0 else "",
        )


def _autofix_gate_responses(diff_quiet_rc: int) -> dict[tuple[str, ...], CommandResult]:
    """Return base command responses shared by the commit gate autofix retry tests."""
    return {
        ("git", "branch", "--show-current"): CommandResult(
            ("git", "branch", "--show-current"), 0, "issue-123\n", ""
        ),
        ("git", "status", "--porcelain"): CommandResult(
            ("git", "status", "--porcelain"), 0, " M src/example.py\n", ""
        ),
        ("git", "diff", "--quiet"): CommandResult(
            ("git", "diff", "--quiet"), diff_quiet_rc, "", ""
        ),
    }


def test_commit_requested_changes_retries_pre_commit_verification_after_autofix(
    tmp_path: Path,
) -> None:
    """An autofix hook (ruff-format) that rewrites files must not fail the commit.

    The first pre-commit run exits non-zero after rewriting a tracked file
    (``git diff --quiet`` reports changes), so the runner re-stages with
    ``git add -u`` and re-runs pre-commit, which now passes.
    """
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")

    fake_runner = _SequencedGateRunner(
        gate_command_fragment="pre-commit run",
        gate_return_codes=[1, 0],
        responses=_autofix_gate_responses(diff_quiet_rc=1),
    )
    config = AppConfig(
        runner=RunnerConfig(
            verification_commands=(),
            pre_commit_verification_command="pre-commit run --all-files",
        )
    )

    commit_requested_changes(
        _make_issue(),
        worktree_path,
        config,
        fake_runner,
        expected_branch="issue-123",
    )

    pre_commit_calls = [
        call for call in fake_runner.calls if call == ["pre-commit", "run", "--all-files"]
    ]
    assert len(pre_commit_calls) == 2
    assert ["git", "add", "-u"] in fake_runner.calls


def test_commit_requested_changes_raises_when_pre_commit_autofix_does_not_resolve(
    tmp_path: Path,
) -> None:
    """A hook that keeps failing after re-staging must still raise (a real error)."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")

    fake_runner = _SequencedGateRunner(
        gate_command_fragment="pre-commit run",
        gate_return_codes=[1, 1],
        responses=_autofix_gate_responses(diff_quiet_rc=1),
    )
    config = AppConfig(
        runner=RunnerConfig(
            verification_commands=(),
            pre_commit_verification_command="pre-commit run --all-files",
        )
    )

    with pytest.raises(VerificationFailedError):
        commit_requested_changes(
            _make_issue(),
            worktree_path,
            config,
            fake_runner,
            expected_branch="issue-123",
        )

    pre_commit_calls = [
        call for call in fake_runner.calls if call == ["pre-commit", "run", "--all-files"]
    ]
    assert len(pre_commit_calls) == 2


def test_commit_requested_changes_retries_verification_commands_after_autofix(
    tmp_path: Path,
) -> None:
    """``verification_commands`` 触发的 autofix 改写同样要重试，而不是直接判死。

    freshai Issue #96 的实证路径：``verification_commands`` 里的 ``just test``
    内部跑 ``pre-commit run --all-files``，ruff-format 重写了一个跟踪文件后以非零
    码退出。该门禁排在 ``pre_commit_verification_command`` 之前，若没有同样的重新
    stage 重试，纯格式化失败会在这里就把 Issue 打成 ``agent/failed``。
    """
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")

    fake_runner = _SequencedGateRunner(
        gate_command_fragment="just test",
        gate_return_codes=[1, 0],
        responses=_autofix_gate_responses(diff_quiet_rc=1),
    )
    config = AppConfig(runner=RunnerConfig(verification_commands=("just test",)))

    commit_requested_changes(
        _make_issue(),
        worktree_path,
        config,
        fake_runner,
        expected_branch="issue-123",
    )

    verification_calls = [call for call in fake_runner.calls if call == ["just", "test"]]
    assert len(verification_calls) == 2
    assert ["git", "add", "-u"] in fake_runner.calls


def test_commit_requested_changes_raises_when_verification_autofix_does_not_resolve(
    tmp_path: Path,
) -> None:
    """重新 stage 后仍失败的 ``verification_commands`` 是真实错误，必须上抛。"""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")

    fake_runner = _SequencedGateRunner(
        gate_command_fragment="just test",
        gate_return_codes=[1, 1],
        responses=_autofix_gate_responses(diff_quiet_rc=1),
    )
    config = AppConfig(runner=RunnerConfig(verification_commands=("just test",)))

    with pytest.raises(VerificationFailedError):
        commit_requested_changes(
            _make_issue(),
            worktree_path,
            config,
            fake_runner,
            expected_branch="issue-123",
        )

    verification_calls = [call for call in fake_runner.calls if call == ["just", "test"]]
    assert len(verification_calls) == 2
