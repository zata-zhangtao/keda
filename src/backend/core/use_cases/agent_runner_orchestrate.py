"""Agent runner orchestration — high-level issue processing flow."""

from __future__ import annotations

import logging
import socket
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import (
    AgentCommitResult,
    AppConfig,
    AttemptResult,
    CommandResult,
    FailureType,
    IssueSummary,
    PullRequestContext,
    ReviewEventMarker,
)
from backend.core.use_cases.agent_review import run_pre_push_review
from backend.core.use_cases.agent_runner_events import (
    format_event_marker,
    parse_latest_event_marker,
)
from backend.core.use_cases.pr_supervisor import (
    build_rework_intent_comment,
    build_supervisor_result_comment,
    execute_rebase,
    execute_repair,
    run_post_pr_supervisor_cycle,
)
from backend.core.use_cases.run_agent_once import (
    choose_agent,
    create_or_reuse_worktree,
    ensure_prd_delivery_ready,
    ensure_verification_passed,
    format_attempt_history,
    format_command,
    get_current_branch,
    get_head_sha,
    has_changes,
    publish_changes,
    run_agent_until_committed,
    run_preflight_checks,
    run_verification,
)

_logger = logging.getLogger(__name__)


def build_implementation_complete_comment(
    *,
    agent: str,
    branch: str,
    head_sha: str,
    verification_results: list[CommandResult],
    attempt_results: list[AttemptResult] | None = None,
) -> str:
    """Build the Issue comment after implementation agent finishes."""

    marker = format_event_marker(
        phase="implementation_complete",
        cycle=1,
        head_sha=head_sha,
    )
    verification_lines = "\n".join(
        f"- `{' '.join(result.command)}`: exit {result.return_code}"
        for result in verification_results
    )
    lines = [
        marker,
        "",
        "## Agent Runner Implementation Complete",
        "",
        f"- Agent: `{agent}`",
        f"- Branch: `{branch}`",
        f"- Head SHA: `{head_sha}`",
        "",
        "Verification:",
        verification_lines,
    ]
    if attempt_results:
        lines.append("")
        lines.append(format_attempt_history(attempt_results))
    return "\n".join(lines)


def build_draft_pr_created_comment(
    *,
    pr_url: str,
    branch: str,
    head_sha: str,
) -> str:
    """Build the Issue comment after Draft PR creation."""
    marker = format_event_marker(
        phase="draft_pr_created",
        cycle=1,
        head_sha=head_sha,
        pr_branch=branch,
    )
    return "\n".join(
        [
            marker,
            "",
            "## Agent Runner Draft PR Created",
            "",
            f"- Branch: `{branch}`",
            f"- Draft PR: {pr_url}",
            f"- Head SHA: `{head_sha}`",
        ]
    )


def _has_rework_intent(
    issue: IssueSummary,
    github_client: IGitHubClient,
) -> tuple[bool, ReviewEventMarker | None]:
    """Return whether the Issue has a post_pr_rework_requested marker."""
    comments = github_client.list_issue_comments(issue.number)
    marker = parse_latest_event_marker(comments)
    if marker is not None and marker.phase == "post_pr_rework_requested":
        return True, marker
    return False, None


def _guard_running_issue_is_rework(
    issue: IssueSummary,
    config: AppConfig,
    github_client: IGitHubClient,
) -> tuple[bool, ReviewEventMarker | None]:
    """Check if a running Issue is eligible for existing PR branch rework."""
    has_rework, marker = _has_rework_intent(issue, github_client)
    if not has_rework or marker is None:
        return False, None
    pr_branch = marker.pr_branch
    if pr_branch is None:
        return False, None
    pr_url = github_client.find_open_pr_by_head(pr_branch)
    if pr_url is None:
        return False, None
    return True, marker


