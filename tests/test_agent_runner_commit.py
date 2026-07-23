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


class _PrecommitVerificationRunner(FakeProcessRunner):
    """Fake runner returning a sequence of return codes for the pre-commit command.

    Drives the pre-commit verification autofix retry: the first run exits
    non-zero (a hook rewrote files), the runner re-stages, and a later run
    passes or keeps failing. All other commands fall back to ``responses``.
    """

    def __init__(self, *, pre_commit_return_codes: list[int], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._pre_commit_return_codes = pre_commit_return_codes
        self._pre_commit_attempts = 0

    def run(self, command, **kwargs):  # type: ignore[override]
        command_list = list(command)
        is_pre_commit = (
            len(command_list) == 3
            and command_list[:2] == ["bash", "-lc"]
            and "pre-commit run" in command_list[2]
        )
        if not is_pre_commit:
            return super().run(command, **kwargs)
        # 记录调用（返回值丢弃），再按序列覆盖退出码模拟 autofix 钩子。
        super().run(command, **kwargs)
        index = min(self._pre_commit_attempts, len(self._pre_commit_return_codes) - 1)
        return_code = self._pre_commit_return_codes[index]
        self._pre_commit_attempts += 1
        return CommandResult(
            command=tuple(command),
            return_code=return_code,
            stdout="",
            stderr="1 file reformatted\n" if return_code != 0 else "",
        )


def _pre_commit_autofix_responses(diff_quiet_rc: int) -> dict[tuple[str, ...], CommandResult]:
    """Return base command responses shared by the pre-commit autofix retry tests."""
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

    fake_runner = _PrecommitVerificationRunner(
        pre_commit_return_codes=[1, 0],
        responses=_pre_commit_autofix_responses(diff_quiet_rc=1),
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

    fake_runner = _PrecommitVerificationRunner(
        pre_commit_return_codes=[1, 1],
        responses=_pre_commit_autofix_responses(diff_quiet_rc=1),
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
