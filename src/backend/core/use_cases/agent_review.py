"""Pre-PR AI review gate for the agent runner."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    ReviewFinding,
)
from backend.core.use_cases.agent_runner_events import (
    format_event_marker,
)
from backend.core.use_cases.agent_runner_failure import (
    ProviderCapacityError,
    format_agent_execution_failure,
    is_provider_capacity_failure,
)
from backend.core.use_cases.run_agent_once import (
    EmptyCommitRequestError,
    commit_requested_changes,
    extract_agent_response_text,
    extract_prd_path,
    get_head_sha,
    run_agent_with_prompt_resilient,
)

_logger = logging.getLogger(__name__)

_VALID_REVIEW_VERDICTS = {"approved", "changes_requested"}
_COMMIT_REQUEST_RELATIVE_PATH = Path(".agent-runner/commit-request.json")

# Default review rules appended after the review packet. The default instructs
# the reviewer to invoke the ``code-reviewer`` skill via the Skill tool and to
# emit structured findings so the runner can converge automatically.
DEFAULT_REVIEW_PROMPT_TEMPLATE: tuple[str, ...] = (
    "Before writing your verdict, call the `code-reviewer` skill using the Skill tool "
    "with the diff and PRD context above.",
    "Use the skill's findings to populate the `findings` array in your response.",
    "If the skill reports no findings, verdict must be `approved`.",
    "If findings exist, apply fixes in the worktree and write "
    "`.agent-runner/commit-request.json` with a descriptive `commit_message`.",
    "Do not leave findings unaddressed while returning `approved`.",
    "",
    "CRITICAL: The `code-reviewer` skill's Chinese text report is input for your "
    "judgment, NOT your final answer to the runner. After calling the skill, you "
    "MUST still produce a final ```json code block with the verdict/summary/findings "
    "schema below. The runner parses only that JSON block; without it the review "
    "fails with 'no parseable verdict'.",
    "",
    "Findings JSON schema:",
    "```json",
    "[",
    "  {",
    '    "category": "requirement|code|validation|docs",',
    '    "severity": "critical|high|medium|low",',
    '    "file": "path/to/file.py",',
    '    "line": 42,',
    '    "title": "short title",',
    '    "description": "why this is a problem",',
    '    "recommendation": "how to fix"',
    "  }",
    "]",
    "```",
    "",
    "Final response must be a single JSON object in a markdown code block with:",
    "- verdict: one of `approved`, `changes_requested`.",
    "- summary: short rationale.",
    "- findings: array of objects matching the schema above (may be empty).",
)


@dataclass(frozen=True)
class ReviewerDecision:
    """Parsed pre-PR reviewer decision."""

    verdict: str
    summary: str = ""
    findings: tuple[ReviewFinding, ...] = ()
    findings_critical: int = 0
    findings_high: int = 0
    findings_medium: int = 0
    findings_low: int = 0
    parseable: bool = True

    @property
    def has_findings(self) -> bool:
        """Return True when at least one structured finding was captured."""
        return bool(self.findings)

    def recomputed_counts(self) -> tuple[int, int, int, int]:
        """Return ``(critical, high, medium, low)`` counts derived from findings."""
        critical = high = medium = low = 0
        for finding in self.findings:
            severity = finding.severity.strip().lower()
            if severity == "critical":
                critical += 1
            elif severity == "high":
                high += 1
            elif severity == "medium":
                medium += 1
            elif severity == "low":
                low += 1
        return critical, high, medium, low


def _resolve_review_prompt_template(config: AppConfig) -> tuple[str, ...]:
    """Return the configured review rules template, falling back to default."""
    override = getattr(config.pre_pr_review, "review_prompt_template", ()) or ()
    if override:
        return tuple(str(line) for line in override)
    return DEFAULT_REVIEW_PROMPT_TEMPLATE


def build_review_packet(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    verification_results: list[CommandResult],
    head_sha: str,
) -> str:
    """Build the context packet sent to the pre-PR reviewer."""
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
    diff_text = diff_result.stdout if diff_result.return_code == 0 else "(diff unavailable)"

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

    review_rules = "\n".join(_resolve_review_prompt_template(config))

    return "\n".join(
        [
            f"Pre-PR Review for Issue #{issue.number}: {issue.title}",
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
            review_rules,
        ]
    )


def parse_reviewer_decision(text: str) -> ReviewerDecision:
    """Parse reviewer verdict and findings from agent output."""
    payload = _extract_json_payload(text)
    if payload is not None:
        verdict = _normalize_review_verdict(payload.get("verdict"))
        if verdict in _VALID_REVIEW_VERDICTS:
            findings = _parse_findings_array(payload.get("findings"))
            decision = ReviewerDecision(
                verdict=verdict,
                summary=str(payload.get("summary", "")),
                findings=findings,
                findings_critical=0,
                findings_high=_int_field(payload, "findings_high"),
                findings_medium=_int_field(payload, "findings_medium"),
                findings_low=_int_field(payload, "findings_low"),
            )
            return _finalize_decision_counts(decision)

    fallback_verdict = _parse_text_verdict(text)
    if fallback_verdict is not None:
        return ReviewerDecision(verdict=fallback_verdict, summary=text.strip())

    return ReviewerDecision(
        verdict="changes_requested",
        summary="Reviewer did not return a parseable verdict.",
        parseable=False,
    )


def _read_commit_request_decision(request_path: Path) -> ReviewerDecision | None:
    """Read optional reviewer metadata from a commit request JSON file."""
    try:
        with request_path.open("r", encoding="utf-8") as request_file:
            request_payload = json.load(request_file)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(request_payload, dict):
        return None
    verdict = _normalize_review_verdict(request_payload.get("verdict"))
    if verdict not in _VALID_REVIEW_VERDICTS:
        return None
    findings = _parse_findings_array(request_payload.get("findings"))
    decision = ReviewerDecision(
        verdict=verdict,
        summary=str(request_payload.get("summary", "")),
        findings=findings,
        findings_critical=0,
        findings_high=_int_field(request_payload, "findings_high"),
        findings_medium=_int_field(request_payload, "findings_medium"),
        findings_low=_int_field(request_payload, "findings_low"),
    )
    return _finalize_decision_counts(decision)


def _merge_reviewer_decisions(
    stdout_decision: ReviewerDecision,
    commit_request_decision: ReviewerDecision | None,
) -> ReviewerDecision:
    """Prefer stdout verdicts, falling back to commit-request metadata.

    The stdout decision is the authoritative source for findings when it is
    parseable; the commit-request file is only used to recover a missing
    verdict, never to override structured findings.
    """
    if stdout_decision.parseable or commit_request_decision is None:
        return stdout_decision
    # When stdout is not parseable, fall back to the commit-request verdict
    # wholesale so reviewers that only encode their decision in the request
    # file still drive the gate correctly.
    return commit_request_decision


def _parse_findings_array(raw: object) -> tuple[ReviewFinding, ...]:
    """Convert a JSON ``findings`` array into a tuple of ``ReviewFinding``.

    Missing or malformed entries fall back to defaults so partial output from
    the reviewer still produces usable comment data instead of breaking the
    gate.
    """
    if not isinstance(raw, list):
        return ()
    findings: list[ReviewFinding] = []
    for entry in raw:
        if isinstance(entry, dict):
            findings.append(
                ReviewFinding(
                    category=str(entry.get("category", "")),
                    severity=str(entry.get("severity", "")),
                    title=str(entry.get("title", "")),
                    description=str(entry.get("description", "")),
                    file=str(entry.get("file", "")),
                    line=_safe_int(entry.get("line")),
                    recommendation=str(entry.get("recommendation", "")),
                )
            )
        elif isinstance(entry, str) and entry.strip():
            # Allow legacy string-array findings ("first finding") by treating
            # the string as a description on a medium-severity finding.
            findings.append(
                ReviewFinding(
                    severity="medium",
                    title=entry.strip()[:120],
                    description=entry.strip(),
                )
            )
    return tuple(findings)


def _safe_int(value: object) -> int:
    """Best-effort coercion to ``int``; returns 0 for any non-numeric input."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _finalize_decision_counts(
    decision: ReviewerDecision,
    fallback_counts: tuple[int, int, int, int] | None = None,
) -> ReviewerDecision:
    """Recompute severity counts from findings and reconcile verdict.

    The reviewer-supplied counts (or any provided fallback) are ignored when
    findings are present, since the parser is the only authoritative source.
    When findings exist alongside ``verdict == "approved"``, the verdict is
    downgraded to ``changes_requested`` so the gate does not silently
    approve unreviewed issues.
    """
    if decision.has_findings:
        critical, high, medium, low = decision.recomputed_counts()
    elif fallback_counts is not None:
        critical, high, medium, low = fallback_counts
    else:
        critical = decision.findings_critical
        high = decision.findings_high
        medium = decision.findings_medium
        low = decision.findings_low

    new_verdict = decision.verdict
    new_summary = decision.summary
    if decision.has_findings and decision.verdict == "approved":
        _logger.warning(
            "Reviewer returned verdict=approved with %d finding(s); "
            "downgrading to changes_requested.",
            len(decision.findings),
        )
        new_verdict = "changes_requested"
        if not new_summary:
            new_summary = "Findings reported without explicit changes_requested."

    if (
        new_verdict == decision.verdict
        and new_summary == decision.summary
        and critical == decision.findings_critical
        and high == decision.findings_high
        and medium == decision.findings_medium
        and low == decision.findings_low
    ):
        return decision

    return ReviewerDecision(
        verdict=new_verdict,
        summary=new_summary,
        findings=decision.findings,
        findings_critical=critical,
        findings_high=high,
        findings_medium=medium,
        findings_low=low,
        parseable=decision.parseable,
    )


