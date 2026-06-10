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
        self._pr_comments: dict[int, list[str]] = {}
        self._pr_contexts: dict[str, object | None] = {}
        self._open_prs: dict[str, str | None] = {}
        self._remote_base_sha: str = "remote-base-sha"
        self._issue_states: dict[int, str] = {}

    def sync_labels(self, labels: LabelConfig) -> None:
        self.calls.append({"method": "sync_labels", "labels": labels})

    def list_ready_issues(self, ready_label: str, limit: int) -> list[IssueSummary]:
        self.calls.append(
            {"method": "list_ready_issues", "ready_label": ready_label, "limit": limit}
        )
        return []

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

    def comment_issue(self, issue_number: int, body: str) -> None:
        self.calls.append(
            {"method": "comment_issue", "issue_number": issue_number, "body": body}
        )
        self._issue_comments.setdefault(issue_number, []).append(body)

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
        return IssueSummary(
            number=issue_number,
            title=f"Issue #{issue_number}",
            url=f"https://github.com/example/repo/issues/{issue_number}",
            body="",
            labels=(),
            state=self._issue_states.get(issue_number, "OPEN"),
        )


class FakeContentGenerator(IContentGenerator):
    """In-memory content generator for tests."""

    def __init__(self, response: str = "") -> None:
        self.response = response
        self.calls: list[list[str]] = []

    def generate(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        timeout: int | None = None,
    ) -> CommandResult:
        self.calls.append([agent_name, prompt[:50]])
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

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout: int | None = None,
        capture_output: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        self.calls.append(list(command))
        self.input_texts.append(input_text)
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
