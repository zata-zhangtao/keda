"""Pre-push AI review gate for the agent runner."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
)
from backend.core.use_cases.agent_runner_events import (
    format_event_marker,
)
from backend.core.use_cases.run_agent_once import (
    EmptyCommitRequestError,
    commit_requested_changes,
    extract_agent_response_text,
    extract_prd_path,
    get_head_sha,
    run_agent_with_prompt,
)

_logger = logging.getLogger(__name__)

_VALID_REVIEW_VERDICTS = {"approved", "changes_requested"}


@dataclass(frozen=True)
class ReviewerDecision:
    """Parsed pre-push reviewer decision."""

    verdict: str
    summary: str = ""
    findings_high: int = 0
    findings_medium: int = 0
    findings_low: int = 0
    parseable: bool = True


def build_review_packet(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    verification_results: list[CommandResult],
    head_sha: str,
) -> str:
    """Build the context packet sent to the pre-push reviewer."""
    prd_path = extract_prd_path(issue.body)
    prd_line = (
        f"Canonical PRD: `{prd_path}`"
        if prd_path
        else "If the Issue references a PRD, read it before reviewing."
    )

    diff_result = process_runner.run(
        ["git", "diff", f"{config.git.base_branch}...{head_sha}"],
        cwd=worktree_path,
        check=False,
    )
    diff_text = (
        diff_result.stdout if diff_result.return_code == 0 else "(diff unavailable)"
    )

    status_result = process_runner.run(
        ["git", "status", "--short"],
        cwd=worktree_path,
        check=False,
    )
    changed_paths = status_result.stdout.strip() or "(no uncommitted changes)"

    verification_lines = "\n".join(
        f"- `{' '.join(result.command)}`: exit {result.return_code}"
        for result in verification_results
    )

    return "\n".join(
        [
            f"Pre-Push Review for Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            prd_line,
            "",
            "Issue body:",
            issue.body,
            "",
            "Changed paths:",
            "```",
            changed_paths,
            "```",
            "",
            "Diff:",
            "```diff",
            diff_text[:8000] if len(diff_text) > 8000 else diff_text,
            "```",
            "",
            "Verification results:",
            verification_lines,
            "",
            "Review rules:",
            "- Inspect the code against the Issue, PRD, and repository standards.",
            "- You may modify files directly in the worktree if you find issues.",
            "- Do not run `git add` or `git commit`; the runner handles commits.",
            "- After making changes, write `.agent-runner/commit-request.json` as JSON with `commit_message`.",
            "- Finish with a single JSON object in a markdown code block.",
            "- Required fields: verdict, summary.",
            "- verdict must be one of: approved, changes_requested.",
            "- Optional fields: findings_high, findings_medium, findings_low.",
        ]
    )


def parse_reviewer_decision(text: str) -> ReviewerDecision:
    """Parse reviewer verdict and finding counts from agent output."""
    payload = _extract_json_payload(text)
    if payload is not None:
        verdict = _normalize_review_verdict(payload.get("verdict"))
        if verdict in _VALID_REVIEW_VERDICTS:
            return ReviewerDecision(
                verdict=verdict,
                summary=str(payload.get("summary", "")),
                findings_high=_int_field(payload, "findings_high"),
                findings_medium=_int_field(payload, "findings_medium"),
                findings_low=_int_field(payload, "findings_low"),
            )

    fallback_verdict = _parse_text_verdict(text)
    if fallback_verdict is not None:
        return ReviewerDecision(verdict=fallback_verdict, summary=text.strip())

    return ReviewerDecision(
        verdict="changes_requested",
        summary="Reviewer did not return a parseable verdict.",
        parseable=False,
    )


def _extract_json_payload(text: str) -> dict[str, object] | None:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        json_text = match.group(1)
    else:
        match = re.search(r"\{.*\"verdict\".*\}", text, re.DOTALL)
        if match is None:
            return None
        json_text = match.group(0)
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_review_verdict(raw_verdict: object) -> str:
    verdict_text = str(raw_verdict or "").strip().lower().replace("-", "_")
    verdict_text = re.sub(r"\s+", "_", verdict_text)
    if verdict_text in {"approve", "approved", "pass", "passed"}:
        return "approved"
    if verdict_text in {
        "changes_requested",
        "change_requested",
        "request_changes",
        "requested_changes",
        "needs_changes",
        "not_approved",
    }:
        return "changes_requested"
    return verdict_text


def _parse_text_verdict(text: str) -> str | None:
    normalized_text = text.strip().lower()
    if re.search(r"verdict\s*[:=-]\s*approved\b", normalized_text):
        return "approved"
    if re.search(r"verdict\s*[:=-]\s*changes?[_ -]requested\b", normalized_text):
        return "changes_requested"
    return None


def _int_field(payload: dict[str, object], key: str) -> int:
    try:
        return int(payload.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def build_pre_push_review_result_comment(
    *,
    verdict: str,
    reviewer: str,
    head_before: str,
    head_after: str,
    verification_passed: bool,
    findings_high: int,
    findings_medium: int,
    findings_low: int,
    action_summary: str,
    cycle: int,
) -> str:
    """Build the human-readable comment for a pre-push review result."""
    marker = format_event_marker(
        phase="pre_push_review",
        cycle=cycle,
        head_sha=head_after,
    )
    verification_line = "passed" if verification_passed else "failed"
    return "\n".join(
        [
            marker,
            "",
            "## Agent Runner Pre-Push Review",
            "",
            f"- Verdict: {verdict}",
            f"- Reviewer: {reviewer}",
            f"- Head Before: `{head_before}`",
            f"- Head After: `{head_after}`",
            f"- Verification: {verification_line}",
            f"- Findings: {findings_high} high, {findings_medium} medium, {findings_low} low",
            f"- Action: {action_summary}",
        ]
    )


def run_pre_push_review(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    selected_agent: str,
    head_sha_before: str,
    expected_branch: str,
    verification_results: list[CommandResult],
) -> tuple[str, list[CommandResult]]:
    """Run the pre-push review gate and return the final head SHA and verification results.

    Args:
        issue: The Issue being processed.
        worktree_path: Path to the worktree.
        config: Application configuration.
        github_client: GitHub client for comments.
        process_runner: Process runner for commands.
        selected_agent: Agent to use for review.
        head_sha_before: SHA before review starts.
        expected_branch: Branch the worktree should be on.
        verification_results: Existing verification results from implementation.

    Returns:
        Tuple of (final_head_sha, final_verification_results).
    """
    review_config = config.pre_push_review
    if not review_config.enabled:
        return head_sha_before, verification_results

    reviewer_agent = selected_agent if review_config.allow_same_agent else "codex"
    if review_config.review_agent != "auto":
        reviewer_agent = review_config.review_agent

    max_attempts = max(1, review_config.max_attempts)
    current_head = head_sha_before
    current_verification = list(verification_results)
    last_failure_summary = "Pre-push review did not approve the changes."

    for attempt_index in range(max_attempts):
        cycle = attempt_index + 1
        review_prompt = build_review_packet(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=process_runner,
            verification_results=current_verification,
            head_sha=current_head,
        )
        review_result = run_agent_with_prompt(
            reviewer_agent,
            review_prompt,
            worktree_path,
            process_runner,
            capture_output=True,
        )
        reviewer_text = extract_agent_response_text(review_result)
        reviewer_decision = parse_reviewer_decision(reviewer_text)

        # Check if reviewer requested changes via commit request
        request_path = worktree_path / ".agent-runner" / "commit-request.json"
        cycle_verdict = reviewer_decision.verdict
        if request_path.is_file():
            cycle_verdict = "changes_requested"
            try:
                current_verification = commit_requested_changes(
                    issue,
                    worktree_path,
                    config,
                    process_runner,
                    expected_branch=expected_branch,
                )
                current_head = get_head_sha(worktree_path, process_runner)
                action_summary = (
                    "reviewer patched and runner committed follow-up changes"
                )
            except EmptyCommitRequestError:
                # reviewer 写了 commit-request 却没有任何实际文件改动：这是良性
                # 空操作（例如建议的改动与现状一致，或上一轮 cycle 已提交修复），
                # 不应被当成 "patch failed" 而让整个 runner 硬失败。
                # 回退到 reviewer 解析出的真实 verdict：若为 approved 则收敛，
                # 若为 changes_requested 则继续循环并在用尽次数后走软失败路径。
                # commit_requested_changes 在抛出前已移除残留的 commit-request 文件。
                cycle_verdict = reviewer_decision.verdict
                if reviewer_decision.verdict == "approved":
                    action_summary = "reviewer approved with an empty commit request"
                else:
                    action_summary = (
                        "reviewer requested changes but produced no committable diff"
                    )
                last_failure_summary = action_summary
            except Exception as exc:  # noqa: BLE001
                action_summary = f"reviewer patch failed to commit: {exc}"
                last_failure_summary = action_summary
                github_client.comment_issue(
                    issue.number,
                    build_pre_push_review_result_comment(
                        verdict="changes requested",
                        reviewer=reviewer_agent,
                        head_before=head_sha_before,
                        head_after=current_head,
                        verification_passed=False,
                        findings_high=0,
                        findings_medium=0,
                        findings_low=0,
                        action_summary=action_summary,
                        cycle=cycle,
                    ),
                )
                if cycle >= max_attempts:
                    raise RuntimeError(f"Pre-push review repair failed: {exc}") from exc
                continue
        else:
            if reviewer_decision.verdict == "approved":
                action_summary = "reviewer approved without changes"
            elif reviewer_decision.parseable:
                action_summary = "reviewer requested changes without a commit request"
            else:
                action_summary = "reviewer returned no parseable verdict"
            last_failure_summary = action_summary

        comment_body = build_pre_push_review_result_comment(
            verdict="approved" if cycle_verdict == "approved" else "changes requested",
            reviewer=reviewer_agent,
            head_before=head_sha_before,
            head_after=current_head,
            verification_passed=all(r.return_code == 0 for r in current_verification),
            findings_high=reviewer_decision.findings_high,
            findings_medium=reviewer_decision.findings_medium,
            findings_low=reviewer_decision.findings_low,
            action_summary=action_summary,
            cycle=cycle,
        )
        github_client.comment_issue(issue.number, comment_body)
        if action_summary.startswith("reviewer approved") and all(
            result.return_code == 0 for result in current_verification
        ):
            return current_head, current_verification

    raise RuntimeError(
        "Pre-push review did not approve after "
        f"{max_attempts} attempt(s): {last_failure_summary}"
    )