def _extract_json_payload(text: str) -> dict[str, object] | None:
    r"""Recover a JSON object from reviewer output, tolerating nested objects.

    Prefers fenced ``\`\`\`json\`\`\`` blocks, then tries the outermost balanced
    brace pair following ``"verdict"`` so reviews that emit nested finding
    objects (which break the greedy regex fallback) still parse.
    """
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        json_text = match.group(1)
    else:
        candidate = _extract_outermost_json_object(text)
        if candidate is None:
            return None
        json_text = candidate
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        payload = _loads_with_trailing_comma_fix(json_text)
        if payload is None:
            return None
    return payload if isinstance(payload, dict) else None


def _loads_with_trailing_comma_fix(json_text: str) -> dict[str, object] | None:
    """Retry ``json.loads`` after stripping trailing commas from objects/arrays.

    The reviewer is allowed to emit slightly malformed JSON (trailing commas
    inside arrays). Fixing them in place lets the parser recover the verdict
    and findings without forcing the runner to hard-fail on cosmetic issues.
    """
    cleaned = re.sub(r",(\s*[}\]])", r"\1", json_text)
    if cleaned == json_text:
        return None
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_outermost_json_object(text: str) -> str | None:
    """Return the substring of the outermost ``{...}`` containing ``"verdict"``."""
    verdict_match = re.search(r'"verdict"\s*:\s*"', text)
    if verdict_match is None:
        return None
    start = text.rfind("{", 0, verdict_match.start())
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


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
    # 引号可选：兼容纯文本（verdict: approved）与残缺 JSON（"verdict": "approved"）。
    normalized_text = text.strip().lower()
    if re.search(r"[\"']?verdict[\"']?\s*[:=-]\s*[\"']?approved\b", normalized_text):
        return "approved"
    if re.search(
        r"[\"']?verdict[\"']?\s*[:=-]\s*[\"']?changes?[_ -]requested\b",
        normalized_text,
    ):
        return "changes_requested"
    return None