def _find_worktree_path_for_issue(
    repo_path: Path,
    issue: IssueSummary,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> Path:
    """Locate the existing worktree for an Issue."""
    path_result = process_runner.run(
        format_command(config.worktree.path_command, issue_number=issue.number),
        cwd=repo_path,
    )
    return Path(path_result.stdout.strip()).resolve()


def _workflow_state_labels(config: AppConfig) -> list[str]:
    """Return durable workflow state labels, excluding agent routing labels."""
    return [
        config.labels.ready,
        config.labels.running,
        config.labels.supervising,
        config.labels.review,
        config.labels.blocked,
    ]


def _mark_issue_failed(
    *,
    issue: IssueSummary,
    config: AppConfig,
    github_client: IGitHubClient,
    exc: Exception,
) -> None:
    """Best-effort failure reporting without hiding the original error."""
    try:
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.failed],
            remove=_workflow_state_labels(config),
        )
    except Exception as label_exc:  # noqa: BLE001 - preserve original failure.
        _logger.error(
            "Failed to mark Issue #%d as %s: %s",
            issue.number,
            config.labels.failed,
            label_exc,
        )

    attempt_results = getattr(exc, "attempt_results", None)
    if attempt_results is not None:
        from backend.core.use_cases.run_agent_once import format_failure_comment

        comment_body = format_failure_comment(exc, attempt_results)
    else:
        comment_body = f"## Agent Runner Failed\n\n```text\n{exc}\n```\n"
    try:
        github_client.comment_issue(issue.number, comment_body)
    except Exception as comment_exc:  # noqa: BLE001 - preserve original failure.
        _logger.error(
            "Failed to comment on Issue #%d failure: %s",
            issue.number,
            comment_exc,
        )


