"""Pytest configuration for local imports."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
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


class FakeProcessRunner(IProcessRunner):
    """In-memory process runner for tests."""

    def __init__(
        self, responses: dict[tuple[str, ...], CommandResult] | None = None
    ) -> None:
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout: int | None = None,
    ) -> CommandResult:
        self.calls.append(list(command))
        key = tuple(command)
        if key in self.responses:
            result = self.responses[key]
            if check and result.return_code != 0:
                raise RuntimeError(f"Command failed: {command}")
            return result
        return CommandResult(
            command=tuple(command), return_code=0, stdout="", stderr=""
        )