def _int_field(payload: dict[str, object], key: str) -> int:
    try:
        return int(payload.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def build_pre_pr_review_result_comment(
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
    findings: tuple[ReviewFinding, ...] = (),
    findings_critical: int = 0,
) -> str:
    """Build the human-readable comment for a pre-PR review result."""
    marker = format_event_marker(
        phase="pre_pr_review",
        cycle=cycle,
        head_sha=head_after,
    )
    verification_line = "passed" if verification_passed else "failed"
    counts_line = (
        f"- Findings: {findings_critical} critical, {findings_high} high, "
        f"{findings_medium} medium, {findings_low} low"
    )
    sections = [
        marker,
        "",
        "## Agent Runner Pre-PR Review",
        "",
        f"- Verdict: {verdict}",
        f"- Reviewer: {reviewer}",
        f"- Head Before: `{head_before}`",
        f"- Head After: `{head_after}`",
        f"- Verification: {verification_line}",
        counts_line,
        f"- Action: {action_summary}",
    ]
    if findings:
        sections.append("")
        sections.append("### Findings")
        sections.append("")
        sections.append("| Severity | Category | File | Line | Title | Recommendation |")
        sections.append("|---|---|---|---|---|---|")
        for finding in findings:
            sections.append(
                "| {sev} | {cat} | {file} | {line} | {title} | {rec} |".format(
                    sev=_escape_cell(finding.severity or "-"),
                    cat=_escape_cell(finding.category or "-"),
                    file=_escape_cell(finding.file or "-"),
                    line=finding.line if finding.line else "-",
                    title=_escape_cell(finding.title or "-"),
                    rec=_escape_cell(finding.recommendation or "-"),
                )
            )
    return "\n".join(sections)


def _escape_cell(value: str) -> str:
    """Escape a value so it can be safely embedded in a markdown table cell."""
    return value.replace("|", "\\|").replace("\n", " ").strip() or "-"


def _build_commit_request_reminder_prompt(
    review_prompt: str,
    findings: tuple[ReviewFinding, ...],
    reminder_index: int,
) -> str:
    """Append a strict reminder when a reviewer listed findings but no patch.

    The reminder is injected back into the same review cycle so the reviewer
    gets another chance to produce ``.agent-runner/commit-request.json``
    instead of leaving the runner with findings it cannot apply automatically.
    """
    finding_lines: list[str] = []
    for finding in findings:
        location = f"{finding.file}:{finding.line}" if finding.file else "unknown location"
        finding_lines.append(
            f"- [{location}] {finding.severity}: {finding.title}\n"
            f"  {finding.description}\n"
            f"  Recommendation: {finding.recommendation}"
        )
    findings_block = "\n".join(finding_lines) or "(no structured findings)"
    reminder = (
        f"\n\nREMINDER #{reminder_index}: The review above reported findings "
        "but did not create `.agent-runner/commit-request.json`. "
        "You MUST now apply concrete fixes in the worktree and write "
        "`.agent-runner/commit-request.json` with a descriptive `commit_message`. "
        "Do not just list findings; produce a patch that addresses every item."
        "\n\nFindings that must be addressed:\n"
        f"{findings_block}"
    )
    return review_prompt + reminder


def run_pre_pr_review(
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
    push_callback: Callable[[], None] | None = None,
) -> tuple[str, list[CommandResult]]:
    """Run the pre-PR review gate and return the final head SHA and verification results.

    The review runs **after** the implementation commit has been pushed to the
    remote. Each successful reviewer patch is itself pushed via ``push_callback``
    so the remote branch always reflects the latest committed state when the
    Draft PR is created.

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
        push_callback: Optional callable invoked after each reviewer patch is
            committed locally. Implementations should run the standard push
            safety checks and ``git push`` for ``expected_branch``.

    Returns:
        Tuple of (final_head_sha, final_verification_results).

    Raises:
        RuntimeError: If review does not converge within ``max_attempts``.
    """
    review_config = config.pre_pr_review
    if not review_config.enabled:
        _logger.info("Pre-PR review disabled for Issue #%d.", issue.number)
        return head_sha_before, verification_results

    reviewer_agent = selected_agent if review_config.allow_same_agent else "codex"
    if review_config.review_agent != "auto":
        reviewer_agent = review_config.review_agent

    max_attempts = max(1, review_config.max_attempts)
    timeout_seconds = max(1, review_config.timeout_seconds)
    current_head = head_sha_before
    current_verification = list(verification_results)
    last_failure_summary = "Pre-PR review did not approve the changes."
    last_decision: ReviewerDecision | None = None
    last_action_summary = last_failure_summary
    last_cycle_verdict = "changes_requested"
    last_cycle_applied_patch = False
    _logger.info(
        "Starting pre-PR review for Issue #%d with reviewer '%s' "
        "(max_attempts=%d, timeout=%ds, head=%s).",
        issue.number,
        reviewer_agent,
        max_attempts,
        timeout_seconds,
        head_sha_before,
    )

    for attempt_index in range(max_attempts):
        cycle = attempt_index + 1
        attempt_started_at = time.monotonic()
        _logger.info(
            "Pre-PR review cycle %d/%d for Issue #%d: building review packet.",
            cycle,
            max_attempts,
            issue.number,
        )
        review_prompt = build_review_packet(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=process_runner,
            verification_results=current_verification,
            head_sha=current_head,
        )
        max_inner_attempts = max(0, review_config.commit_request_reminder_attempts)
        for inner_attempt in range(max_inner_attempts + 1):
            _logger.info(
                "Pre-PR review cycle %d/%d for Issue #%d: running reviewer '%s' "
                "(inner attempt %d/%d).",
                cycle,
                max_attempts,
                issue.number,
                reviewer_agent,
                inner_attempt + 1,
                max_inner_attempts + 1,
            )
            try:
                review_result = run_agent_with_prompt_resilient(
                    reviewer_agent,
                    review_prompt,
                    worktree_path,
                    process_runner,
                    capture_output=True,
                    timeout_seconds=timeout_seconds,
                    issue=issue,
                    transient_retry_attempts=(config.runner.transient_retry_attempts),
                    transient_retry_delay_seconds=(config.runner.transient_retry_delay_seconds),
                )
            except (subprocess.CalledProcessError, OSError) as exc:
                # Transient blips are already retried inside the resilient
                # wrapper. A provider-capacity failure here will keep failing on
                # the same reviewer agent, so escalate to let the cross-agent
                # fallback switch agents instead of failing the Issue.
                if is_provider_capacity_failure(exc):
                    raise ProviderCapacityError(format_agent_execution_failure(exc), []) from exc
                raise
            reviewer_text = extract_agent_response_text(review_result)
            stdout_decision = parse_reviewer_decision(reviewer_text)

            # Check if reviewer requested changes via commit request
            request_path = worktree_path / _COMMIT_REQUEST_RELATIVE_PATH
            commit_request_decision = (
                _read_commit_request_decision(request_path) if request_path.is_file() else None
            )
            reviewer_decision = _merge_reviewer_decisions(
                stdout_decision,
                commit_request_decision,
            )
            _logger.info(
                "Pre-PR review cycle %d/%d for Issue #%d: parsed verdict=%s "
                "(inner attempt %d/%d, stdout_parseable=%s, "
                "commit_request_verdict=%s, findings=%d).",
                cycle,
                max_attempts,
                issue.number,
                reviewer_decision.verdict,
                inner_attempt + 1,
                max_inner_attempts + 1,
                stdout_decision.parseable,
                commit_request_decision.verdict if commit_request_decision else "none",
                len(reviewer_decision.findings),
            )

            request_path_was_present = request_path.is_file()
            missing_commit_request = (
                reviewer_decision.has_findings
                and reviewer_decision.verdict == "changes_requested"
                and not request_path_was_present
            )
            if missing_commit_request and inner_attempt < max_inner_attempts:
                _logger.info(
                    "Pre-PR review cycle %d/%d for Issue #%d: reviewer reported "
                    "%d finding(s) without a commit request; re-prompting.",
                    cycle,
                    max_attempts,
                    issue.number,
                    len(reviewer_decision.findings),
                )
                review_prompt = _build_commit_request_reminder_prompt(
                    review_prompt,
                    reviewer_decision.findings,
                    reminder_index=inner_attempt + 1,
                )
                continue
            break

        elapsed_seconds = time.monotonic() - attempt_started_at
        _logger.info(
            "Pre-PR review cycle %d/%d for Issue #%d: reviewer exited with code %d after %.1fs.",
            cycle,
            max_attempts,
            issue.number,
            review_result.return_code,
            elapsed_seconds,
        )
        cycle_verdict = reviewer_decision.verdict
        request_path_was_present = request_path.is_file()
        if request_path_was_present:
            _logger.info(
                "Pre-PR review cycle %d/%d for Issue #%d: reviewer wrote "
                "commit request; processing through commit proxy.",
                cycle,
                max_attempts,
                issue.number,
            )
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
                if push_callback is not None:
                    _logger.info(
                        "Pre-PR review cycle %d/%d for Issue #%d: pushing "
                        "reviewer patch from %s to remote.",
                        cycle,
                        max_attempts,
                        issue.number,
                        current_head,
                    )
                    push_callback()
                # commit_requested_changes 正常返回意味着补丁已通过 staging 后
                # 的 verification 重跑（失败会抛 VerificationFailedError）。
                # 因此 approved + 补丁提交成功应当轮收敛，而不是被强制降级为
                # changes_requested 后在最后一轮必然硬失败。
                if reviewer_decision.verdict == "approved":
                    cycle_verdict = "approved"
                    action_summary = "reviewer approved and runner committed follow-up patch"
                else:
                    action_summary = "reviewer patched and runner committed follow-up changes"
                last_failure_summary = action_summary
                last_cycle_applied_patch = True
                _logger.info(
                    "Pre-PR review cycle %d/%d for Issue #%d: reviewer "
                    "changes committed at head %s.",
                    cycle,
                    max_attempts,
                    issue.number,
                    current_head,
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
                    action_summary = "reviewer requested changes but produced no committable diff"
                last_failure_summary = action_summary
                _logger.info(
                    "Pre-PR review cycle %d/%d for Issue #%d: empty commit "
                    "request handled as verdict=%s.",
                    cycle,
                    max_attempts,
                    issue.number,
                    reviewer_decision.verdict,
                )
            except Exception as exc:  # noqa: BLE001
                action_summary = f"reviewer patch failed to commit: {exc}"
                last_failure_summary = action_summary
                _logger.exception(
                    "Pre-PR review cycle %d/%d for Issue #%d: reviewer commit request failed.",
                    cycle,
                    max_attempts,
                    issue.number,
                )
                github_client.comment_issue(
                    issue.number,
                    build_pre_pr_review_result_comment(
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
                    raise RuntimeError(f"Pre-PR review repair failed: {exc}") from exc
                continue
        else:
            if reviewer_decision.verdict == "approved":
                action_summary = "reviewer approved without changes"
            elif reviewer_decision.parseable:
                if reviewer_decision.has_findings:
                    action_summary = "reviewer reported findings but produced no commit request"
                else:
                    action_summary = "reviewer requested changes without a commit request"
            else:
                action_summary = "reviewer returned no parseable verdict"
            last_failure_summary = action_summary

        comment_body = build_pre_pr_review_result_comment(
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
            findings=reviewer_decision.findings,
            findings_critical=reviewer_decision.findings_critical,
        )
        github_client.comment_issue(issue.number, comment_body)
        _logger.info(
            "Pre-PR review cycle %d/%d for Issue #%d: wrote result comment "
            "with verdict=%s and action=%s.",
            cycle,
            max_attempts,
            issue.number,
            "approved" if cycle_verdict == "approved" else "changes_requested",
            action_summary,
        )
        last_decision = reviewer_decision
        last_action_summary = action_summary
        last_cycle_verdict = cycle_verdict
        last_cycle_had_commit_request = bool(request_path_was_present)
        if action_summary.startswith("reviewer approved") and all(
            result.return_code == 0 for result in current_verification
        ):
            _logger.info(
                "Pre-PR review approved Issue #%d after %d cycle(s).",
                issue.number,
                cycle,
            )
            return current_head, current_verification

    # 最后一轮仍未收敛：若 reviewer 已提供最终修复 commit request，runner
    # 必须接受并继续发布；否则软失败并保留 findings 详情。
    final_decision = last_decision
    if final_decision is None:
        final_decision = ReviewerDecision(
            verdict="changes_requested",
            summary="Pre-PR review produced no parseable decision.",
        )
    # 收敛兜底：最后一轮 reviewer 写出了 commit request 且 verification 全部通过，
    # 即便 verdict 不是 "approved"，runner 也必须接受该最终修复并继续发布流程。
    if (
        last_cycle_had_commit_request
        and last_cycle_applied_patch
        and last_cycle_verdict == "changes_requested"
        and all(result.return_code == 0 for result in current_verification)
    ):
        _logger.info(
            "Pre-PR review accepted final commit-request fixes for "
            "Issue #%d after exhausting %d cycle(s).",
            issue.number,
            max_attempts,
        )
        return current_head, current_verification
    # The per-cycle comment above already recorded findings for the last
    # attempt; only emit the trailing summary comment when no per-cycle
    # comment has been written yet (defensive guard for harness coverage
    # where cycles were skipped).
    if max_attempts == 0:
        github_client.comment_issue(
            issue.number,
            build_pre_pr_review_result_comment(
                verdict="changes requested",
                reviewer=reviewer_agent,
                head_before=head_sha_before,
                head_after=current_head,
                verification_passed=all(r.return_code == 0 for r in current_verification),
                findings_high=final_decision.findings_high,
                findings_medium=final_decision.findings_medium,
                findings_low=final_decision.findings_low,
                action_summary=last_action_summary,
                cycle=max_attempts,
                findings=final_decision.findings,
                findings_critical=final_decision.findings_critical,
            ),
        )
    if last_cycle_verdict == "approved":
        _logger.info(
            "Pre-PR review accepted final approval for Issue #%d on the last cycle.",
            issue.number,
        )
        return current_head, current_verification

    _logger.warning(
        "Pre-PR review did not approve Issue #%d after %d attempt(s): %s",
        issue.number,
        max_attempts,
        last_failure_summary,
    )
    raise RuntimeError(
        f"Pre-PR review did not approve after {max_attempts} attempt(s): {last_failure_summary}"
    )