def _count_local_commits_since_base(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> int:
    base_ref_name = f"{config.git.remote}/{config.git.base_branch}"
    ahead_result = process_runner.run(
        ["git", "rev-list", "--count", f"{base_ref_name}..HEAD"],
        cwd=worktree_path,
        check=False,
    )
    if ahead_result.return_code != 0:
        return 0
    try:
        return int(ahead_result.stdout.strip() or "0")
    except ValueError:
        return 0


def _get_merge_base_sha(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> str:
    base_ref_name = f"{config.git.remote}/{config.git.base_branch}"
    merge_base_result = process_runner.run(
        ["git", "merge-base", "HEAD", base_ref_name],
        cwd=worktree_path,
        check=False,
    )
    merge_base_sha = merge_base_result.stdout.strip()
    if merge_base_result.return_code == 0 and merge_base_sha:
        return merge_base_sha
    return get_head_sha(worktree_path, process_runner)


def _reuse_existing_local_commit(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> AgentCommitResult | None:
    """Return existing clean local commits ready for publish, if present."""
    local_commit_count = _count_local_commits_since_base(
        worktree_path, config, process_runner
    )
    if local_commit_count <= 0 or has_changes(worktree_path, process_runner):
        return None

    verification_results = run_verification(worktree_path, config, process_runner)
    ensure_verification_passed(verification_results)
    ensure_prd_delivery_ready(issue, worktree_path, process_runner)
    if has_changes(worktree_path, process_runner):
        return None

    base_ref_name = f"{config.git.remote}/{config.git.base_branch}"
    head_sha = get_head_sha(worktree_path, process_runner)
    _logger.info(
        "Reusing %d existing local commit(s) for Issue #%d at %s.",
        local_commit_count,
        issue.number,
        head_sha,
    )
    return AgentCommitResult(
        verification_results=verification_results,
        attempt_results=[
            AttemptResult(
                attempt_number=1,
                failure_type=FailureType.SUCCESS,
                recovered=True,
                detail=(
                    f"Reused {local_commit_count} existing local commit(s) "
                    f"already ahead of {base_ref_name}; agent was not invoked."
                ),
            )
        ],
    )


def _finish_implementation_publication(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    selected_agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    expected_branch: str,
    commit_result: AgentCommitResult,
    content_generator: IContentGenerator | None = None,
) -> None:
    verification_results = commit_result.verification_results
    after_sha = get_head_sha(worktree_path, process_runner)
    github_client.comment_issue(
        issue.number,
        build_implementation_complete_comment(
            agent=selected_agent,
            branch=expected_branch,
            head_sha=after_sha,
            verification_results=verification_results,
            attempt_results=commit_result.attempt_results,
        ),
    )

    final_sha, _final_verification_results = run_pre_push_review(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=github_client,
        process_runner=process_runner,
        selected_agent=selected_agent,
        head_sha_before=after_sha,
        expected_branch=expected_branch,
        verification_results=verification_results,
    )

    branch, pr_url = publish_changes(
        issue,
        worktree_path,
        config,
        github_client,
        process_runner,
        expected_branch=expected_branch,
        content_generator=content_generator,
    )

    github_client.edit_issue_labels(
        issue.number,
        add=[config.labels.supervising],
        remove=[config.labels.running],
    )

    publish_sha = get_head_sha(worktree_path, process_runner)
    github_client.comment_issue(
        issue.number,
        build_draft_pr_created_comment(
            pr_url=pr_url,
            branch=branch,
            head_sha=publish_sha,
        ),
    )

    supervisor_config = config.post_pr_supervisor
    if supervisor_config.enabled:
        pr_context = github_client.get_pull_request_context(branch)
        if pr_context is None:
            pr_context = PullRequestContext(
                pr_url=pr_url,
                branch=branch,
                head_sha=publish_sha,
                base_sha=_get_merge_base_sha(worktree_path, config, process_runner),
            )
        supervisor_agent = (
            selected_agent
            if supervisor_config.supervisor_agent == "auto"
            else supervisor_config.supervisor_agent
        )
        _run_supervisor_with_repair_loop(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            github_client=github_client,
            process_runner=process_runner,
            pr_context=pr_context,
            supervisor_agent=supervisor_agent,
        )
    else:
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.review],
            remove=[config.labels.supervising],
        )

    _logger.info(
        "Published Issue #%d from %s at %s after implementation head %s.",
        issue.number,
        branch,
        final_sha,
        after_sha,
    )


def _finish_existing_commit_publication(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    selected_agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    expected_branch: str,
    commit_result: AgentCommitResult,
    content_generator: IContentGenerator | None = None,
) -> None:
    verification_results = commit_result.verification_results
    head_sha = get_head_sha(worktree_path, process_runner)
    github_client.comment_issue(
        issue.number,
        build_implementation_complete_comment(
            agent=selected_agent,
            branch=expected_branch,
            head_sha=head_sha,
            verification_results=verification_results,
            attempt_results=commit_result.attempt_results,
        ),
    )

    branch, pr_url = publish_changes(
        issue,
        worktree_path,
        config,
        github_client,
        process_runner,
        expected_branch=expected_branch,
        content_generator=content_generator,
    )
    publish_sha = get_head_sha(worktree_path, process_runner)
    github_client.edit_issue_labels(
        issue.number,
        add=[config.labels.review],
        remove=_workflow_state_labels(config),
    )
    github_client.comment_issue(
        issue.number,
        build_draft_pr_created_comment(
            pr_url=pr_url,
            branch=branch,
            head_sha=publish_sha,
        ),
    )
    _logger.info(
        "Recovered publication for Issue #%d from %s at %s.",
        issue.number,
        branch,
        publish_sha,
    )


def _has_existing_local_commit_ready_for_publish(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> bool:
    try:
        worktree_path = _find_worktree_path_for_issue(
            repo_path, issue, config, process_runner
        )
        return _count_local_commits_since_base(
            worktree_path, config, process_runner
        ) > 0 and not has_changes(worktree_path, process_runner)
    except Exception as exc:  # noqa: BLE001 - candidate probing must not fail polling.
        _logger.info(
            "Skipping existing local commit probe for Issue #%d: %s",
            issue.number,
            exc,
        )
        return False


def _process_ready_issue(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None = None,
) -> None:
    """Process a ready Issue through the full first-implementation path."""
    selected_agent = choose_agent(issue, config, agent)
    github_client.edit_issue_labels(
        issue.number, add=[config.labels.running], remove=[config.labels.ready]
    )
    github_client.comment_issue(
        issue.number,
        "## Agent Runner Claimed\n\n"
        f"- Host: `{socket.gethostname()}`\n"
        f"- Agent: `{selected_agent}`\n",
    )
    worktree_path = create_or_reuse_worktree(repo_path, issue, config, process_runner)
    before_sha = get_head_sha(worktree_path, process_runner)
    expected_branch = get_current_branch(worktree_path, process_runner)
    commit_result = _reuse_existing_local_commit(
        issue, worktree_path, config, process_runner
    )
    if commit_result is not None:
        _finish_existing_commit_publication(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            selected_agent=selected_agent,
            github_client=github_client,
            process_runner=process_runner,
            expected_branch=expected_branch,
            commit_result=commit_result,
            content_generator=content_generator,
        )
        return

    new_commit_result = run_agent_until_committed(
        selected_agent=selected_agent,
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        process_runner=process_runner,
        before_sha=before_sha,
        expected_branch=expected_branch,
    )
    _finish_implementation_publication(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        selected_agent=selected_agent,
        github_client=github_client,
        process_runner=process_runner,
        expected_branch=expected_branch,
        commit_result=new_commit_result,
        content_generator=content_generator,
    )


def _run_supervisor_with_repair_loop(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    pr_context: PullRequestContext,
    supervisor_agent: str,
) -> None:
    """Run supervisor cycles with bounded inline repair/rebase."""
    max_repair = max(0, config.post_pr_supervisor.max_repair_attempts)
    current_pr_context = pr_context

    for cycle in range(1, max_repair + 2):
        action_result = run_post_pr_supervisor_cycle(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            github_client=github_client,
            process_runner=process_runner,
            pr_context=current_pr_context,
            supervisor_agent=supervisor_agent,
            cycle=cycle,
        )

        if action_result.action == "approve_for_human_review":
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.review],
                remove=[config.labels.supervising],
            )
            return

        if action_result.action in ("request_human_input",):
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.blocked],
                remove=[config.labels.supervising],
            )
            return

        if action_result.action == "mark_failed":
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.failed],
                remove=[config.labels.supervising],
            )
            return

        if action_result.action in ("repair_pr_branch", "resolve_conflict"):
            if cycle > max_repair:
                github_client.comment_issue(
                    issue.number,
                    build_supervisor_result_comment(
                        action="max_repair_exceeded",
                        supervisor=supervisor_agent,
                        summary="Max repair attempts exceeded; moving to blocked.",
                        findings_counts={},
                        verification_status="",
                        head_sha=current_pr_context.head_sha,
                        cycle=cycle,
                    ),
                )
                github_client.edit_issue_labels(
                    issue.number,
                    add=[config.labels.blocked],
                    remove=[config.labels.supervising],
                )
                return
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.running],
                remove=[config.labels.supervising],
            )
            execute_repair(
                issue=issue,
                worktree_path=worktree_path,
                config=config,
                process_runner=process_runner,
                pr_branch=current_pr_context.branch,
                expected_head=current_pr_context.head_sha,
                supervisor_agent=supervisor_agent,
            )
            repair_sha = get_head_sha(worktree_path, process_runner)
            github_client.comment_issue(
                issue.number,
                build_rework_intent_comment(
                    action=action_result.action,
                    pr_branch=current_pr_context.branch,
                    head_sha=repair_sha,
                ),
            )
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.supervising],
                remove=[config.labels.running],
            )
            current_pr_context = PullRequestContext(
                pr_url=current_pr_context.pr_url,
                branch=current_pr_context.branch,
                head_sha=repair_sha,
                base_sha=current_pr_context.base_sha,
            )
            continue

        if action_result.action == "rebase_pr_branch":
            if cycle > max_repair:
                github_client.comment_issue(
                    issue.number,
                    build_supervisor_result_comment(
                        action="max_rebase_exceeded",
                        supervisor=supervisor_agent,
                        summary="Max rebase attempts exceeded; moving to blocked.",
                        findings_counts={},
                        verification_status="",
                        head_sha=current_pr_context.head_sha,
                        cycle=cycle,
                    ),
                )
                github_client.edit_issue_labels(
                    issue.number,
                    add=[config.labels.blocked],
                    remove=[config.labels.supervising],
                )
                return
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.running],
                remove=[config.labels.supervising],
            )
            execute_rebase(
                issue=issue,
                worktree_path=worktree_path,
                config=config,
                process_runner=process_runner,
                pr_branch=current_pr_context.branch,
                expected_head=current_pr_context.head_sha,
                supervisor_agent=supervisor_agent,
            )
            rebase_sha = get_head_sha(worktree_path, process_runner)
            github_client.comment_issue(
                issue.number,
                build_rework_intent_comment(
                    action=action_result.action,
                    pr_branch=current_pr_context.branch,
                    head_sha=rebase_sha,
                ),
            )
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.supervising],
                remove=[config.labels.running],
            )
            current_pr_context = PullRequestContext(
                pr_url=current_pr_context.pr_url,
                branch=current_pr_context.branch,
                head_sha=rebase_sha,
                base_sha=current_pr_context.base_sha,
            )
            continue

        # Unknown action: treat as blocked
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.blocked],
            remove=[config.labels.supervising],
        )
        return

    # If we exhausted all cycles without approval, block
    github_client.edit_issue_labels(
        issue.number,
        add=[config.labels.blocked],
        remove=[config.labels.supervising],
    )


