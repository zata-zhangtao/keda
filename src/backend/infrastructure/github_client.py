"""GitHub CLI client implementation."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from backend.infrastructure.process_runner import CommandResult, SubprocessRunner

_logger = logging.getLogger(__name__)

# GitHub rejects POST bodies above ~65,536 characters; stay well below it.
_MAX_GITHUB_BODY_LENGTH = 60000

# C0 control characters (except tab/newline/carriage-return) and DEL trip
# GitHub's request validation, which rejects the POST with a generic
# ``400 Bad Request`` ("Whoa there!") page. Agent CLI output forwarded into
# failure comments (e.g. ``claude --output-format stream-json --verbose``) can
# embed these raw bytes, so every body is scrubbed before it reaches ``gh``.
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_BODY_TRUNCATION_MARKER = "\n\n... (truncated to fit GitHub's size limit) ...\n\n"

# Number of attempts for transient GitHub CLI network failures.
_MAX_GH_RETRIES = 3

# Delay between retries in seconds.
_GH_RETRY_DELAY_SECONDS = 1.0

#: Extracts the numeric comment ID from a GitHub issue comment URL.
_COMMENT_ID_URL_PATTERN = re.compile(r"/issues/(?:\d+)#issuecomment-(\d+)")


def _extract_comment_id_from_url(url: str) -> int | None:
    """Return the numeric comment ID from a GitHub comment URL."""
    match = _COMMENT_ID_URL_PATTERN.search(url)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


# Patterns matched against combined stdout/stderr of a failed ``gh`` call to
# decide whether the failure is likely transient and worth retrying.
_RETRYABLE_GH_ERROR_PATTERNS = (
    re.compile(r"TLS handshake timeout", re.IGNORECASE),
    re.compile(r"connection timed out", re.IGNORECASE),
    re.compile(r"i/o timeout", re.IGNORECASE),
    re.compile(r"no such host", re.IGNORECASE),
    re.compile(r"temporary failure in name resolution", re.IGNORECASE),
    re.compile(r"500\s+Internal Server Error", re.IGNORECASE),
    re.compile(r"502\s+Bad Gateway", re.IGNORECASE),
    re.compile(r"503\s+Service Unavailable", re.IGNORECASE),
    re.compile(r"504\s+Gateway Timeout", re.IGNORECASE),
)


def sanitize_github_body(body: str, *, max_length: int = _MAX_GITHUB_BODY_LENGTH) -> str:
    """Strip request-breaking control characters and bound the body length.

    GitHub returns a generic ``400 Bad Request`` page for POST bodies that
    contain raw control characters or exceed its size limit. Both can occur
    when agent command output is embedded in a failure comment, so callers
    route every Markdown body through this guard before handing it to ``gh``.

    Args:
        body: The raw Markdown body to post.
        max_length: Maximum characters to keep; longer bodies are
            middle-truncated with a marker so both the start and the tail of
            the original content survive.

    Returns:
        A sanitized body safe to send to the GitHub CLI.
    """
    cleaned_body = _CONTROL_CHARACTER_PATTERN.sub("", body)
    if len(cleaned_body) <= max_length:
        return cleaned_body
    keep_length = max(0, max_length - len(_BODY_TRUNCATION_MARKER))
    head_length = keep_length * 2 // 3
    tail_length = keep_length - head_length
    tail = cleaned_body[-tail_length:] if tail_length else ""
    return cleaned_body[:head_length] + _BODY_TRUNCATION_MARKER + tail


@dataclass(frozen=True)
class IssueSummary:
    """GitHub Issue selected for runner execution."""

    number: int
    title: str
    url: str
    body: str
    labels: tuple[str, ...]
    state: str = "OPEN"


@dataclass(frozen=True)
class PullRequestSummary:
    """Local mirror of :class:`backend.core.shared.models.agent_runner.PullRequestSummary`."""

    number: int
    state: str
    url: str
    is_draft: bool
    merged: bool
    title: str


@dataclass(frozen=True)
class LabelConfig:
    """GitHub labels used as runner queue state."""

    ready: str = "agent/ready"
    running: str = "agent/running"
    supervising: str = "agent/supervising"
    review: str = "agent/review"
    failed: str = "agent/failed"
    blocked: str = "agent/blocked"
    waiting: str = "agent/waiting"
    validation_pending: str = "validation/pending"
    validation_passed: str = "validation/passed"
    group_prefix: str = "task-group/"
    rework_prd: str = "agent/rework-prd"
    deliberate: str = "agent/deliberate"
    agent_labels: dict[str, str] = field(
        default_factory=lambda: {
            "codex": "agent/codex",
            "claude": "agent/claude",
            "kimi": "agent/kimi",
        }
    )


@dataclass(frozen=True)
class PullRequestContext:
    """PR context returned by GitHub CLI."""

    pr_url: str
    branch: str
    head_sha: str
    base_sha: str
    mergeable: bool | None = None
    checks_state: str | None = None
    checks_summary: tuple[str, ...] = ()
    number: int | None = None
    body: str = ""


@dataclass(frozen=True)
class GhAuthStatus:
    """GitHub CLI authentication status."""

    authenticated: bool
    account: str | None = None
    failure_reason: str | None = None


_CHECK_FAILURE_STATES = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "ERROR",
    "FAILURE",
    "FAILED",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}
_CHECK_PENDING_STATES = {
    "EXPECTED",
    "IN_PROGRESS",
    "PENDING",
    "QUEUED",
    "REQUESTED",
    "WAITING",
}
_CHECK_SUCCESS_STATES = {"NEUTRAL", "SKIPPED", "SUCCESS"}


def _normalize_optional_state(raw_value: object) -> str | None:
    """Normalize GitHub CLI check state values for comparison."""
    if raw_value is None:
        return None
    normalized_value = str(raw_value).strip().upper()
    return normalized_value or None


def _normalize_mergeable(raw_mergeable: object) -> bool | None:
    """Normalize GitHub CLI mergeable enum output to the core boolean model."""
    if isinstance(raw_mergeable, bool):
        return raw_mergeable

    mergeable_state = _normalize_optional_state(raw_mergeable)
    if mergeable_state in ("MERGEABLE", "TRUE"):
        return True
    if mergeable_state in ("CONFLICTING", "FALSE"):
        return False
    return None


def _extract_rollup_entries(raw_rollup: object) -> list[dict[str, object]]:
    """Return status check entries from supported GitHub CLI rollup shapes."""
    if isinstance(raw_rollup, list):
        return [entry for entry in raw_rollup if isinstance(entry, dict)]
    if isinstance(raw_rollup, dict):
        for key in ("nodes", "checks", "contexts"):
            raw_entries = raw_rollup.get(key)
            if isinstance(raw_entries, list):
                return [entry for entry in raw_entries if isinstance(entry, dict)]
    return []


def _check_display_name(raw_check: dict[str, object]) -> str:
    """Return a compact human name for a check rollup entry."""
    for key in ("name", "context", "workflowName"):
        raw_name = raw_check.get(key)
        if raw_name:
            return str(raw_name)
    return str(raw_check.get("__typename", "check"))


def _check_summary_line(raw_check: dict[str, object]) -> str:
    """Build a readable summary for a failed or pending check."""
    state_parts = []
    for key in ("status", "conclusion", "state"):
        raw_state = raw_check.get(key)
        if raw_state:
            state_parts.append(f"{key}={raw_state}")

    check_url = raw_check.get("detailsUrl") or raw_check.get("targetUrl")
    suffix = f" ({', '.join(state_parts)})" if state_parts else ""
    if check_url:
        suffix = f"{suffix} {check_url}".rstrip()
    return f"{_check_display_name(raw_check)}{suffix}"


def _check_entry_state(raw_check: dict[str, object]) -> str:
    """Classify one status check rollup entry."""
    conclusion = _normalize_optional_state(raw_check.get("conclusion"))
    state = _normalize_optional_state(raw_check.get("state"))
    status = _normalize_optional_state(raw_check.get("status"))

    for candidate_state in (conclusion, state):
        if candidate_state in _CHECK_FAILURE_STATES:
            return "FAILURE"
        if candidate_state in _CHECK_PENDING_STATES:
            return "PENDING"
        if candidate_state in _CHECK_SUCCESS_STATES:
            return "SUCCESS"

    if status in _CHECK_FAILURE_STATES:
        return "FAILURE"
    if status in _CHECK_PENDING_STATES:
        return "PENDING"
    if status == "COMPLETED":
        return "PENDING" if conclusion is None else "SUCCESS"
    if status in _CHECK_SUCCESS_STATES:
        return "SUCCESS"
    return "PENDING"


# Stable sort order for ``list_pull_requests_for_issue`` output. Open
# PRs first (incl. drafts), then closed, then merged last so the table
# always reads "in flight → historical".
_STATE_ORDER = {"open": 0, "draft": 1, "closed": 2, "merged": 3}


def _normalise_pr_state(raw_state: object, *, is_draft: bool, merged_at: object) -> str:
    """Map GitHub's PR state and draft flag into one of four buckets."""
    if merged_at:
        return "merged"
    if is_draft:
        return "draft"
    state_text = str(raw_state or "").upper()
    if state_text == "MERGED":
        return "merged"
    if state_text == "CLOSED":
        return "closed"
    return "open"


