"""Issue-side operations for the GitHub CLI client.

Module-level helper functions that drive ``gh issue ...`` and
``gh api repos/.../issues/comments/...`` invocations. They take a
client-like first argument (``_ClientProtocol``) so the main
:class:`backend.infrastructure.github_client.GitHubCliClient` can keep its
method-level public surface while delegating the actual ``gh`` command
construction here.

Backward compatibility: ``GitHubCliClient.list_ready_issues`` etc. continue
to exist on the class as thin pass-throughs.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Sequence

from backend.infrastructure.github_helpers import _extract_comment_id_from_url
from backend.infrastructure.github_models import IssueSummary

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)


class _ClientProtocol(Protocol):
    """Duck-typed interface expected by the issue-side helpers.

    Implemented by :class:`GitHubCliClient`; the helpers only touch the
    methods needed to drive ``gh`` invocations.
    """

    repo_path: Path

    def _run_with_retry(
        self, command: Sequence[str], *, cwd: Path, check: bool = True
    ) -> object: ...

    def _write_body_file(self, temp_dir: str, filename: str, body: str) -> Path: ...

    def _get_owner_repo(self) -> str: ...

    def _list_issue_label_names(self, issue_number: int) -> set[str]: ...


def _build_issue_summary(raw_issue: dict[str, object]) -> IssueSummary:
    """Map a ``gh issue ... --json`` row into an :class:`IssueSummary`."""
    return IssueSummary(
        number=int(raw_issue["number"]),
        title=str(raw_issue.get("title", "")),
        url=str(raw_issue.get("url", "")),
        body=str(raw_issue.get("body", "") or ""),
        labels=tuple(
            str(raw_label.get("name", ""))
            for raw_label in raw_issue.get("labels", [])
            if raw_label.get("name")
        ),
        state=str(raw_issue.get("state", "OPEN") or "OPEN"),
    )


def list_ready_issues(client: _ClientProtocol, ready_label: str, limit: int) -> list[IssueSummary]:
    """List open Issues with the ready label."""
    result = client._run_with_retry(
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
        cwd=client.repo_path,
    )
    raw_issues = json.loads(result.stdout or "[]")
    return [_build_issue_summary(raw_issue) for raw_issue in raw_issues]


def list_rework_prd_issues(
    client: _ClientProtocol, rework_prd_label: str, limit: int
) -> list[IssueSummary]:
    """List open Issues with the rework-prd label."""
    result = client._run_with_retry(
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
        cwd=client.repo_path,
    )
    raw_issues = json.loads(result.stdout or "[]")
    return [_build_issue_summary(raw_issue) for raw_issue in raw_issues]


def list_review_candidate_issues(
    client: _ClientProtocol, labels: Sequence[str], limit: int
) -> list[IssueSummary]:
    """List open Issues with any of the given labels."""
    seen_numbers: set[int] = set()
    candidates: list[IssueSummary] = []
    for label in labels:
        result = client._run_with_retry(
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
            cwd=client.repo_path,
        )
        raw_issues = json.loads(result.stdout or "[]")
        for raw_issue in raw_issues:
            number = int(raw_issue["number"])
            if number in seen_numbers:
                continue
            seen_numbers.add(number)
            candidates.append(_build_issue_summary(raw_issue))
    return candidates


def list_issues_by_label(
    client: _ClientProtocol, label: str | None, limit: int, state: str = "all"
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
    result = client._run_with_retry(command, cwd=client.repo_path)
    raw_issues = json.loads(result.stdout or "[]")
    return [_build_issue_summary(raw_issue) for raw_issue in raw_issues]


def get_issue(client: _ClientProtocol, issue_number: int) -> IssueSummary:
    """Return the Issue summary for the given issue number."""
    result = client._run_with_retry(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--json",
            "number,title,url,labels,body,state",
        ],
        cwd=client.repo_path,
        check=False,
    )
    if result.return_code != 0:
        raise RuntimeError(
            f"Failed to fetch Issue #{issue_number}: {result.stderr.strip() or result.stdout}"
        )
    raw_issue = json.loads(result.stdout or "{}")
    return _build_issue_summary(raw_issue)


def edit_issue_labels(
    client: _ClientProtocol,
    issue_number: int,
    *,
    add: Sequence[str] = (),
    remove: Sequence[str] = (),
) -> None:
    """Add and remove Issue labels."""
    current_labels = client._list_issue_label_names(issue_number)
    labels_to_add = [label for label in add if label not in current_labels]
    requested_add_labels = set(add)
    labels_to_remove = [
        label for label in remove if label in current_labels and label not in requested_add_labels
    ]
    if not labels_to_add and not labels_to_remove:
        return

    command = ["gh", "issue", "edit", str(issue_number)]
    for label in labels_to_add:
        command.extend(["--add-label", label])
    for label in labels_to_remove:
        command.extend(["--remove-label", label])
    client._run_with_retry(command, cwd=client.repo_path)


def list_issue_label_names(client: _ClientProtocol, issue_number: int) -> set[str]:
    """Return the set of label names currently on an Issue."""
    result = client._run_with_retry(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--json",
            "labels",
        ],
        cwd=client.repo_path,
    )
    raw_issue = json.loads(result.stdout or "{}")
    return {
        str(raw_label.get("name", ""))
        for raw_label in raw_issue.get("labels", [])
        if raw_label.get("name")
    }


def comment_issue(client: _ClientProtocol, issue_number: int, body: str) -> None:
    """Post a Markdown comment to an Issue."""
    with tempfile.TemporaryDirectory(prefix="iar-comment-") as temp_dir:
        comment_path = client._write_body_file(temp_dir, "comment.md", body)
        client._run_with_retry(
            [
                "gh",
                "issue",
                "comment",
                str(issue_number),
                "--body-file",
                str(comment_path),
            ],
            cwd=client.repo_path,
        )


def edit_issue_body(client: _ClientProtocol, issue_number: int, body: str) -> None:
    """Replace the body of an Issue."""
    with tempfile.TemporaryDirectory(prefix="iar-issue-body-") as temp_dir:
        body_path = Path(temp_dir) / "issue_body.md"
        body_path.write_text(body, encoding="utf-8")
        client._run_with_retry(
            [
                "gh",
                "issue",
                "edit",
                str(issue_number),
                "--body-file",
                str(body_path),
            ],
            cwd=client.repo_path,
        )


def create_issue(
    client: _ClientProtocol,
    *,
    title: str,
    body: str,
    labels: Sequence[str],
) -> str:
    """Create a GitHub Issue and return its URL."""
    with tempfile.TemporaryDirectory(prefix="iar-issue-") as temp_dir:
        body_path = client._write_body_file(temp_dir, "issue.md", body)
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
        result = client._run_with_retry(command, cwd=client.repo_path)
    return result.stdout.strip().splitlines()[-1]


def list_issue_comments(client: _ClientProtocol, issue_number: int) -> list[str]:
    """Return raw comment bodies for an Issue."""
    result = client._run_with_retry(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
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


def list_issue_comment_entries(client: _ClientProtocol, issue_number: int) -> list[tuple[int, str]]:
    """Return (comment_id, body) entries for an Issue.

    The numeric comment ID is parsed from the comment URL so callers can
    edit comments via the REST API. Comments without a usable URL are
    included with ``comment_id=0`` so ``list_issue_comments`` semantics are
    preserved and callers can still see the body.
    """
    result = client._run_with_retry(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
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
    entries: list[tuple[int, str]] = []
    for raw_comment in comments:
        url = str(raw_comment.get("url", ""))
        comment_id = _extract_comment_id_from_url(url) or 0
        body = str(raw_comment.get("body", ""))
        entries.append((comment_id, body))
    return entries


def edit_issue_comment(client: _ClientProtocol, comment_id: int, body: str) -> None:
    """Edit an existing Issue comment."""
    owner_repo = client._get_owner_repo()
    with tempfile.TemporaryDirectory(prefix="iar-comment-edit-") as temp_dir:
        body_path = client._write_body_file(temp_dir, "comment.md", body)
        client._run_with_retry(
            [
                "gh",
                "api",
                f"repos/{owner_repo}/issues/comments/{comment_id}",
                "-X",
                "PATCH",
                "-F",
                f"body@{body_path}",
            ],
            cwd=client.repo_path,
        )


__all__ = [
    "comment_issue",
    "create_issue",
    "edit_issue_body",
    "edit_issue_comment",
    "edit_issue_labels",
    "get_issue",
    "list_issue_comment_entries",
    "list_issue_comments",
    "list_issue_label_names",
    "list_issues_by_label",
    "list_ready_issues",
    "list_rework_prd_issues",
    "list_review_candidate_issues",
]