def _process_running_rework(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    marker: ReviewEventMarker,
) -> None:
    """Process a running Issue with a post-PR rework intent marker."""
    pr_branch = marker.pr_branch
    if pr_branch is None:
        raise RuntimeError("Rework marker missing pr_branch")

    worktree_path = _find_worktree_path_for_issue(
        repo_path, issue, config, process_runner
    )
    current_branch = get_current_branch(worktree_path, process_runner)
    if current_branch != pr_branch:
        raise RuntimeError(
            f"Rework aborted: on branch {current_branch}, expected {pr_branch}"
        )

    expected_head = marker.head_sha or get_head_sha(worktree_path, process_runner)
    action = marker.action or "repair_pr_branch"
    supervisor_agent = choose_agent(issue, config, agent)

    if action == "rebase_pr_branch":
        execute_rebase(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=process_runner,
            pr_branch=pr_branch,
            expected_head=expected_head,
            supervisor_agent=supervisor_agent,
        )
        rebase_sha = get_head_sha(worktree_path, process_runner)
        github_client.comment_issue(
            issue.number,
            build_rework_intent_comment(
                action=action,
                pr_branch=pr_branch,
                head_sha=rebase_sha,
            ),
        )
    else:
        execute_repair(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=process_runner,
            pr_branch=pr_branch,
            expected_head=expected_head,
            supervisor_agent=supervisor_agent,
        )
        repair_sha = get_head_sha(worktree_path, process_runner)
        github_client.comment_issue(
            issue.number,
            build_rework_intent_comment(
                action=action,
                pr_branch=pr_branch,
                head_sha=repair_sha,
            ),
        )

    github_client.edit_issue_labels(
        issue.number,
        add=[config.labels.supervising],
        remove=[config.labels.running],
    )

    # Run supervisor cycle after rework
    pr_context = github_client.get_pull_request_context(pr_branch)
    if pr_context is None:
        pr_context = PullRequestContext(
            pr_url=github_client.find_open_pr_by_head(pr_branch) or "",
            branch=pr_branch,
            head_sha=get_head_sha(worktree_path, process_runner),
            base_sha=expected_head,
        )

    if config.post_pr_supervisor.enabled:
        _run_supervisor_with_repair_loop(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            github_client=github_client,
            process_runner=process_runner,
            pr_context=pr_context,
            supervisor_agent=supervisor_agent,
        )
    else:
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.review],
            remove=[config.labels.running],
        )


