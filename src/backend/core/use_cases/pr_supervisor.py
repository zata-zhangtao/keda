"""Post-PR supervisor cycle for agent runner."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PullRequestContext,
    SupervisorActionResult,
)
from backend.core.use_cases.agent_runner_events import (
    format_event_marker,
)
from backend.core.use_cases.run_agent_once import (
    commit_requested_changes,
    ensure_verification_passed,
    extract_agent_response_text,
    get_current_branch,
    get_head_sha,
    has_changes,
    read_commit_request,
    remove_commit_request,
    run_agent_with_prompt,
    run_verification,
    validate_safe_changes,
)

_logger = logging.getLogger(__name__)


VALID_SUPERVISOR_ACTIONS: set[str] = {
    "approve_for_human_review",
    "repair_pr_branch",
    "rebase_pr_branch",
    "resolve_conflict",
    "request_human_input",
    "mark_failed",
}


def build_supervisor_prompt(
    issue: IssueSummary,
    pr_context: PullRequestContext,
    config: AppConfig,
    process_runner: IProcessRunner,
    worktree_path: Path,
    issue_comments: list[str],
    pr_comments: list[str],
    base_sha_remote: str,
) -> str:
    """Build the prompt sent to the post-PR supervisor agent."""
    prd_path_match = re.search(r"PRD path:\s*`([^`]+)`", issue.body)
    prd_line = (
        f"Canonical PRD: `{prd_path_match.group(1)}`"
        if prd_path_match
        else "If the Issue references a PRD, read it before reviewing."
    )

    diff_result = process_runner.run(
        ["git", "diff", f"{config.git.base_branch}...{pr_context.head_sha}"],
        cwd=worktree_path,
        check=False,
    )
    diff_text = (
        diff_result.stdout if diff_result.return_code == 0 else "(diff unavailable)"
    )

    verification_results = run_verification(worktree_path, config, process_runner)
    verification_lines = "\n".join(
        f"- `{' '.join(result.command)}`: exit {result.return_code}"
        for result in verification_results
    )

    issue_comments_text = "\n".join(
        f"- {comment[:200]}" for comment in issue_comments[-10:]
    )
    pr_comments_text = "\n".join(f"- {comment[:200]}" for comment in pr_comments[-10:])

    return "\n".join(
        [
            f"Post-PR Supervisor Review for Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            f"PR URL: {pr_context.pr_url}",
            f"Branch: `{pr_context.branch}`",
            f"Head SHA: `{pr_context.head_sha}`",
            f"Base SHA (remote): `{base_sha_remote}`",
            f"PR Base SHA: `{pr_context.base_sha}`",
            f"Mergeable: {pr_context.mergeable}",
            f"Checks state: {pr_context.checks_state}",
            "Checks summary:",
            "\n".join(f"- {check}" for check in pr_context.checks_summary) or "(none)",
            prd_line,
            "",
            "Issue body:",
            issue.body,
            "",
            "Diff:",
            "```diff",
            diff_text[:6000] if len(diff_text) > 6000 else diff_text,
            "```",
            "",
            "Verification results:",
            verification_lines,
            "",
            "Recent Issue comments:",
            issue_comments_text or "(none)",
            "",
            "Recent PR comments:",
            pr_comments_text or "(none)",
            "",
            "Review workflow context:",
            "- Review scope: docs/guides/review-workflow.md",
            "- Check requirement alignment, code safety, validation evidence, and docs sync.",
            "",
            "Output rules:",
            "- Respond with a single JSON object in a markdown code block.",
            "- Required fields: action, summary.",
            "- action must be one of: approve_for_human_review, repair_pr_branch, rebase_pr_branch, resolve_conflict, request_human_input, mark_failed.",
            "- Optional fields: findings_high (int), findings_medium (int), findings_low (int), verification_status (str), head_sha (str).",
            "- Do not modify files; only return the JSON decision.",
        ]
    )


def parse_supervisor_action(text: str) -> SupervisorActionResult:
    """Parse supervisor JSON output from agent response text."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        json_text = match.group(1)
    else:
        match = re.search(r"\{.*\"action\".*\}", text, re.DOTALL)
        json_text = match.group(0) if match else "{}"

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        payload = {}

    action = str(payload.get("action", "request_human_input"))
    if action not in VALID_SUPERVISOR_ACTIONS:
        action = "request_human_input"

    findings = {}
    for level in ("high", "medium", "low"):
        key = f"findings_{level}"
        if key in payload:
            try:
                findings[level] = int(payload[key])
            except (ValueError, TypeError):
                findings[level] = 0

    return SupervisorActionResult(
        action=action,
        summary=str(payload.get("summary", "")),
        findings_counts=findings,
        verification_status=str(payload.get("verification_status", "")),
        head_sha=str(payload.get("head_sha", "")) or None,
    )


