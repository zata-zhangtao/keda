"""Pull-request-side operations for the GitHub CLI client.

Module-level helper functions that drive ``gh pr ...`` invocations. They
take a client-like first argument so the main
:class:`backend.infrastructure.github_client.GitHubCliClient` can keep its
method-level public surface while delegating the actual ``gh`` command
construction here.

Backward compatibility: ``GitHubCliClient.create_draft_pr`` etc. continue
to exist on the class as thin pass-throughs.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol, Sequence

from backend.infrastructure.github_helpers import (
    _STATE_ORDER,
    _aggregate_status_check_rollup,
    _normalize_mergeable,
    _parse_pr_summary,
)
from backend.infrastructure.github_models import PullRequestContext, PullRequestSummary

_logger = logging.getLogger(__name__)


class _ClientProtocol(Protocol):
    """Duck-typed interface expected by the PR-side helpers.

    Implemented by :class:`GitHubCliClient`; the helpers only touch the
    methods needed to drive ``gh`` invocations.
    """

    repo_path: Path

    def _run_with_retry(
        self, command: Sequence[str], *, cwd: Path, check: bool = True
    ) -> object: ...

    def _write_body_file(self, temp_dir: str, filename: str, body: str) -> Path: ...


def create_draft_pr(
    client: _ClientProtocol,
    *,
    title: str,
    body: str,
    base_branch: str,
    cwd: Path,
) -> str:
    """Create a draft pull request from the current branch."""
    with tempfile.TemporaryDirectory(prefix="iar-pr-") as temp_dir:
        body_path = client._write_body_file(temp_dir, "pr.md", body)
        result = client._run_with_retry(
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


def get_pull_request_context(client: _ClientProtocol, branch: str) -> PullRequestContext | None:
    """Return PR context for an open PR on the given branch."""
    result = client._run_with_retry(
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
        cwd=client.repo_path,
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
    checks_state, checks_summary = _aggregate_status_check_rollup(raw_pr.get("statusCheckRollup"))
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


def comment_pr(client: _ClientProtocol, pr_number: int, body: str) -> None:
    """Post a Markdown comment to a Pull Request."""
    with tempfile.TemporaryDirectory(prefix="iar-pr-comment-") as temp_dir:
        comment_path = client._write_body_file(temp_dir, "comment.md", body)
        client._run_with_retry(
            [
                "gh",
                "pr",
                "comment",
                str(pr_number),
                "--body-file",
                str(comment_path),
            ],
            cwd=client.repo_path,
        )


def update_pull_request_body(client: _ClientProtocol, pr_number: int, body: str) -> None:
    """Replace the description body of a Pull Request."""
    with tempfile.TemporaryDirectory(prefix="iar-pr-body-") as temp_dir:
        body_path = client._write_body_file(temp_dir, "body.md", body)
        client._run_with_retry(
            [
                "gh",
                "pr",
                "edit",
                str(pr_number),
                "--body-file",
                str(body_path),
            ],
            cwd=client.repo_path,
        )


def merge_pull_request(client: _ClientProtocol, pr_number: int, *, method: str = "squash") -> None:
    """Merge a Pull Request using ``gh pr merge`` with the requested method.

    ``method`` only accepts ``"squash"`` for now. Squashing gives a single
    revert-friendly commit on the base branch and matches the merge queue
    PRD's hard requirement.

    Already-merged responses are treated as idempotent success so the
    merge queue can safely re-enter after a daemon crash without throwing.

    Args:
        pr_number: Target Pull Request number.
        method: Merge method; must be ``"squash"``.

    Raises:
        ValueError: When ``method`` is not ``"squash"``.
        RuntimeError: When ``gh pr merge`` exits non-zero with anything
            other than the idempotent-already-merged case.
    """
    if method != "squash":
        raise ValueError(f"merge_pull_request method must be 'squash'; got {method!r}.")
    try:
        client._run_with_retry(
            ["gh", "pr", "merge", str(pr_number), "--squash"],
            cwd=client.repo_path,
            check=False,
        )
    except subprocess.CalledProcessError as exc:
        combined_output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        if "Already merged" in combined_output or "already merged" in combined_output:
            _logger.info(
                "PR #%d is already merged; treating merge request as no-op.",
                pr_number,
            )
            return
        raise RuntimeError(
            f"gh pr merge failed for PR #{pr_number}: "
            f"{(exc.stderr or '').strip() or (exc.stdout or '').strip()}"
        ) from exc


def list_pr_comments(client: _ClientProtocol, pr_number: int) -> list[str]:
    """Return raw comment bodies for a PR."""
    result = client._run_with_retry(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--comments",
            "--json",
            "comments",
        ],
        cwd=client.repo_path,
        check=False,
    )
    if result.return_code != 0:
        return []
    raw_data = json.loads(result.stdout or "{}")
    comments = raw_data.get("comments", [])
    return [str(c.get("body", "")) for c in comments if c.get("body")]


def find_open_pr_by_head(client: _ClientProtocol, branch: str) -> str | None:
    """Return PR URL if an open PR exists for the branch."""
    result = client._run_with_retry(
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
        cwd=client.repo_path,
        check=False,
    )
    if result.return_code != 0:
        return None
    raw_prs = json.loads(result.stdout or "[]")
    if not raw_prs:
        return None
    return str(raw_prs[0].get("url", ""))


def find_merged_pr_by_head(client: _ClientProtocol, branch: str) -> str | None:
    """Return PR URL if a merged PR exists for the branch."""
    result = client._run_with_retry(
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
        cwd=client.repo_path,
        check=False,
    )
    if result.return_code != 0:
        return None
    raw_prs = json.loads(result.stdout or "[]")
    if not raw_prs:
        return None
    return str(raw_prs[0].get("url", ""))


def get_remote_base_sha(client: _ClientProtocol, remote: str, base_branch: str) -> str:
    """Return the SHA of the remote base branch."""
    result = client._run_with_retry(
        [
            "git",
            "rev-parse",
            f"{remote}/{base_branch}",
        ],
        cwd=client.repo_path,
        check=False,
    )
    if result.return_code != 0:
        return ""
    return result.stdout.strip()


def list_pull_requests_for_issue(
    client: _ClientProtocol, repo: str, issue_number: int
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
    result = client._run_with_retry(command, cwd=client.repo_path)
    raw_prs = json.loads(result.stdout or "[]")
    pulls = [_parse_pr_summary(raw_pr) for raw_pr in raw_prs]
    pulls.sort(key=lambda pull: (_STATE_ORDER.get(pull.state, 99), pull.number))
    return pulls


__all__ = [
    "comment_pr",
    "create_draft_pr",
    "find_merged_pr_by_head",
    "find_open_pr_by_head",
    "get_pull_request_context",
    "get_remote_base_sha",
    "list_pr_comments",
    "list_pull_requests_for_issue",
    "merge_pull_request",
    "update_pull_request_body",
]
