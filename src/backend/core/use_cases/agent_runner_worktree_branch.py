"""Worktree branch healing and remote reconciliation for the agent runner."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner
from backend.core.shared.models.agent_runner import AppConfig, IssueSummary
from backend.core.use_cases.agent_runner_commit import (
    read_commit_request,
    remove_commit_request,
)
from backend.core.use_cases.agent_runner_feedback import ensure_verification_passed
from backend.core.use_cases.agent_runner_publish import validate_safe_changes
from backend.core.use_cases.agent_runner_git import (
    get_active_rebase_target,
    get_current_branch,
    get_head_sha,
    has_rebase_metadata,
    has_changes,
    is_detached_head,
    run_verification,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "_ensure_worktree_branch",
    "_reconcile_worktree_with_remote_branch",
]


def _ensure_worktree_branch(
    worktree_path: Path,
    expected_branch: str,
    issue: IssueSummary,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> None:
    """Make sure the worktree is on the expected branch, self-healing when safe.

    A worktree can end up in detached HEAD because of a concurrent or aborted
    rebase (e.g. the post-PR supervisor rebased while the main runner claimed
    the Issue). This helper recovers automatically:

    - Active rebase: try ``git rebase --continue`` when there are no conflicts;
      when conflicts exist, ask the configured agent to resolve them and then
      continue the rebase. If the agent cannot resolve the conflicts after the
      configured number of attempts, abort the rebase and checkout the recorded
      target branch so the runner can still proceed.
    - Detached HEAD without active rebase: move the expected branch to the
      current detached HEAD when the move is a fast-forward or the branch does
      not exist yet. If the branch and HEAD have diverged, raise a clear error
      so a human can decide which history to keep.
    """
    if not is_detached_head(worktree_path, process_runner):
        return

    rebase_target = get_active_rebase_target(worktree_path, process_runner)
    if rebase_target is not None:
        if rebase_target != expected_branch:
            raise RuntimeError(
                f"Worktree {worktree_path} is in an active rebase for branch "
                f"'{rebase_target}', but Issue #{issue.number} expects "
                f"'{expected_branch}'. Manual reconciliation is required."
            )
        _recover_from_active_rebase(worktree_path, rebase_target, issue, config, process_runner)
        return
    if has_rebase_metadata(worktree_path, process_runner):
        raise RuntimeError(
            f"Worktree {worktree_path} is in an active rebase, but the target "
            f"branch cannot be confirmed for Issue #{issue.number}. Manual "
            "reconciliation is required."
        )

    _attach_branch_to_detached_head(worktree_path, expected_branch, process_runner)


def _recover_from_active_rebase(
    worktree_path: Path,
    rebase_target: str,
    issue: IssueSummary,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> None:
    """Leave an active rebase in a clean checkout of ``rebase_target``.

    Reuses the same agent-driven conflict resolution strategy as the post-PR
    supervisor: when ``git rebase --continue`` cannot proceed because of
    conflicts, the configured agent is prompted to resolve them and write a
    commit request. The runner stages the resolved files, verifies them, and
    continues the rebase. Only after exhausting the configured repair attempts
    do we fall back to ``git rebase --abort``.
    """
    from backend.core.use_cases.pr_supervisor import build_conflict_resolution_prompt

    def _try_continue_rebase() -> bool:
        continue_result = process_runner.run(
            ["git", "-c", "core.editor=true", "rebase", "--continue"],
            cwd=worktree_path,
            check=False,
        )
        return continue_result.return_code == 0

    def _conflicted_files() -> list[str]:
        diff_names_result = process_runner.run(
            ["git", "diff", "--name-only", "--diff-filter", "U"],
            cwd=worktree_path,
            check=False,
        )
        return [line.strip() for line in diff_names_result.stdout.splitlines() if line.strip()]

    if not _conflicted_files() and _try_continue_rebase():
        return

    max_attempts = max(0, config.post_pr_supervisor.max_repair_attempts)
    for _attempt in range(1, max_attempts + 1):
        conflicted = _conflicted_files()
        if not conflicted:
            if _try_continue_rebase():
                return
            break

        prompt = build_conflict_resolution_prompt(
            issue,
            rebase_target,
            get_head_sha(worktree_path, process_runner),
            conflicted,
        )

        # Avoid a circular import: run_agent_once imports this module, and the
        # agent helpers in turn import pr_supervisor.
        from backend.core.use_cases.run_agent_once import (
            choose_agent,
            run_agent_with_prompt,
        )

        agent_name = choose_agent(issue, config, "auto")
        try:
            run_agent_with_prompt(agent_name, prompt, worktree_path, process_runner, issue=issue)
        except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
            _logger.warning(
                "Agent conflict resolution attempt %d/%d failed for Issue #%d: %s",
                _attempt,
                max_attempts,
                issue.number,
                exc,
            )
            continue

        request_path = worktree_path / ".agent-runner" / "commit-request.json"
        if not request_path.is_file():
            continue
        commit_message = read_commit_request(worktree_path, issue)
        remove_commit_request(worktree_path)
        if not has_changes(worktree_path, process_runner):
            raise RuntimeError("Agent requested a commit but produced no file changes.")
        validate_safe_changes(worktree_path, config, process_runner)
        process_runner.run(["git", "add", "-A"], cwd=worktree_path)
        verification_results = run_verification(worktree_path, config, process_runner)
        ensure_verification_passed(verification_results)
        process_runner.run(
            ["git", "commit", "-m", commit_message],
            cwd=worktree_path,
        )
        if _try_continue_rebase():
            return

    process_runner.run(["git", "rebase", "--abort"], cwd=worktree_path, check=False)
    if is_detached_head(worktree_path, process_runner):
        process_runner.run(["git", "checkout", rebase_target], cwd=worktree_path)


def _attach_branch_to_detached_head(
    worktree_path: Path,
    expected_branch: str,
    process_runner: IProcessRunner,
) -> None:
    """Move ``expected_branch`` to the detached HEAD when it is safe to do so."""
    detached_sha = get_head_sha(worktree_path, process_runner)
    branch_ref = f"refs/heads/{expected_branch}"
    branch_exists_result = process_runner.run(
        ["git", "show-ref", "--verify", "--quiet", branch_ref],
        cwd=worktree_path,
        check=False,
    )

    if branch_exists_result.return_code != 0:
        process_runner.run(
            ["git", "checkout", "-b", expected_branch],
            cwd=worktree_path,
        )
        return

    branch_sha = _rev_parse(worktree_path, branch_ref, process_runner)
    if detached_sha == branch_sha:
        process_runner.run(["git", "checkout", expected_branch], cwd=worktree_path)
        return

    branch_is_ancestor = _is_ancestor(worktree_path, branch_sha, detached_sha, process_runner)
    if branch_is_ancestor:
        process_runner.run(
            ["git", "checkout", "-B", expected_branch, detached_sha],
            cwd=worktree_path,
        )
        return

    raise RuntimeError(
        f"Worktree {worktree_path} is in detached HEAD and the expected branch "
        f"'{expected_branch}' has diverged from HEAD ({detached_sha[:8]}). "
        "Manual reconciliation is required to decide which history to keep."
    )


def _remote_branch_exists(
    worktree_path: Path,
    remote: str,
    branch: str,
    process_runner: IProcessRunner,
) -> bool:
    """Check whether a branch exists on the configured remote."""
    result = process_runner.run(
        ["git", "ls-remote", "--heads", remote, branch],
        cwd=worktree_path,
        check=False,
    )
    return result.return_code == 0 and bool(result.stdout.strip())


def _fetch_remote_branch(
    worktree_path: Path,
    remote: str,
    branch: str,
    process_runner: IProcessRunner,
) -> None:
    """Fetch a single remote branch into its remote-tracking ref."""
    process_runner.run(
        ["git", "fetch", remote, f"+{branch}:refs/remotes/{remote}/{branch}"],
        cwd=worktree_path,
    )


def _is_ancestor(
    worktree_path: Path,
    ancestor_sha: str,
    descendant_ref: str,
    process_runner: IProcessRunner,
) -> bool:
    """Return whether ancestor_sha is an ancestor of descendant_ref."""
    result = process_runner.run(
        ["git", "merge-base", "--is-ancestor", ancestor_sha, descendant_ref],
        cwd=worktree_path,
        check=False,
    )
    return result.return_code == 0


def _rev_parse(
    worktree_path: Path,
    ref: str,
    process_runner: IProcessRunner,
) -> str:
    """Return the full SHA for a ref."""
    result = process_runner.run(["git", "rev-parse", ref], cwd=worktree_path)
    return result.stdout.strip()


def _reconcile_worktree_with_remote_branch(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> None:
    """Safely reconcile the current worktree branch with its remote branch.

    - Remote branch missing: no-op.
    - Already in sync: no-op.
    - Local behind remote with clean worktree: fast-forward.
    - Local behind remote with dirty worktree: fail without reset.
    - Local ahead of remote: preserve local commits.
    - Diverged: fail and request manual reconciliation.
    """
    branch = get_current_branch(worktree_path, process_runner)
    remote = config.git.remote
    remote_ref = f"{remote}/{branch}"

    if not _remote_branch_exists(worktree_path, remote, branch, process_runner):
        return

    _fetch_remote_branch(worktree_path, remote, branch, process_runner)

    local_sha = get_head_sha(worktree_path, process_runner)
    remote_sha = _rev_parse(worktree_path, remote_ref, process_runner)
    if local_sha == remote_sha:
        return

    local_is_ancestor = _is_ancestor(worktree_path, local_sha, remote_ref, process_runner)
    remote_is_ancestor = _is_ancestor(worktree_path, remote_sha, "HEAD", process_runner)

    if local_is_ancestor:
        if has_changes(worktree_path, process_runner):
            raise RuntimeError(
                f"Worktree branch {branch} is behind {remote_ref}, "
                "but the worktree has uncommitted changes."
            )
        process_runner.run(["git", "merge", "--ff-only", remote_ref], cwd=worktree_path)
        return

    if remote_is_ancestor:
        return

    raise RuntimeError(
        f"Worktree branch {branch} has diverged from {remote_ref}; "
        "manual reconciliation is required."
    )