def guard_supervisor_action_for_pr_state(
    action_result: SupervisorActionResult,
    pr_context: PullRequestContext,
) -> SupervisorActionResult:
    """Prevent unsafe approval when deterministic PR state is not reviewable."""
    if action_result.action != "approve_for_human_review":
        return action_result

    if pr_context.mergeable is False:
        summary = (
            "Approval blocked by PR mergeability gate: the PR is currently "
            "conflicting or otherwise not mergeable. Requesting rebase before "
            f"human review. Supervisor summary: {action_result.summary}"
        )
        return SupervisorActionResult(
            action="rebase_pr_branch",
            summary=summary,
            findings_counts=action_result.findings_counts,
            verification_status=action_result.verification_status,
            head_sha=action_result.head_sha,
        )

    if pr_context.checks_state == "FAILURE":
        failed_checks_text = (
            "; ".join(pr_context.checks_summary)
            if pr_context.checks_summary
            else "failed PR checks"
        )
        summary = (
            "Approval blocked by PR checks gate: checks are failing "
            f"({failed_checks_text}). Requesting branch repair before human "
            f"review. Supervisor summary: {action_result.summary}"
        )
        return SupervisorActionResult(
            action="repair_pr_branch",
            summary=summary,
            findings_counts=action_result.findings_counts,
            verification_status=action_result.verification_status,
            head_sha=action_result.head_sha,
        )

    return action_result


def build_supervisor_result_comment(
    *,
    action: str,
    supervisor: str,
    summary: str,
    findings_counts: dict[str, int],
    verification_status: str,
    head_sha: str | None,
    cycle: int,
    checks_state: str | None = None,
    mergeable: bool | None = None,
    issue_comments_count: int | None = None,
    pr_comments_count: int | None = None,
) -> str:
    """Build the human-readable comment for a supervisor cycle result."""
    marker = format_event_marker(
        phase="post_pr_supervisor",
        cycle=cycle,
        head_sha=head_sha,
        checks_state=checks_state,
        mergeable=mergeable,
        issue_comments_count=issue_comments_count,
        pr_comments_count=pr_comments_count,
    )
    high = findings_counts.get("high", 0)
    medium = findings_counts.get("medium", 0)
    low = findings_counts.get("low", 0)
    return "\n".join(
        [
            marker,
            "",
            "## Agent Runner Post-PR Supervisor",
            "",
            f"- Action: {action}",
            f"- Supervisor: {supervisor}",
            f"- Summary: {summary}",
            f"- Findings: {high} high, {medium} medium, {low} low",
            f"- Verification: {verification_status or 'unknown'}",
            f"- Head SHA: `{head_sha or 'N/A'}`",
        ]
    )


def build_rework_intent_comment(
    *,
    action: str,
    pr_branch: str,
    head_sha: str,
) -> str:
    """Build the comment that marks a post-PR rework intent."""
    marker = format_event_marker(
        phase="post_pr_rework_requested",
        cycle=1,
        head_sha=head_sha,
        pr_branch=pr_branch,
        action=action,
    )
    return "\n".join(
        [
            marker,
            "",
            "## Agent Runner Post-PR Rework Requested",
            "",
            f"- Action: {action}",
            f"- PR Branch: `{pr_branch}`",
            f"- Head SHA: `{head_sha}`",
            "- A runner will pick this up on the next `run-once` pass.",
        ]
    )


def build_rebase_repair_complete_comment(
    *,
    action: str,
    head_sha: str,
    verification_passed: bool,
) -> str:
    """Build the comment after a rebase or repair completes."""
    marker = format_event_marker(
        phase="rebase_repair_complete",
        cycle=1,
        head_sha=head_sha,
    )
    return "\n".join(
        [
            marker,
            "",
            "## Agent Runner Rebase/Repair Complete",
            "",
            f"- Action: {action}",
            f"- Head SHA: `{head_sha}`",
            f"- Verification: {'passed' if verification_passed else 'failed'}",
        ]
    )


def build_conflict_resolution_prompt(
    issue: IssueSummary,
    pr_branch: str,
    expected_head: str,
    conflicted_files: list[str],
) -> str:
    """Build the prompt for the rebase conflict resolution agent."""
    files_text = "\n".join(f"- {f}" for f in conflicted_files) or "(none)"
    return "\n".join(
        [
            f"Resolve rebase conflicts for Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            f"PR Branch: `{pr_branch}`",
            f"Expected HEAD: `{expected_head}`",
            "",
            "The rebase onto the remote base branch encountered conflicts in these files:",
            files_text,
            "",
            "Resolve all conflicts and request a commit.",
            "- Only modify conflicted files inside the current worktree.",
            "- Do not switch branches, push, or abort the rebase.",
            "- Do not run `git add` or `git commit`; the runner handles staging.",
            "- After resolving conflicts, write `.agent-runner/commit-request.json` "
            "as JSON with `commit_message`.",
        ]
    )


