"""GitHub CLI client implementation."""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from backend.infrastructure.process_runner import SubprocessRunner

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IssueSummary:
    """GitHub Issue selected for runner execution."""

    number: int
    title: str
    url: str
    body: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class LabelConfig:
    """GitHub labels used as runner queue state."""

    ready: str = "agent/ready"
    running: str = "agent/running"
    supervising: str = "agent/supervising"
    review: str = "agent/review"
    failed: str = "agent/failed"
    blocked: str = "agent/blocked"
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

    def __init__(
        self, repo_path: Path, process_runner: SubprocessRunner | None = None
    ) -> None:
        """Create the client.

        Args:
            repo_path: Target repository path.
            process_runner: Optional process runner to use for gh commands.
        """
        self.repo_path = repo_path
        self._runner = process_runner or SubprocessRunner()

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
        }
        configured_names.update(
            {f"agent/{k}": v for k, v in labels.agent_labels.items()}
        )
        for label_name, color, description in label_specs:
            effective_name = configured_names.get(label_name, label_name)
            self._runner.run(
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
        result = self._runner.run(
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
                "number,title,url,labels,body",
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
        self._runner.run(command, cwd=self.repo_path)

    def _list_issue_label_names(self, issue_number: int) -> set[str]:
        result = self._runner.run(
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
            comment_path = Path(temp_dir) / "comment.md"
            comment_path.write_text(body, encoding="utf-8")
            self._runner.run(
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

    def create_issue(
        self,
        *,
        title: str,
        body: str,
        labels: Sequence[str],
    ) -> str:
        """Create a GitHub Issue and return its URL."""
        with tempfile.TemporaryDirectory(prefix="iar-issue-") as temp_dir:
            body_path = Path(temp_dir) / "issue.md"
            body_path.write_text(body, encoding="utf-8")
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
            result = self._runner.run(command, cwd=self.repo_path)
        return result.stdout.strip().splitlines()[-1]

    def create_draft_pr(
        self, *, title: str, body: str, base_branch: str, cwd: Path
    ) -> str:
        """Create a draft pull request from the current branch."""
        with tempfile.TemporaryDirectory(prefix="iar-pr-") as temp_dir:
            body_path = Path(temp_dir) / "pr.md"
            body_path.write_text(body, encoding="utf-8")
            result = self._runner.run(
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

    def list_review_candidate_issues(
        self, labels: Sequence[str], limit: int
    ) -> list[IssueSummary]:
        """List open Issues with any of the given labels."""
        seen_numbers: set[int] = set()
        candidates: list[IssueSummary] = []
        for label in labels:
            result = self._runner.run(
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
                    "number,title,url,labels,body",
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
                    )
                )
        return candidates

    def get_pull_request_context(self, branch: str) -> PullRequestContext | None:
        """Return PR context for an open PR on the given branch."""
        result = self._runner.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "url,headRefName,headRefOid,baseRefOid,mergeable,statusCheckRollup",
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
        return PullRequestContext(
            pr_url=str(raw_pr.get("url", "")),
            branch=str(raw_pr.get("headRefName", branch)),
            head_sha=str(raw_pr.get("headRefOid", "")),
            base_sha=str(raw_pr.get("baseRefOid", "")),
            mergeable=_normalize_mergeable(raw_pr.get("mergeable")),
            checks_state=checks_state,
            checks_summary=checks_summary,
        )

    def list_issue_comments(self, issue_number: int) -> list[str]:
        """Return raw comment bodies for an Issue."""
        result = self._runner.run(
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

    def list_pr_comments(self, pr_number: int) -> list[str]:
        """Return raw comment bodies for a PR."""
        result = self._runner.run(
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
        result = self._runner.run(
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

    def get_remote_base_sha(self, remote: str, base_branch: str) -> str:
        """Return the SHA of the remote base branch."""
        result = self._runner.run(
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