def _process_running_publish_recovery(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None = None,
) -> None:
    """Resume publication for a running Issue that already has local commits."""
    selected_agent = choose_agent(issue, config, agent)
    worktree_path = _find_worktree_path_for_issue(
        repo_path, issue, config, process_runner
    )
    expected_branch = get_current_branch(worktree_path, process_runner)
    commit_result = _reuse_existing_local_commit(
        issue, worktree_path, config, process_runner
    )
    if commit_result is None:
        raise RuntimeError(
            f"Issue #{issue.number} has no clean local commit ready for publication."
        )

    _finish_existing_commit_publication(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        selected_agent=selected_agent,
        github_client=github_client,
        process_runner=process_runner,
        expected_branch=expected_branch,
        commit_result=commit_result,
        content_generator=content_generator,
    )


def run_once(
    *,
    repo_path: Path,
    config: AppConfig,
    dry_run: bool,
    agent: str,
    max_issues: int,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None = None,
) -> int:
    """Run one polling pass.

    Args:
        repo_path: Target repository path.
        config: Application configuration.
        dry_run: If True, only list ready issues without processing.
        agent: Agent override (auto, codex, claude).
        max_issues: Maximum issues to process.
        github_client: Client for interacting with GitHub.
        process_runner: Runner for executing subprocess commands.
        content_generator: Optional content generator for AI-generated PR content.

    Returns:
        Exit code (0 on success, 1 if any issue failed).
    """
    if not dry_run:
        try:
            run_preflight_checks(repo_path, config, process_runner)
        except Exception as exc:  # noqa: BLE001 - report preflight failure cleanly.
            _logger.error("Agent runner preflight failed: %s", exc)
            return 1

    ready_issues = github_client.list_ready_issues(config.labels.ready, max_issues)
    processed_count = 0
    issues_to_process: list[tuple[IssueSummary, str]] = []

    for issue in ready_issues:
        issues_to_process.append((issue, "ready"))
        processed_count += 1

    remaining = max_issues - processed_count
    if remaining > 0:
        running_candidates = github_client.list_review_candidate_issues(
            [config.labels.running], remaining
        )
        for issue in running_candidates:
            is_rework, marker = _guard_running_issue_is_rework(
                issue, config, github_client
            )
            if is_rework and marker is not None:
                issues_to_process.append((issue, "running_rework"))
            elif _has_existing_local_commit_ready_for_publish(
                issue=issue,
                repo_path=repo_path,
                config=config,
                process_runner=process_runner,
            ):
                issues_to_process.append((issue, "running_publish_recovery"))
            else:
                _logger.info(
                    "Skipping Issue #%d with label %s: no rework marker, open PR, or clean local commit.",
                    issue.number,
                    config.labels.running,
                )

    if not issues_to_process:
        _logger.info(
            "No open Issues found with label %s or eligible running rework.",
            config.labels.ready,
        )
        return 0

    exit_code = 0
    for issue, issue_kind in issues_to_process:
        selected_agent = choose_agent(issue, config, agent)
        if dry_run:
            _logger.info(
                "DRY RUN: would process Issue #%d (%s) with %s: %s",
                issue.number,
                issue_kind,
                selected_agent,
                issue.title,
            )
            continue
        try:
            if issue_kind == "ready":
                _process_ready_issue(
                    issue=issue,
                    repo_path=repo_path,
                    config=config,
                    agent=agent,
                    github_client=github_client,
                    process_runner=process_runner,
                    content_generator=content_generator,
                )
            elif issue_kind == "running_rework":
                _, marker = _guard_running_issue_is_rework(issue, config, github_client)
                if marker is None:
                    continue
                _process_running_rework(
                    issue=issue,
                    repo_path=repo_path,
                    config=config,
                    agent=agent,
                    github_client=github_client,
                    process_runner=process_runner,
                    marker=marker,
                )
            else:
                _process_running_publish_recovery(
                    issue=issue,
                    repo_path=repo_path,
                    config=config,
                    agent=agent,
                    github_client=github_client,
                    process_runner=process_runner,
                    content_generator=content_generator,
                )
            _logger.info("Completed Issue #%d: %s", issue.number, issue.title)
        except Exception as exc:  # noqa: BLE001 - report queue failures and continue.
            exit_code = 1
            _mark_issue_failed(
                issue=issue,
                config=config,
                github_client=github_client,
                exc=exc,
            )
            _logger.error("Failed Issue #%d: %s", issue.number, exc)
    return exit_code
