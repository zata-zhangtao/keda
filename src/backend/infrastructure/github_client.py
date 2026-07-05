"""GitHub CLI client implementation.

Public surface:

- :class:`GitHubCliClient` — small wrapper around the GitHub CLI that
  satisfies the ``IGitHubClient`` interface from
  ``backend.core.shared.interfaces.agent_runner`` via duck typing.
- :func:`sanitize_github_body` — body scrubber that strips C0 control
  characters and middle-truncates Markdown to stay under GitHub's
  POST size limit.

The actual ``gh`` command construction lives in focused helper modules:

- :mod:`backend.infrastructure.github_models` — frozen dataclasses and
  constants for return types.
- :mod:`backend.infrastructure.github_helpers` — pure module-level helpers
  (sanitisation, check rollup parsing, PR state normalisation).
- :mod:`backend.infrastructure.github_labels` — ``sync_labels`` and the
  static label spec table.
- :mod:`backend.infrastructure.github_issue_ops` — ``gh issue ...``
  invocations.
- :mod:`backend.infrastructure.github_pr_ops` — ``gh pr ...`` invocations.

The class methods below are 1-line delegations to those helpers. They
exist so callers can keep using the historical
``client.<domain_method>(...)`` calling convention.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Sequence

from backend.infrastructure.github_helpers import (
    _RETRYABLE_GH_ERROR_PATTERNS,
    sanitize_github_body,
)
from backend.infrastructure.github_issue_ops import (
    comment_issue,
    create_issue,
    edit_issue_body,
    edit_issue_comment,
    edit_issue_labels,
    get_issue,
    list_issue_comment_entries,
    list_issue_comments,
    list_issue_label_names,
    list_issues_by_label,
    list_ready_issues,
    list_rework_prd_issues,
    list_review_candidate_issues,
)
from backend.infrastructure.github_labels import sync_labels
from backend.infrastructure.github_models import (
    GhAuthStatus,
    IssueSummary,
    LabelConfig,
    PullRequestContext,
    PullRequestSummary,
)
from backend.infrastructure.github_pr_ops import (
    comment_pr,
    create_draft_pr,
    find_merged_pr_by_head,
    find_open_pr_by_head,
    get_pull_request_context,
    get_remote_base_sha,
    list_pr_comments,
    list_pull_requests_for_issue,
    merge_pull_request,
    update_pull_request_body,
)
from backend.infrastructure.process_runner import CommandResult, SubprocessRunner

_logger = logging.getLogger(__name__)

# Re-export data shapes from :mod:`backend.infrastructure.github_models`
# so existing ``from backend.infrastructure.github_client import GhAuthStatus``
# imports continue to work after the line-split refactor.
__all__ = [
    "GhAuthStatus",
    "GitHubCliClient",
    "IssueSummary",
    "LabelConfig",
    "PullRequestContext",
    "PullRequestSummary",
    "sanitize_github_body",
]

# Number of attempts for transient GitHub CLI network failures.
_MAX_GH_RETRIES = 3

# Delay between retries in seconds.
_GH_RETRY_DELAY_SECONDS = 1.0


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

    # --- core invocation primitives -------------------------------------

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
                    "GitHub CLI transient error (attempt %d/%d), retrying in %.1fs: %s",
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

    def _list_issue_label_names(self, issue_number: int) -> set[str]:
        """Convenience wrapper retained for backward compatibility."""
        return list_issue_label_names(self, issue_number)

    # --- auth + labels --------------------------------------------------

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
        sync_labels(self, labels)

    # --- issue delegations ---------------------------------------------

    def list_ready_issues(self, ready_label: str, limit: int) -> list[IssueSummary]:
        return list_ready_issues(self, ready_label, limit)

    def list_rework_prd_issues(self, rework_prd_label: str, limit: int) -> list[IssueSummary]:
        return list_rework_prd_issues(self, rework_prd_label, limit)

    def list_review_candidate_issues(self, labels: Sequence[str], limit: int) -> list[IssueSummary]:
        return list_review_candidate_issues(self, labels, limit)

    def list_issues_by_label(
        self, label: str | None, limit: int, state: str = "all"
    ) -> list[IssueSummary]:
        return list_issues_by_label(self, label, limit, state)

    def get_issue(self, issue_number: int) -> IssueSummary:
        return get_issue(self, issue_number)

    def edit_issue_labels(
        self,
        issue_number: int,
        *,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
    ) -> None:
        edit_issue_labels(self, issue_number, add=add, remove=remove)

    def comment_issue(self, issue_number: int, body: str) -> None:
        comment_issue(self, issue_number, body)

    def edit_issue_body(self, issue_number: int, body: str) -> None:
        edit_issue_body(self, issue_number, body)

    def create_issue(self, *, title: str, body: str, labels: Sequence[str]) -> str:
        return create_issue(self, title=title, body=body, labels=labels)

    def list_issue_comments(self, issue_number: int) -> list[str]:
        return list_issue_comments(self, issue_number)

    def list_issue_comment_entries(self, issue_number: int) -> list[tuple[int, str]]:
        return list_issue_comment_entries(self, issue_number)

    def edit_issue_comment(self, comment_id: int, body: str) -> None:
        edit_issue_comment(self, comment_id, body)

    # --- PR delegations -------------------------------------------------

    def create_draft_pr(self, *, title: str, body: str, base_branch: str, cwd: Path) -> str:
        return create_draft_pr(self, title=title, body=body, base_branch=base_branch, cwd=cwd)

    def get_pull_request_context(self, branch: str) -> PullRequestContext | None:
        return get_pull_request_context(self, branch)

    def comment_pr(self, pr_number: int, body: str) -> None:
        comment_pr(self, pr_number, body)

    def update_pull_request_body(self, pr_number: int, body: str) -> None:
        update_pull_request_body(self, pr_number, body)

    def merge_pull_request(self, pr_number: int, *, method: str = "squash") -> None:
        merge_pull_request(self, pr_number, method=method)

    def list_pr_comments(self, pr_number: int) -> list[str]:
        return list_pr_comments(self, pr_number)

    def find_open_pr_by_head(self, branch: str) -> str | None:
        return find_open_pr_by_head(self, branch)

    def find_merged_pr_by_head(self, branch: str) -> str | None:
        return find_merged_pr_by_head(self, branch)

    def get_remote_base_sha(self, remote: str, base_branch: str) -> str:
        return get_remote_base_sha(self, remote, base_branch)

    def list_pull_requests_for_issue(
        self, repo: str, issue_number: int
    ) -> list[PullRequestSummary]:
        return list_pull_requests_for_issue(self, repo, issue_number)