def _parse_pr_summary(raw_pr: dict[str, object]) -> PullRequestSummary:
    """Convert a ``gh pr list --json`` row into a PullRequestSummary."""
    merged_at = raw_pr.get("mergedAt") or ""
    is_draft = bool(raw_pr.get("isDraft", False))
    return PullRequestSummary(
        number=int(raw_pr.get("number", 0)),
        state=_normalise_pr_state(raw_pr.get("state"), is_draft=is_draft, merged_at=merged_at),
        url=str(raw_pr.get("url", "")),
        is_draft=is_draft,
        merged=bool(merged_at),
        title=str(raw_pr.get("title", "")),
    )


def _aggregate_status_check_rollup(
    raw_rollup: object,
) -> tuple[str | None, tuple[str, ...]]:
    """Aggregate GitHub CLI statusCheckRollup entries into a stable state."""
    rollup_entries = _extract_rollup_entries(raw_rollup)
    if not rollup_entries:
        return None, ()

    pending_summaries: list[str] = []
    failure_summaries: list[str] = []
    for raw_check in rollup_entries:
        check_state = _check_entry_state(raw_check)
        if check_state == "FAILURE":
            failure_summaries.append(_check_summary_line(raw_check))
        elif check_state == "PENDING":
            pending_summaries.append(_check_summary_line(raw_check))

    if failure_summaries:
        return "FAILURE", tuple(failure_summaries)
    if pending_summaries:
        return "PENDING", tuple(pending_summaries)
    return "SUCCESS", ()