def execute_rebase(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    pr_branch: str,
    expected_head: str,
    supervisor_agent: str,
) -> list[CommandResult]:
    """Rebase the PR branch onto the latest remote base safely.

    Args:
        issue: The Issue being rebased.
        worktree_path: Path to the worktree.
        config: Application configuration.
        process_runner: Process runner for git commands.
        pr_branch: Name of the PR branch.
        expected_head: Expected current HEAD SHA before rebase.
        supervisor_agent: Agent to run for conflict resolution.

    Returns:
        Verification results after rebase.
    """
    current_head = get_head_sha(worktree_path, process_runner)
    if current_head != expected_head:
        raise RuntimeError(
            f"Rebase aborted: HEAD {current_head} does not match expected {expected_head}"
        )

    current_branch = get_current_branch(worktree_path, process_runner)
    if current_branch != pr_branch:
        raise RuntimeError(
            f"Rebase aborted: on branch {current_branch}, expected {pr_branch}"
        )

    remote = config.git.remote
    base_branch = config.git.base_branch

    # Fetch latest base branch
    process_runner.run(
        ["git", "fetch", remote, base_branch],
        cwd=worktree_path,
    )

    # Rebase onto fetched base
    rebase_result = process_runner.run(
        ["git", "rebase", f"{remote}/{base_branch}"],
        cwd=worktree_path,
        check=False,
    )

    if rebase_result.return_code != 0:
        max_attempts = max(0, config.post_pr_supervisor.max_repair_attempts)
        for attempt in range(1, max_attempts + 1):
            diff_names_result = process_runner.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=worktree_path,
                check=False,
            )
            conflicted_files = [
                line.strip()
                for line in diff_names_result.stdout.splitlines()
                if line.strip()
            ]
            prompt = build_conflict_resolution_prompt(
                issue, pr_branch, expected_head, conflicted_files
            )
            run_agent_with_prompt(
                supervisor_agent, prompt, worktree_path, process_runner
            )

            request_path = worktree_path / ".agent-runner" / "commit-request.json"
            if request_path.is_file():
                current_branch = get_current_branch(worktree_path, process_runner)
                if current_branch != pr_branch:
                    raise RuntimeError(
                        f"Refusing to commit on unexpected branch: {current_branch}"
                    )
                _ = read_commit_request(worktree_path, issue)
                remove_commit_request(worktree_path)
                if not has_changes(worktree_path, process_runner):
                    raise RuntimeError(
                        "Agent requested a commit but produced no file changes."
                    )
                validate_safe_changes(worktree_path, config, process_runner)
                process_runner.run(["git", "add", "-A"], cwd=worktree_path)
                verification_results = run_verification(
                    worktree_path, config, process_runner
                )
                ensure_verification_passed(verification_results)
                continue_result = process_runner.run(
                    ["git", "rebase", "--continue"],
                    cwd=worktree_path,
                    check=False,
                )
                if continue_result.return_code == 0:
                    verification_results = run_verification(
                        worktree_path, config, process_runner
                    )
                    ensure_verification_passed(verification_results)
                    process_runner.run(
                        ["git", "push", "--force-with-lease", remote, pr_branch],
                        cwd=worktree_path,
                    )
                    return verification_results
            else:
                if has_changes(worktree_path, process_runner):
                    raise RuntimeError(
                        "Rebase conflict agent changed files without writing "
                        ".agent-runner/commit-request.json."
                    )
                continue_result = process_runner.run(
                    ["git", "rebase", "--continue"],
                    cwd=worktree_path,
                    check=False,
                )
                if continue_result.return_code == 0:
                    verification_results = run_verification(
                        worktree_path, config, process_runner
                    )
                    ensure_verification_passed(verification_results)
                    process_runner.run(
                        ["git", "push", "--force-with-lease", remote, pr_branch],
                        cwd=worktree_path,
                    )
                    return verification_results

        process_runner.run(
            ["git", "rebase", "--abort"],
            cwd=worktree_path,
            check=False,
        )
        raise RuntimeError("Rebase conflict resolution exhausted")

    # Verify after rebase
    verification_results = run_verification(worktree_path, config, process_runner)
    ensure_verification_passed(verification_results)

    # Push with force-with-lease only on the PR branch
    process_runner.run(
        ["git", "push", "--force-with-lease", remote, pr_branch],
        cwd=worktree_path,
    )

    return verification_results


