"""Pytest configuration for local imports."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import (
    CommandResult,
    IssueSummary,
    LabelConfig,
    PullRequestSummary,
)


def _ensure_project_root_on_path() -> None:
    """Ensure project root is on sys.path for local package imports."""
    project_root_path = Path(__file__).resolve().parents[1]
    if str(project_root_path) not in sys.path:
        sys.path.insert(0, str(project_root_path))


_ensure_project_root_on_path()


class FakeGitHubClient(IGitHubClient):
    """In-memory GitHub client for tests."""

    def __init__(
        self, issue_url: str = "https://github.com/example/repo/issues/42"
    ) -> None:
        self._issue_url = issue_url
        self.calls: list[dict] = []
        self._issue_comments: dict[int, list[str]] = {}
        self._issue_bodies: dict[int, str] = {}
        self._pr_comments: dict[int, list[str]] = {}
        self._pr_contexts: dict[str, object | None] = {}
        self._open_prs: dict[str, str | None] = {}
        self._merged_prs: dict[str, str | None] = {}
        self._remote_base_sha: str = "remote-base-sha"
        self._issue_states: dict[int, str] = {}
        self._issue_title: str | None = None
        self._issue_labels: dict[int, tuple[str, ...]] = {}
        self._rework_prd_issues: list[IssueSummary] = []
        self._prs_by_repo_issue: dict[tuple[str, int], list[PullRequestSummary]] = {}
        self._list_issues_by_label_result: list[IssueSummary] = []

    def sync_labels(self, labels: LabelConfig) -> None:
        self.calls.append({"method": "sync_labels", "labels": labels})

    def list_ready_issues(self, ready_label: str, limit: int) -> list[IssueSummary]:
        self.calls.append(
            {"method": "list_ready_issues", "ready_label": ready_label, "limit": limit}
        )
        return []

    def list_rework_prd_issues(
        self, rework_prd_label: str, limit: int
    ) -> list[IssueSummary]:
        self.calls.append(
            {
                "method": "list_rework_prd_issues",
                "rework_prd_label": rework_prd_label,
                "limit": limit,
            }
        )
        return self._rework_prd_issues[:limit]

    def set_rework_prd_issues(self, issues: list[IssueSummary]) -> None:
        self._rework_prd_issues = issues

    def edit_issue_labels(
        self, issue_number: int, *, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        self.calls.append(
            {
                "method": "edit_issue_labels",
                "issue_number": issue_number,
                "add": list(add),
                "remove": list(remove),
            }
        )
        current = set(self._issue_labels.get(issue_number, ()))
        current.update(add)
        current.difference_update(remove)
        self._issue_labels[issue_number] = tuple(current)

    def comment_issue(self, issue_number: int, body: str) -> None:
        self.calls.append(
            {"method": "comment_issue", "issue_number": issue_number, "body": body}
        )
        self._issue_comments.setdefault(issue_number, []).append(body)

    def edit_issue_body(self, issue_number: int, body: str) -> None:
        self.calls.append(
            {"method": "edit_issue_body", "issue_number": issue_number, "body": body}
        )
        self._issue_bodies[issue_number] = body

    def get_issue_body(self, issue_number: int) -> str | None:
        return self._issue_bodies.get(issue_number)

    def comment_pr(self, pr_number: int, body: str) -> None:
        self.calls.append(
            {"method": "comment_pr", "pr_number": pr_number, "body": body}
        )
        self._pr_comments.setdefault(pr_number, []).append(body)

    def update_pull_request_body(self, pr_number: int, body: str) -> None:
        self.calls.append(
            {
                "method": "update_pull_request_body",
                "pr_number": pr_number,
                "body": body,
            }
        )

    def create_issue(self, *, title: str, body: str, labels: Sequence[str]) -> str:
        self.calls.append(
            {
                "method": "create_issue",
                "title": title,
                "body": body,
                "labels": list(labels),
            }
        )
        return self._issue_url

    def create_draft_pr(
        self, *, title: str, body: str, base_branch: str, cwd: Path
    ) -> str:
        self.calls.append(
            {
                "method": "create_draft_pr",
                "title": title,
                "body": body,
                "base_branch": base_branch,
            }
        )
        return "https://github.com/example/repo/pull/1"

    def list_review_candidate_issues(
        self, labels: Sequence[str], limit: int
    ) -> list[IssueSummary]:
        self.calls.append(
            {
                "method": "list_review_candidate_issues",
                "labels": list(labels),
                "limit": limit,
            }
        )
        return []

    def get_pull_request_context(self, branch: str) -> object | None:
        self.calls.append({"method": "get_pull_request_context", "branch": branch})
        return self._pr_contexts.get(branch)

    def list_issue_comments(self, issue_number: int) -> list[str]:
        self.calls.append(
            {"method": "list_issue_comments", "issue_number": issue_number}
        )
        return list(self._issue_comments.get(issue_number, []))

    def list_pr_comments(self, pr_number: int) -> list[str]:
        self.calls.append({"method": "list_pr_comments", "pr_number": pr_number})
        return list(self._pr_comments.get(pr_number, []))

    def find_open_pr_by_head(self, branch: str) -> str | None:
        self.calls.append({"method": "find_open_pr_by_head", "branch": branch})
        return self._open_prs.get(branch)

    def find_merged_pr_by_head(self, branch: str) -> str | None:
        self.calls.append({"method": "find_merged_pr_by_head", "branch": branch})
        return self._merged_prs.get(branch)

    def get_remote_base_sha(self, remote: str, base_branch: str) -> str:
        self.calls.append(
            {
                "method": "get_remote_base_sha",
                "remote": remote,
                "base_branch": base_branch,
            }
        )
        return self._remote_base_sha

    def get_issue(self, issue_number: int) -> IssueSummary:
        self.calls.append({"method": "get_issue", "issue_number": issue_number})
        title = (
            self._issue_title
            if self._issue_title is not None
            else f"Issue #{issue_number}"
        )
        return IssueSummary(
            number=issue_number,
            title=title,
            url=f"https://github.com/example/repo/issues/{issue_number}",
            body="",
            labels=self._issue_labels.get(issue_number, ()),
            state=self._issue_states.get(issue_number, "OPEN"),
        )

    def list_issues_by_label(
        self, label: str, limit: int, state: str = "all"
    ) -> list[IssueSummary]:
        self.calls.append(
            {
                "method": "list_issues_by_label",
                "label": label,
                "limit": limit,
                "state": state,
            }
        )
        return list(self._list_issues_by_label_result[:limit])

    def set_list_issues_by_label_result(self, issues: list[IssueSummary]) -> None:
        self._list_issues_by_label_result = list(issues)

    def set_prs_for_repo_issue(
        self, repo: str, issue_number: int, pulls: list[PullRequestSummary]
    ) -> None:
        self._prs_by_repo_issue[(repo, issue_number)] = list(pulls)

    def list_pull_requests_for_issue(
        self, repo: str, issue_number: int
    ) -> list[PullRequestSummary]:
        self.calls.append(
            {
                "method": "list_pull_requests_for_issue",
                "repo": repo,
                "issue_number": issue_number,
            }
        )
        return list(self._prs_by_repo_issue.get((repo, issue_number), []))


class FakeContentGenerator(IContentGenerator):
    """In-memory content generator for tests."""

    def __init__(self, response: str = "") -> None:
        self.response = response
        self.calls: list[list[str]] = []
        self.prompts: list[str] = []

    def generate(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        timeout: int | None = None,
    ) -> CommandResult:
        self.calls.append([agent_name, prompt[:50]])
        self.prompts.append(prompt)
        return CommandResult(
            command=("generate", agent_name),
            return_code=0,
            stdout=self.response,
            stderr="",
        )


class FakeProcessRunner(IProcessRunner):
    """In-memory process runner for tests."""

    def __init__(
        self, responses: dict[tuple[str, ...], CommandResult] | None = None
    ) -> None:
        self.responses = responses or {}
        self.calls: list[list[str]] = []
        self.input_texts: list[str | None] = []
        self.labels: list[str | None] = []

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout: int | None = None,
        capture_output: bool = True,
        input_text: str | None = None,
        label: str | None = None,
    ) -> CommandResult:
        self.calls.append(list(command))
        self.input_texts.append(input_text)
        self.labels.append(label)
        key = tuple(command)
        if key in self.responses:
            result = self.responses[key]
            if check and result.return_code != 0:
                raise RuntimeError(f"Command failed: {command}")
            if not capture_output:
                return CommandResult(
                    command=result.command,
                    return_code=result.return_code,
                    stdout="",
                    stderr="",
                )
            return result
        return CommandResult(
            command=tuple(command), return_code=0, stdout="", stderr=""
        )