class GitHubCliClient:
    """Small wrapper around the GitHub CLI.

    Implements the ``IGitHubClient`` interface from
    ``backend.core.shared.interfaces.agent_runner`` via duck typing.
    """

    def __init__(self, repo_path: Path, process_runner: SubprocessRunner | None = None) -> None:
        """Create the client.

        Args:
            repo_path: Target repository path.
            process_runner: Optional process runner to use for gh commands.
        """
        self.repo_path = repo_path
        self._runner = process_runner or SubprocessRunner()

    def _is_retryable_gh_error(self, exc: subprocess.CalledProcessError) -> bool:
        """Return True when a failed ``gh`` call looks like a transient network error."""
        combined_output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        return any(pattern.search(combined_output) for pattern in _RETRYABLE_GH_ERROR_PATTERNS)

    def _run_with_retry(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout: int | None = None,
        capture_output: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        """Run a ``gh`` command, retrying a limited number of times on transient errors.

        Retries are only attempted when ``check=True`` and the failure matches
        one of the known transient network patterns (timeouts, DNS failures,
        or HTTP 5xx responses from GitHub).
        """

        last_exc: subprocess.CalledProcessError | None = None
        for attempt in range(1, _MAX_GH_RETRIES + 1):
            try:
                return self._runner.run(
                    command,
                    cwd=cwd,
                    check=check,
                    timeout=timeout,
                    capture_output=capture_output,
                    input_text=input_text,
                )
            except subprocess.CalledProcessError as exc:
                last_exc = exc
                if not check or attempt >= _MAX_GH_RETRIES or not self._is_retryable_gh_error(exc):
                    raise
                _logger.warning(
                    "GitHub CLI transient error (attempt %d/%d), " "retrying in %.1fs: %s",
                    attempt,
                    _MAX_GH_RETRIES,
                    _GH_RETRY_DELAY_SECONDS,
                    exc.stderr.strip() if exc.stderr else str(exc),
                )
                time.sleep(_GH_RETRY_DELAY_SECONDS)
        # pragma: no cover - loop always returns or raises before exhausting.
        raise last_exc  # type: ignore[misc]

    def _write_body_file(self, temp_dir: str, filename: str, body: str) -> Path:
        """Sanitize a Markdown body and write it for a ``--body-file`` flag.

        Routing every body through :func:`sanitize_github_body` keeps raw
        control characters and oversized payloads from triggering GitHub's
        ``400 Bad Request`` rejection.
        """
        body_path = Path(temp_dir) / filename
        body_path.write_text(sanitize_github_body(body), encoding="utf-8")
        return body_path

    def check_auth_status(self) -> GhAuthStatus:
        """Run ``gh auth status`` and parse the result.

        Returns:
            GhAuthStatus indicating whether the user is authenticated.
        """
        result = self._run_with_retry(
            ["gh", "auth", "status", "--hostname", "github.com"],
            cwd=self.repo_path,
            check=False,
        )
        combined_output = (result.stdout or "") + "\n" + (result.stderr or "")

        if "✓ Logged in" in combined_output:
            account: str | None = None
            for line in combined_output.splitlines():
                if "✓ Logged in" in line and " as " in line:
                    account = line.split(" as ", 1)[1].strip().split()[0]
                    break
            return GhAuthStatus(authenticated=True, account=account)

        failure_reason: str | None = None
        for line in combined_output.splitlines():
            stripped = line.strip()
            if stripped.startswith("X Failed to log in"):
                failure_reason = stripped
                break
            if "invalid" in stripped.lower() or "expired" in stripped.lower():
                failure_reason = stripped

        if not failure_reason:
            failure_reason = "GitHub CLI 认证失败"

        return GhAuthStatus(authenticated=False, failure_reason=failure_reason)

    def sync_labels(self, labels: LabelConfig) -> None:
        """Create or update standard labels."""
        label_specs = [
            ("agent/ready", "0E8A16", "Issue is ready for a local AI runner to claim."),
            (
                "agent/running",
                "FBCA04",
                "Issue is currently being executed by a local AI runner.",
            ),
            (
                "agent/supervising",
                "C5DEF5",
                "PR exists and automatic post-PR supervisor is reviewing or reprocessing.",
            ),
            ("agent/review", "1D76DB", "AI runner opened work for human review."),
            ("agent/failed", "D73A4A", "AI runner failed and posted details."),
            ("agent/blocked", "000000", "AI runner needs human input."),
            (
                "agent/waiting",
                "FEF2C0",
                "Issue has unmet dependencies and is waiting for upstream closure.",
            ),
            (
                "agent/rework-prd",
                "D93F0B",
                "Request the AI runner to generate or rewrite this Issue's PRD.",
            ),
            (
                "agent/deliberate",
                "D4C5F9",
                "Issue needs multi-agent deliberation (Phase 0) before implementation.",
            ),
            (
                "validation/pending",
                "FBCA04",
                "Realistic Validation evidence awaits human sign-off on the PR.",
            ),
            (
                "validation/passed",
                "0E8A16",
                "A human verified the validation evidence and signed off.",
            ),
            (
                "source/prd",
                "0052CC",
                "Issue has a canonical PRD tracked in the repository.",
            ),
            ("type/feature", "1D76DB", "User-facing feature or capability work."),
            ("type/refactor", "5319E7", "Internal refactor or structural improvement."),
            ("type/bug", "D73A4A", "Broken behavior or regression fix."),
            ("status/backlog", "BFDADC", "Tracked work that is not in progress yet."),
        ]
        _agent_label_meta: dict[str, tuple[str, str]] = {
            "codex": ("5319E7", "Use Codex for local runner execution."),
            "claude": ("BFDADC", "Use Claude Code for local runner execution."),
            "kimi": ("FF6B6B", "Use Kimi for local runner execution."),
        }
        for agent_name, label_text in labels.agent_labels.items():
            color, description = _agent_label_meta.get(
                agent_name, ("5319E7", f"Use {agent_name} for local runner execution.")
            )
            label_specs.append((f"agent/{agent_name}", color, description))
        configured_names = {
            "agent/ready": labels.ready,
            "agent/running": labels.running,
            "agent/supervising": labels.supervising,
            "agent/review": labels.review,
            "agent/failed": labels.failed,
            "agent/blocked": labels.blocked,
            "agent/waiting": labels.waiting,
            "agent/rework-prd": labels.rework_prd,
            "agent/deliberate": labels.deliberate,
            "validation/pending": labels.validation_pending,
            "validation/passed": labels.validation_passed,
        }
        configured_names.update({f"agent/{k}": v for k, v in labels.agent_labels.items()})
        for label_name, color, description in label_specs:
            effective_name = configured_names.get(label_name, label_name)
            self._run_with_retry(
                [
                    "gh",
                    "label",
                    "create",
                    effective_name,
                    "--color",
                    color,
                    "--description",
                    description,
                    "--force",
                ],
                cwd=self.repo_path,
            )

    def list_ready_issues(self, ready_label: str, limit: int) -> list[IssueSummary]:
        """List open Issues with the ready label."""
        result = self._run_with_retry(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--label",
                ready_label,
                "--limit",
                str(limit),
                "--json",
                "number,title,url,labels,body,state",
            ],
            cwd=self.repo_path,
        )
        raw_issues = json.loads(result.stdout or "[]")
        return [
            IssueSummary(
                number=int(raw_issue["number"]),
                title=str(raw_issue.get("title", "")),
                url=str(raw_issue.get("url", "")),
                body=str(raw_issue.get("body", "") or ""),
                labels=tuple(
                    raw_label.get("name", "")
                    for raw_label in raw_issue.get("labels", [])
                    if raw_label.get("name")
                ),
                state=str(raw_issue.get("state", "OPEN") or "OPEN"),
            )
            for raw_issue in raw_issues
        ]

    def edit_issue_labels(
        self,
        issue_number: int,
        *,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
    ) -> None:
        """Add and remove Issue labels."""
        current_labels = self._list_issue_label_names(issue_number)
        labels_to_add = [label for label in add if label not in current_labels]
        requested_add_labels = set(add)
        labels_to_remove = [
            label
            for label in remove
            if label in current_labels and label not in requested_add_labels
        ]
        if not labels_to_add and not labels_to_remove:
            return

        command = ["gh", "issue", "edit", str(issue_number)]
        for label in labels_to_add:
            command.extend(["--add-label", label])
        for label in labels_to_remove:
            command.extend(["--remove-label", label])
        self._run_with_retry(command, cwd=self.repo_path)

    def _list_issue_label_names(self, issue_number: int) -> set[str]:
        result = self._run_with_retry(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--json",
                "labels",
            ],
            cwd=self.repo_path,
        )
        raw_issue = json.loads(result.stdout or "{}")
        return {
            str(raw_label.get("name", ""))
            for raw_label in raw_issue.get("labels", [])
            if raw_label.get("name")
        }

    def comment_issue(self, issue_number: int, body: str) -> None:
        """Post a Markdown comment to an Issue."""
        with tempfile.TemporaryDirectory(prefix="iar-comment-") as temp_dir:
            comment_path = self._write_body_file(temp_dir, "comment.md", body)
            self._run_with_retry(
                [
                    "gh",
                    "issue",
                    "comment",
                    str(issue_number),
                    "--body-file",
                    str(comment_path),
                ],
                cwd=self.repo_path,
            )

    def list_rework_prd_issues(self, rework_prd_label: str, limit: int) -> list[IssueSummary]:
        """List open Issues with the rework-prd label."""
        result = self._run_with_retry(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--label",
                rework_prd_label,
                "--limit",
                str(limit),
                "--json",
                "number,title,url,labels,body,state",
            ],
            cwd=self.repo_path,
        )
        raw_issues = json.loads(result.stdout or "[]")
        return [
            IssueSummary(
                number=int(raw_issue["number"]),
                title=str(raw_issue.get("title", "")),
                url=str(raw_issue.get("url", "")),
                body=str(raw_issue.get("body", "") or ""),
                labels=tuple(
                    raw_label.get("name", "")
                    for raw_label in raw_issue.get("labels", [])
                    if raw_label.get("name")
                ),
                state=str(raw_issue.get("state", "OPEN") or "OPEN"),
            )
            for raw_issue in raw_issues
        ]

    def edit_issue_body(self, issue_number: int, body: str) -> None:
        """Replace the body of an Issue."""
        with tempfile.TemporaryDirectory(prefix="iar-issue-body-") as temp_dir:
            body_path = Path(temp_dir) / "issue_body.md"
            body_path.write_text(body, encoding="utf-8")
            self._run_with_retry(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(issue_number),
                    "--body-file",
                    str(body_path),
                ],
                cwd=self.repo_path,
            )

    def create_issue(
        self,
        *,
        title: str,
        body: str,
        labels: Sequence[str],
    ) -> str:
        """Create a GitHub Issue and return its URL."""
        with tempfile.TemporaryDirectory(prefix="iar-issue-") as temp_dir:
            body_path = self._write_body_file(temp_dir, "issue.md", body)
            command = [
                "gh",
                "issue",
                "create",
                "--title",
                title,
                "--body-file",
                str(body_path),
            ]
            for label in labels:
                command.extend(["--label", label])
            result = self._run_with_retry(command, cwd=self.repo_path)
        return result.stdout.strip().splitlines()[-1]

    def create_draft_pr(self, *, title: str, body: str, base_branch: str, cwd: Path) -> str:
        """Create a draft pull request from the current branch."""
        with tempfile.TemporaryDirectory(prefix="iar-pr-") as temp_dir:
            body_path = self._write_body_file(temp_dir, "pr.md", body)
            result = self._run_with_retry(
                [
                    "gh",
                    "pr",
                    "create",
                    "--draft",
                    "--base",
                    base_branch,
                    "--title",
                    title,
                    "--body-file",
                    str(body_path),
                ],
                cwd=cwd,
            )
        return result.stdout.strip().splitlines()[-1]

    def list_review_candidate_issues(self, labels: Sequence[str], limit: int) -> list[IssueSummary]:
        """List open Issues with any of the given labels."""
        seen_numbers: set[int] = set()
        candidates: list[IssueSummary] = []
        for label in labels:
            result = self._run_with_retry(
                [
                    "gh",
                    "issue",
                    "list",
                    "--state",
                    "open",
                    "--label",
                    label,
                    "--limit",
                    str(limit),
                    "--json",
                    "number,title,url,labels,body,state",
                ],
                cwd=self.repo_path,
            )
            raw_issues = json.loads(result.stdout or "[]")
            for raw_issue in raw_issues:
                number = int(raw_issue["number"])
                if number in seen_numbers:
                    continue
                seen_numbers.add(number)
                candidates.append(
                    IssueSummary(
                        number=number,
                        title=str(raw_issue.get("title", "")),
                        url=str(raw_issue.get("url", "")),
                        body=str(raw_issue.get("body", "") or ""),
                        labels=tuple(
                            raw_label.get("name", "")
                            for raw_label in raw_issue.get("labels", [])
                            if raw_label.get("name")
                        ),
                        state=str(raw_issue.get("state", "OPEN") or "OPEN"),
                    )
                )
        return candidates

    def get_pull_request_context(self, branch: str) -> PullRequestContext | None:
        """Return PR context for an open PR on the given branch."""
        result = self._run_with_retry(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "url,number,body,headRefName,headRefOid,baseRefOid,mergeable,statusCheckRollup",
            ],
            cwd=self.repo_path,
            check=False,
        )
        if result.return_code != 0:
            _logger.warning(
                "Unable to load full PR context for branch %s: %s",
                branch,
                result.stderr.strip() or f"gh exited with status {result.return_code}",
            )
            return None
        raw_prs = json.loads(result.stdout or "[]")
        if not raw_prs:
            return None
        raw_pr = raw_prs[0]
        checks_state, checks_summary = _aggregate_status_check_rollup(
            raw_pr.get("statusCheckRollup")
        )
        raw_pr_number = raw_pr.get("number")
        return PullRequestContext(
            pr_url=str(raw_pr.get("url", "")),
            branch=str(raw_pr.get("headRefName", branch)),
            head_sha=str(raw_pr.get("headRefOid", "")),
            base_sha=str(raw_pr.get("baseRefOid", "")),
            mergeable=_normalize_mergeable(raw_pr.get("mergeable")),
            checks_state=checks_state,
            checks_summary=checks_summary,
            number=int(raw_pr_number) if raw_pr_number is not None else None,
            body=str(raw_pr.get("body", "") or ""),
        )

    def list_issue_comments(self, issue_number: int) -> list[str]:
        """Return raw comment bodies for an Issue."""
        result = self._run_with_retry(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--comments",
                "--json",
                "comments",
            ],
            cwd=self.repo_path,
            check=False,
        )
        if result.return_code != 0:
            return []
        raw_data = json.loads(result.stdout or "{}")
        comments = raw_data.get("comments", [])
        return [str(c.get("body", "")) for c in comments if c.get("body")]

    def list_issue_comment_entries(self, issue_number: int) -> list[tuple[int, str]]:
        """Return (comment_id, body) entries for an Issue.

        The numeric comment ID is parsed from the comment URL so callers can
        edit comments via the REST API. Comments without a usable URL are
        included with ``comment_id=0`` so ``list_issue_comments`` semantics are
        preserved and callers can still see the body.
        """
        result = self._run_with_retry(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--comments",
                "--json",
                "comments",
            ],
            cwd=self.repo_path,
            check=False,
        )
        if result.return_code != 0:
            return []
        raw_data = json.loads(result.stdout or "{}")
        comments = raw_data.get("comments", [])
        entries: list[tuple[int, str]] = []
        for raw_comment in comments:
            url = str(raw_comment.get("url", ""))
            comment_id = _extract_comment_id_from_url(url) or 0
            body = str(raw_comment.get("body", ""))
            entries.append((comment_id, body))
        return entries

    def edit_issue_comment(self, comment_id: int, body: str) -> None:
        """Edit an existing Issue comment."""
        owner_repo = self._get_owner_repo()
        with tempfile.TemporaryDirectory(prefix="iar-comment-edit-") as temp_dir:
            body_path = self._write_body_file(temp_dir, "comment.md", body)
            self._run_with_retry(
                [
                    "gh",
                    "api",
                    f"repos/{owner_repo}/issues/comments/{comment_id}",
                    "-X",
                    "PATCH",
                    "-F",
                    f"body@{body_path}",
                ],
                cwd=self.repo_path,
            )

    def _get_owner_repo(self) -> str:
        """Return 'owner/name' for the current repository."""
        result = self._run_with_retry(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            cwd=self.repo_path,
            check=False,
        )
        if result.return_code != 0:
            raise RuntimeError(f"Unable to determine repository owner/name: {result.stderr}")
        raw_data = json.loads(result.stdout or "{}")
        owner_repo = raw_data.get("nameWithOwner")
        if not owner_repo:
            raise RuntimeError("gh repo view did not return nameWithOwner")
        return str(owner_repo)

    def comment_pr(self, pr_number: int, body: str) -> None:
        """Post a Markdown comment to a Pull Request."""
        with tempfile.TemporaryDirectory(prefix="iar-pr-comment-") as temp_dir:
            comment_path = self._write_body_file(temp_dir, "comment.md", body)
            self._run_with_retry(
                [
                    "gh",
                    "pr",
                    "comment",
                    str(pr_number),
                    "--body-file",
                    str(comment_path),
                ],
                cwd=self.repo_path,
            )

    def update_pull_request_body(self, pr_number: int, body: str) -> None:
        """Replace the description body of a Pull Request."""
        with tempfile.TemporaryDirectory(prefix="iar-pr-body-") as temp_dir:
            body_path = self._write_body_file(temp_dir, "body.md", body)
            self._run_with_retry(
                [
                    "gh",
                    "pr",
                    "edit",
                    str(pr_number),
                    "--body-file",
                    str(body_path),
                ],
                cwd=self.repo_path,
            )

    def list_pr_comments(self, pr_number: int) -> list[str]:
        """Return raw comment bodies for a PR."""
        result = self._run_with_retry(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--comments",
                "--json",
                "comments",
            ],
            cwd=self.repo_path,
            check=False,
        )
        if result.return_code != 0:
            return []
        raw_data = json.loads(result.stdout or "{}")
        comments = raw_data.get("comments", [])
        return [str(c.get("body", "")) for c in comments if c.get("body")]

    def find_open_pr_by_head(self, branch: str) -> str | None:
        """Return PR URL if an open PR exists for the branch."""
        result = self._run_with_retry(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "url",
            ],
            cwd=self.repo_path,
            check=False,
        )
        if result.return_code != 0:
            return None
        raw_prs = json.loads(result.stdout or "[]")
        if not raw_prs:
            return None
        return str(raw_prs[0].get("url", ""))

    def find_merged_pr_by_head(self, branch: str) -> str | None:
        """Return PR URL if a merged PR exists for the branch."""
        result = self._run_with_retry(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "merged",
                "--json",
                "url",
            ],
            cwd=self.repo_path,
            check=False,
        )
        if result.return_code != 0:
            return None
        raw_prs = json.loads(result.stdout or "[]")
        if not raw_prs:
            return None
        return str(raw_prs[0].get("url", ""))

    def get_remote_base_sha(self, remote: str, base_branch: str) -> str:
        """Return the SHA of the remote base branch."""
        result = self._run_with_retry(
            [
                "git",
                "rev-parse",
                f"{remote}/{base_branch}",
            ],
            cwd=self.repo_path,
            check=False,
        )
        if result.return_code != 0:
            return ""
        return result.stdout.strip()

    def get_issue(self, issue_number: int) -> IssueSummary:
        """Return the Issue summary for the given issue number."""
        result = self._run_with_retry(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--json",
                "number,title,url,labels,body,state",
            ],
            cwd=self.repo_path,
            check=False,
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"Failed to fetch Issue #{issue_number}: {result.stderr.strip() or result.stdout}"
            )
        raw_issue = json.loads(result.stdout or "{}")
        return IssueSummary(
            number=int(raw_issue["number"]),
            title=str(raw_issue.get("title", "")),
            url=str(raw_issue.get("url", "")),
            body=str(raw_issue.get("body", "") or ""),
            labels=tuple(
                raw_label.get("name", "")
                for raw_label in raw_issue.get("labels", [])
                if raw_label.get("name")
            ),
            state=str(raw_issue.get("state", "OPEN") or "OPEN"),
        )

    def list_issues_by_label(
        self, label: str | None, limit: int, state: str = "all"
    ) -> list[IssueSummary]:
        """List Issues by label across open and closed states.

        When ``label`` is ``None``, the ``--label`` flag is omitted so
        the listing returns issues regardless of label.
        """
        command: list[str] = [
            "gh",
            "issue",
            "list",
            "--state",
            state,
            "--limit",
            str(limit),
            "--json",
            "number,title,url,labels,body,state",
        ]
        if label is not None:
            command[3:3] = ["--label", label]
        result = self._run_with_retry(command, cwd=self.repo_path)
        raw_issues = json.loads(result.stdout or "[]")
        return [
            IssueSummary(
                number=int(raw_issue["number"]),
                title=str(raw_issue.get("title", "")),
                url=str(raw_issue.get("url", "")),
                body=str(raw_issue.get("body", "") or ""),
                labels=tuple(
                    raw_label.get("name", "")
                    for raw_label in raw_issue.get("labels", [])
                    if raw_label.get("name")
                ),
                state=str(raw_issue.get("state", "OPEN") or "OPEN"),
            )
            for raw_issue in raw_issues
        ]

    def list_pull_requests_for_issue(
        self, repo: str, issue_number: int
    ) -> list[PullRequestSummary]:
        """List PRs that reference or close the given Issue.

        Uses ``gh pr list --search`` to find PRs whose body or commits
        mention the Issue via closing keywords. State is normalised to
        one of ``"open"`` / ``"draft"`` / ``"merged"`` / ``"closed"``.
        """
        search_query = (
            f"closes:#{issue_number} OR fixes:#{issue_number} "
            f"OR resolves:#{issue_number} OR refs:#{issue_number}"
        )
        command = [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--search",
            search_query,
            "--state",
            "all",
            "--limit",
            "100",
            "--json",
            "number,title,state,url,isDraft,mergedAt",
        ]
        result = self._run_with_retry(command, cwd=self.repo_path)
        raw_prs = json.loads(result.stdout or "[]")
        pulls = [_parse_pr_summary(raw_pr) for raw_pr in raw_prs]
        pulls.sort(key=lambda pull: (_STATE_ORDER.get(pull.state, 99), pull.number))
        return pulls
