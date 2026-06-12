"""Clean up stale iAR issue worktrees and local branches."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner


DEFAULT_ISSUE_BRANCH_PREFIX = "issue-"
DEFAULT_WORKTREE_DIR_NAME = ".iar-worktrees"


class WorktreeCleanupStatus(str, Enum):
    """Outcome for a single branch considered by cleanup."""

    WOULD_DELETE = "would_delete"
    DELETED = "deleted"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class WorktreeCleanupRequest:
    """Options controlling stale iAR worktree cleanup."""

    repo_path: Path
    remote: str = "origin"
    base_branch: str = "main"
    dry_run: bool = True
    force: bool = False
    branch_prefix: str = DEFAULT_ISSUE_BRANCH_PREFIX
    managed_worktree_root_path: Path | None = None


@dataclass(frozen=True)
class IssueBranchCandidate:
    """Local branch that follows the configured iAR issue branch pattern."""

    branch: str
    issue_number: int


@dataclass(frozen=True)
class WorktreeCleanupBranchResult:
    """Cleanup decision for a single local issue branch."""

    branch: str
    issue_number: int
    status: WorktreeCleanupStatus
    reason: str
    worktree_path: Path | None = None


@dataclass(frozen=True)
class WorktreeCleanupResult:
    """Aggregated cleanup result."""

    branches: tuple[WorktreeCleanupBranchResult, ...]

    @property
    def deleted_count(self) -> int:
        """Return the number of branches actually deleted."""
        return self._count_status(WorktreeCleanupStatus.DELETED)

    @property
    def would_delete_count(self) -> int:
        """Return the number of branches that would be deleted in dry-run mode."""
        return self._count_status(WorktreeCleanupStatus.WOULD_DELETE)

    @property
    def skipped_count(self) -> int:
        """Return the number of skipped branches."""
        return self._count_status(WorktreeCleanupStatus.SKIPPED)

    @property
    def failed_count(self) -> int:
        """Return the number of branches that failed during deletion."""
        return self._count_status(WorktreeCleanupStatus.FAILED)

    def _count_status(self, status: WorktreeCleanupStatus) -> int:
        return sum(
            1 for branch_result in self.branches if branch_result.status is status
        )


def cleanup_iar_worktrees(
    request: WorktreeCleanupRequest,
    *,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> WorktreeCleanupResult:
    """Delete stale local issue branches whose GitHub Issue is closed.

    A branch is eligible only when all default safety checks pass:
    its name matches ``issue-<number>``, the corresponding remote branch is
    gone after ``git fetch --prune``, the GitHub Issue is closed, the worktree
    is iAR-managed, the worktree is clean, and the branch is merged into the
    configured remote base branch. ``force`` bypasses the dirty and merged
    checks, but still requires a closed Issue and missing remote branch.
    """
    repo_path = request.repo_path.resolve()
    managed_worktree_root_path = (
        request.managed_worktree_root_path
        if request.managed_worktree_root_path is not None
        else repo_path / DEFAULT_WORKTREE_DIR_NAME
    ).resolve()

    _fetch_pruned_remote(repo_path, request.remote, process_runner)
    current_branch = _current_branch(repo_path, process_runner)
    branch_worktree_paths = _branch_worktree_paths(repo_path, process_runner)
    local_issue_branches = _local_issue_branches(
        repo_path, request.branch_prefix, process_runner
    )

    cleanup_results = [
        _evaluate_issue_branch(
            candidate,
            request=request,
            repo_path=repo_path,
            managed_worktree_root_path=managed_worktree_root_path,
            current_branch=current_branch,
            branch_worktree_paths=branch_worktree_paths,
            github_client=github_client,
            process_runner=process_runner,
        )
        for candidate in local_issue_branches
    ]
    return WorktreeCleanupResult(branches=tuple(cleanup_results))


def _evaluate_issue_branch(
    candidate: IssueBranchCandidate,
    *,
    request: WorktreeCleanupRequest,
    repo_path: Path,
    managed_worktree_root_path: Path,
    current_branch: str,
    branch_worktree_paths: dict[str, Path],
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> WorktreeCleanupBranchResult:
    branch = candidate.branch
    issue_number = candidate.issue_number
    candidate_worktree_path = branch_worktree_paths.get(branch)

    if branch == current_branch:
        return _skipped(
            candidate, "branch is currently checked out", candidate_worktree_path
        )

    if _remote_branch_exists(repo_path, request.remote, branch, process_runner):
        return _skipped(
            candidate,
            f"remote branch {request.remote}/{branch} still exists",
            candidate_worktree_path,
        )

    issue_closed, issue_reason = _issue_is_closed(github_client, issue_number)
    if not issue_closed:
        return _skipped(candidate, issue_reason, candidate_worktree_path)

    if candidate_worktree_path is not None and not _path_is_inside(
        candidate_worktree_path, managed_worktree_root_path
    ):
        return _skipped(
            candidate,
            f"branch is checked out outside {managed_worktree_root_path}",
            candidate_worktree_path,
        )

    if (
        candidate_worktree_path is not None
        and not request.force
        and _worktree_has_changes(candidate_worktree_path, process_runner)
    ):
        return _skipped(
            candidate,
            "worktree has uncommitted changes; use --force to delete",
            candidate_worktree_path,
        )

    if not request.force and not _branch_is_effectively_merged(
        repo_path,
        branch,
        remote=request.remote,
        base_branch=request.base_branch,
        github_client=github_client,
        process_runner=process_runner,
    ):
        return _skipped(
            candidate,
            f"branch is not merged into {request.remote}/{request.base_branch} "
            "and no merged PR was found; use --force to delete",
            candidate_worktree_path,
        )

    if request.dry_run:
        return WorktreeCleanupBranchResult(
            branch=branch,
            issue_number=issue_number,
            status=WorktreeCleanupStatus.WOULD_DELETE,
            reason="Issue is closed and remote branch is gone",
            worktree_path=candidate_worktree_path,
        )

    return _delete_issue_branch(
        candidate,
        repo_path=repo_path,
        worktree_path=candidate_worktree_path,
        remote=request.remote,
        base_branch=request.base_branch,
        force=request.force,
        process_runner=process_runner,
    )


def _fetch_pruned_remote(
    repo_path: Path, remote: str, process_runner: IProcessRunner
) -> None:
    process_runner.run(["git", "fetch", remote, "--prune"], cwd=repo_path)


def _current_branch(repo_path: Path, process_runner: IProcessRunner) -> str:
    current_branch_result = process_runner.run(
        ["git", "branch", "--show-current"], cwd=repo_path
    )
    return current_branch_result.stdout.strip()


def _local_issue_branches(
    repo_path: Path, branch_prefix: str, process_runner: IProcessRunner
) -> tuple[IssueBranchCandidate, ...]:
    branch_list_result = process_runner.run(
        ["git", "branch", "--format", "%(refname:short)"], cwd=repo_path
    )
    branch_pattern = re.compile(rf"^{re.escape(branch_prefix)}(?P<issue_number>\d+)$")
    branch_candidates: list[IssueBranchCandidate] = []
    for branch_line in branch_list_result.stdout.splitlines():
        branch_name = branch_line.strip()
        branch_match = branch_pattern.fullmatch(branch_name)
        if branch_match is None:
            continue
        branch_candidates.append(
            IssueBranchCandidate(
                branch=branch_name,
                issue_number=int(branch_match.group("issue_number")),
            )
        )
    return tuple(branch_candidates)


def _branch_worktree_paths(
    repo_path: Path, process_runner: IProcessRunner
) -> dict[str, Path]:
    worktree_list_result = process_runner.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo_path
    )
    branch_paths: dict[str, Path] = {}
    current_worktree_path: Path | None = None
    for worktree_line in worktree_list_result.stdout.splitlines():
        if worktree_line.startswith("worktree "):
            current_worktree_path = Path(
                worktree_line.removeprefix("worktree ")
            ).resolve()
            continue
        if worktree_line.startswith("branch refs/heads/") and current_worktree_path:
            branch_name = worktree_line.removeprefix("branch refs/heads/").strip()
            branch_paths[branch_name] = current_worktree_path
            continue
        if not worktree_line.strip():
            current_worktree_path = None
    return branch_paths


def _remote_branch_exists(
    repo_path: Path,
    remote: str,
    branch: str,
    process_runner: IProcessRunner,
) -> bool:
    remote_ref_result = process_runner.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"],
        cwd=repo_path,
        check=False,
    )
    return remote_ref_result.return_code == 0


def _issue_is_closed(
    github_client: IGitHubClient, issue_number: int
) -> tuple[bool, str]:
    try:
        issue_summary = github_client.get_issue(issue_number)
    except Exception as exc:  # noqa: BLE001 - cleanup should continue per branch.
        return False, f"Issue #{issue_number} lookup failed: {exc}"

    issue_state = str(getattr(issue_summary, "state", "OPEN") or "OPEN").lower()
    if issue_state == "closed":
        return True, "Issue is closed"
    return False, f"Issue #{issue_number} is {issue_state.upper()}"


def _worktree_has_changes(worktree_path: Path, process_runner: IProcessRunner) -> bool:
    status_result = process_runner.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
    )
    return bool(status_result.stdout.strip())


def _branch_is_ancestor_merged(
    repo_path: Path,
    branch: str,
    *,
    remote: str,
    base_branch: str,
    process_runner: IProcessRunner,
) -> bool:
    """Return whether ``branch`` is an ancestor of the remote base branch."""
    merge_base_result = process_runner.run(
        ["git", "merge-base", "--is-ancestor", branch, f"{remote}/{base_branch}"],
        cwd=repo_path,
        check=False,
    )
    return merge_base_result.return_code == 0


def _branch_is_effectively_merged(
    repo_path: Path,
    branch: str,
    *,
    remote: str,
    base_branch: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> bool:
    """Return whether the branch is safe to delete as already merged.

    A regular merge is detected via git ancestry. Squash and rebase merges do
    not make the source branch an ancestor of the base branch, so we fall back
    to asking GitHub whether a PR from this branch has been merged.
    """
    if _branch_is_ancestor_merged(
        repo_path,
        branch,
        remote=remote,
        base_branch=base_branch,
        process_runner=process_runner,
    ):
        return True
    return github_client.find_merged_pr_by_head(branch) is not None


def _delete_issue_branch(
    candidate: IssueBranchCandidate,
    *,
    repo_path: Path,
    worktree_path: Path | None,
    remote: str,
    base_branch: str,
    force: bool,
    process_runner: IProcessRunner,
) -> WorktreeCleanupBranchResult:
    if worktree_path is not None:
        remove_command = ["git", "worktree", "remove"]
        if force:
            remove_command.append("--force")
        remove_worktree_result = process_runner.run(
            [*remove_command, str(worktree_path)],
            cwd=repo_path,
            check=False,
        )
        if remove_worktree_result.return_code != 0:
            return _failed(
                candidate,
                _command_failure_reason("worktree remove", remove_worktree_result),
                worktree_path,
            )
        prune_result = process_runner.run(
            ["git", "worktree", "prune"],
            cwd=repo_path,
            check=False,
        )
        if prune_result.return_code != 0:
            return _failed(
                candidate,
                _command_failure_reason("worktree prune", prune_result),
                worktree_path,
            )

    ancestry_merged = _branch_is_ancestor_merged(
        repo_path,
        candidate.branch,
        remote=remote,
        base_branch=base_branch,
        process_runner=process_runner,
    )
    branch_delete_flag = "-d" if ancestry_merged and not force else "-D"
    delete_branch_result = process_runner.run(
        ["git", "branch", branch_delete_flag, candidate.branch],
        cwd=repo_path,
        check=False,
    )
    if delete_branch_result.return_code != 0:
        return _failed(
            candidate,
            _command_failure_reason("branch delete", delete_branch_result),
            worktree_path,
        )
    return WorktreeCleanupBranchResult(
        branch=candidate.branch,
        issue_number=candidate.issue_number,
        status=WorktreeCleanupStatus.DELETED,
        reason="Issue is closed and remote branch is gone",
        worktree_path=worktree_path,
    )


def _command_failure_reason(command_name: str, command_result: object) -> str:
    stdout_text = str(getattr(command_result, "stdout", "") or "").strip()
    stderr_text = str(getattr(command_result, "stderr", "") or "").strip()
    detail_text = stderr_text or stdout_text or "no command output"
    return f"{command_name} failed: {detail_text}"


def _skipped(
    candidate: IssueBranchCandidate, reason: str, worktree_path: Path | None
) -> WorktreeCleanupBranchResult:
    return WorktreeCleanupBranchResult(
        branch=candidate.branch,
        issue_number=candidate.issue_number,
        status=WorktreeCleanupStatus.SKIPPED,
        reason=reason,
        worktree_path=worktree_path,
    )


def _failed(
    candidate: IssueBranchCandidate, reason: str, worktree_path: Path | None
) -> WorktreeCleanupBranchResult:
    return WorktreeCleanupBranchResult(
        branch=candidate.branch,
        issue_number=candidate.issue_number,
        status=WorktreeCleanupStatus.FAILED,
        reason=reason,
        worktree_path=worktree_path,
    )


def _path_is_inside(path: Path, parent_path: Path) -> bool:
    resolved_path = path.resolve()
    resolved_parent_path = parent_path.resolve()
    try:
        resolved_path.relative_to(resolved_parent_path)
        return True
    except ValueError:
        return False
