"""Publishing and safety validation for the agent runner."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import AppConfig, IssueSummary
from backend.core.use_cases.agent_runner_feedback import (
    assert_prd_archived_for_publish,
)
from backend.core.use_cases.agent_runner_git import (
    get_current_branch,
    list_changed_paths,
    list_git_remotes,
)
from backend.core.use_cases.agent_runner_validation import (
    build_validation_checklist_block,
    ensure_no_evidence_paths_in_changes,
    extract_realistic_validation_items,
    validation_required,
)
from backend.core.use_cases.generated_content import (
    build_pr_context,
    generate_pr_content,
)


class DraftPRCreationError(RuntimeError):
    """Raised when draft PR creation fails after a successful push."""

    pass


class PushChangesError(RuntimeError):
    """Raised when pushing the branch to remote fails."""

    pass


__all__ = [
    "DraftPRCreationError",
    "PushChangesError",
    "create_draft_pr",
    "is_forbidden_path",
    "publish_changes",
    "push_changes",
    "run_preflight_checks",
    "validate_publish_remote",
    "validate_safe_changes",
]


def is_forbidden_path(changed_path_text: str, config: AppConfig) -> bool:
    """Whether a changed path matches any configured forbidden pattern.

    Matches both the full repo-relative path and its basename against
    ``config.safety.forbidden_path_patterns`` (fnmatch), so a pattern like
    ``.env`` blocks ``.env`` anywhere in the tree.
    """
    changed_path_name = Path(changed_path_text).name
    for forbidden_pattern in config.safety.forbidden_path_patterns:
        if fnmatch(changed_path_text, forbidden_pattern) or fnmatch(
            changed_path_name,
            forbidden_pattern,
        ):
            return True
    return False


def validate_safe_changes(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> None:
    """Refuse to publish changes to configured forbidden paths."""
    blocked_paths = [
        changed_path_text
        for changed_path_text in list_changed_paths(worktree_path, process_runner)
        if is_forbidden_path(changed_path_text, config)
    ]
    if blocked_paths:
        blocked_paths_text = ", ".join(sorted(set(blocked_paths)))
        raise RuntimeError(f"Refusing to publish forbidden paths: {blocked_paths_text}")


def validate_publish_remote(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> str:
    """Return the configured publish remote after confirming it exists."""
    remote_names = list_git_remotes(worktree_path, process_runner)
    configured_remote_name = config.git.remote
    if configured_remote_name in remote_names:
        return configured_remote_name

    available_remotes_text = ", ".join(remote_names) if remote_names else "(none)"
    raise RuntimeError(
        "Configured git remote "
        f"'{configured_remote_name}' does not exist. "
        f"Available remotes: {available_remotes_text}. "
        "Update [agent_runner.git].remote in .iar.toml or config.toml before publishing."
    )


def run_preflight_checks(
    repo_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> None:
    """Validate runner configuration before claiming any Issue."""
    validate_publish_remote(repo_path, config, process_runner)


def _validate_branch_for_publish(
    worktree_path: Path,
    process_runner: IProcessRunner,
    *,
    expected_branch: str | None,
    issue: IssueSummary,
) -> str:
    """Resolve the current branch and verify it matches ``expected_branch``.

    Shared by :func:`push_changes` and :func:`create_draft_pr` to keep the
    branch drift guard consistent with the legacy :func:`publish_changes`
    behavior.
    """
    branch = get_current_branch(worktree_path, process_runner)
    if not branch:
        raise RuntimeError("Refusing to publish: worktree is in detached HEAD state.")
    if expected_branch is not None and branch != expected_branch:
        raise RuntimeError(
            f"Refusing to publish from unexpected branch: {branch} (expected {expected_branch})"
        )
    return branch


def push_changes(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    *,
    expected_branch: str | None = None,
    require_prd_archived: bool = True,
) -> str:
    """Push the current branch to the configured remote.

    Pre-push safety checks (PRD archive assertion, forbidden path scan, evidence
    path exclusion, remote validation) are run in the same order as the legacy
    :func:`publish_changes` so the on-disk guarantees are unchanged.

    Args:
        issue: Issue being published (used for PRD archive assertions).
        worktree_path: Agent worktree path.
        config: Agent Runner configuration.
        process_runner: Command runner.
        expected_branch: Optional explicit branch to publish from. When set, the
            worktree's current branch must match or the call is rejected.
        require_prd_archived: When ``True`` (default), assert that the canonical
            PRD has been archived before pushing. PRD rework publishes new
            proposal PRDs in ``tasks/pending/`` (not yet archived) and must opt
            out by passing ``False``.

    Returns:
        The branch that was pushed.

    Raises:
        RuntimeError: If a safety check fails or ``git push`` exits non-zero.
    """
    branch = _validate_branch_for_publish(
        worktree_path,
        process_runner,
        expected_branch=expected_branch,
        issue=issue,
    )
    if require_prd_archived:
        assert_prd_archived_for_publish(issue, worktree_path)
    validate_safe_changes(worktree_path, config, process_runner)
    ensure_no_evidence_paths_in_changes(worktree_path, config, process_runner)
    publish_remote_name = validate_publish_remote(worktree_path, config, process_runner)
    try:
        process_runner.run(["git", "push", "-u", publish_remote_name, branch], cwd=worktree_path)
    except (RuntimeError, OSError) as exc:
        raise PushChangesError(str(exc)) from exc
    return branch


def create_draft_pr(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    *,
    expected_branch: str | None = None,
    content_generator: IContentGenerator | None = None,
) -> tuple[str, str]:
    """Create a draft PR for the current branch, or reuse an existing open PR.

    Assumes the branch has already been pushed by :func:`push_changes`. Performs
    the same branch guard, PR lookup, generated content, and validation
    checklist logic as the legacy :func:`publish_changes` so the resulting PR
    text and behaviour are unchanged.

    Args:
        issue: Issue being published.
        worktree_path: Agent worktree path.
        config: Agent Runner configuration.
        github_client: GitHub client used for PR lookup and creation.
        process_runner: Command runner.
        expected_branch: Optional explicit branch to verify before creating the
            PR. When set, the worktree's current branch must match.
        content_generator: Optional AI content generator for PR title/body.

    Returns:
        ``(branch, pr_url)`` tuple.

    Raises:
        DraftPRCreationError: If the PR lookup or creation step fails.
    """
    branch = _validate_branch_for_publish(
        worktree_path,
        process_runner,
        expected_branch=expected_branch,
        issue=issue,
    )

    try:
        existing_pr_url = github_client.find_open_pr_by_head(branch)
    except Exception as exc:
        raise DraftPRCreationError(str(exc)) from exc
    if existing_pr_url is not None:
        return branch, existing_pr_url

    fallback_title = f"[Agent] {issue.title}"
    fallback_body = f"Closes #{issue.number}\n\nGenerated by issue-agent-runner.\n"

    gc_config = config.generated_content
    pr_title = fallback_title
    pr_body = fallback_body
    if gc_config.enabled:
        gc_context = build_pr_context(
            issue=issue,
            branch=branch,
            base_branch=config.git.base_branch,
            worktree_path=worktree_path,
            process_runner=process_runner,
            target_config=gc_config.draft_pr,
        )
        generated = generate_pr_content(
            config=gc_config,
            context=gc_context,
            fallback_title=fallback_title,
            fallback_body=fallback_body,
            generator=content_generator,
            cwd=worktree_path,
        )
        pr_title = generated.title
        pr_body = generated.body

    if validation_required(issue.body, config):
        validation_checklist_items = extract_realistic_validation_items(issue.body)
        if validation_checklist_items:
            checklist_block = build_validation_checklist_block(validation_checklist_items)
            pr_body = f"{pr_body.rstrip()}\n\n{checklist_block}\n"

    try:
        pr_url = github_client.create_draft_pr(
            title=pr_title,
            body=pr_body,
            base_branch=config.git.base_branch,
            cwd=worktree_path,
        )
    except Exception as exc:
        raise DraftPRCreationError(str(exc)) from exc
    return branch, pr_url


def publish_changes(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    *,
    expected_branch: str | None = None,
    content_generator: IContentGenerator | None = None,
    require_prd_archived: bool = True,
) -> tuple[str, str]:
    """Compatibility wrapper that pushes then creates a draft PR in one call.

    New flows should call :func:`push_changes` and :func:`create_draft_pr`
    independently so the publish gate (PR creation) can be ordered around the
    pre-PR review. This wrapper is retained for call sites that legitimately
    need the combined behaviour (e.g. PRD rework publication that is not gated
    by pre-PR review).
    """
    push_changes(
        issue,
        worktree_path,
        config,
        process_runner,
        expected_branch=expected_branch,
        require_prd_archived=require_prd_archived,
    )
    return create_draft_pr(
        issue,
        worktree_path,
        config,
        github_client,
        process_runner,
        expected_branch=expected_branch,
        content_generator=content_generator,
    )