def execute_repair(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    pr_branch: str,
    expected_head: str,
    supervisor_agent: str,
) -> list[CommandResult]:
    """Run a repair agent on the existing PR branch and commit changes.

    Args:
        issue: The Issue being repaired.
        worktree_path: Path to the worktree.
        config: Application configuration.
        process_runner: Process runner for commands.
        pr_branch: Name of the PR branch.
        expected_head: Expected current HEAD SHA before repair.
        supervisor_agent: Agent to run for repair.

    Returns:
        Verification results after repair commit.
    """
    current_head = get_head_sha(worktree_path, process_runner)
    if current_head != expected_head:
        raise RuntimeError(
            f"Repair aborted: HEAD {current_head} does not match expected {expected_head}"
        )

    current_branch = get_current_branch(worktree_path, process_runner)
    if current_branch != pr_branch:
        raise RuntimeError(
            f"Repair aborted: on branch {current_branch}, expected {pr_branch}"
        )

    repair_prompt = "\n".join(
        [
            f"Repair PR branch for Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            f"Worktree: {worktree_path}",
            "",
            "The post-PR supervisor requested code changes on this branch.",
            "Inspect the current worktree, make the necessary fixes, and request a commit.",
            "- Only modify files inside the current worktree.",
            "- Do not switch branches, merge main, push, or create PRs.",
            "- Do not run `git add` or `git commit`; the runner handles commits.",
            "- After fixing, write `.agent-runner/commit-request.json` as JSON with `commit_message`.",
        ]
    )

    run_agent_with_prompt(
        supervisor_agent, repair_prompt, worktree_path, process_runner
    )

    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    if request_path.is_file():
        verification_results = commit_requested_changes(
            issue,
            worktree_path,
            config,
            process_runner,
            expected_branch=pr_branch,
        )
    else:
        if has_changes(worktree_path, process_runner):
            raise RuntimeError(
                "Repair agent changed files without writing "
                ".agent-runner/commit-request.json."
            )
        verification_results = run_verification(worktree_path, config, process_runner)
        ensure_verification_passed(verification_results)

    remote = config.git.remote
    process_runner.run(
        ["git", "push", remote, pr_branch],
        cwd=worktree_path,
    )

    return verification_results


def run_post_pr_supervisor_cycle(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    pr_context: PullRequestContext,
    supervisor_agent: str,
    cycle: int,
) -> SupervisorActionResult:
    """Run a single post-PR supervisor cycle.

    Args:
        issue: The Issue being supervised.
        worktree_path: Path to the worktree.
        config: Application configuration.
        github_client: GitHub client for comments and context.
        process_runner: Process runner for commands.
        pr_context: PR context.
        supervisor_agent: Agent to use for supervision.
        cycle: Cycle number for event markers.

    Returns:
        Supervisor action result.
    """
    issue_comments = github_client.list_issue_comments(issue.number)
    # Derive PR number from URL for PR comments
    pr_number_match = re.search(r"/pull/(\d+)", pr_context.pr_url)
    pr_comments: list[str] = []
    if pr_number_match:
        pr_comments = github_client.list_pr_comments(int(pr_number_match.group(1)))

    base_sha_remote = github_client.get_remote_base_sha(
        config.git.remote, config.git.base_branch
    )

    supervisor_prompt = build_supervisor_prompt(
        issue=issue,
        pr_context=pr_context,
        config=config,
        process_runner=process_runner,
        worktree_path=worktree_path,
        issue_comments=issue_comments,
        pr_comments=pr_comments,
        base_sha_remote=base_sha_remote,
    )

    result = run_agent_with_prompt(
        supervisor_agent,
        supervisor_prompt,
        worktree_path,
        process_runner,
        capture_output=True,
    )
    raw_action_result = parse_supervisor_action(extract_agent_response_text(result))
    action_result = guard_supervisor_action_for_pr_state(
        raw_action_result,
        pr_context,
    )

    comment_body = build_supervisor_result_comment(
        action=action_result.action,
        supervisor=supervisor_agent,
        summary=action_result.summary,
        findings_counts=action_result.findings_counts,
        verification_status=action_result.verification_status,
        head_sha=action_result.head_sha or pr_context.head_sha,
        cycle=cycle,
        checks_state=pr_context.checks_state,
        mergeable=pr_context.mergeable,
        issue_comments_count=len(issue_comments),
        pr_comments_count=len(pr_comments),
    )
    github_client.comment_issue(issue.number, comment_body)

    return action_result
