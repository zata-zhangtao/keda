"""Agent Runner abstract interfaces (ports)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence

from backend.core.shared.models.agent_runner import (
    CommandResult,
    IssueSummary,
    LabelConfig,
    PullRequestContext,
)


class IProcessRunner(ABC):
    """Run external commands."""

    @abstractmethod
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout: int | None = None,
        capture_output: bool = True,
    ) -> CommandResult:
        """Run a command and capture its result.

        Args:
            command: Command and arguments to execute.
            cwd: Working directory for the command.
            check: Whether to raise on non-zero exit code.
            timeout: Optional timeout in seconds.
            capture_output: Whether to capture stdout/stderr. When False,
                output streams directly to the terminal and returned
                CommandResult contains empty strings.

        Returns:
            CommandResult with captured output.
        """
        ...


class IGitHubClient(ABC):
    """Interact with GitHub."""

    @abstractmethod
    def sync_labels(self, labels: LabelConfig) -> None:
        """Create or update standard labels."""
        ...

    @abstractmethod
    def list_ready_issues(self, ready_label: str, limit: int) -> list[IssueSummary]:
        """List open Issues with the ready label."""
        ...

    @abstractmethod
    def edit_issue_labels(
        self,
        issue_number: int,
        *,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
    ) -> None:
        """Add and remove Issue labels."""
        ...

    @abstractmethod
    def comment_issue(self, issue_number: int, body: str) -> None:
        """Post a Markdown comment to an Issue."""
        ...

    @abstractmethod
    def create_issue(
        self,
        *,
        title: str,
        body: str,
        labels: Sequence[str],
    ) -> str:
        """Create a GitHub Issue and return its URL."""
        ...

    @abstractmethod
    def create_draft_pr(
        self,
        *,
        title: str,
        body: str,
        base_branch: str,
        cwd: Path,
    ) -> str:
        """Create a draft pull request from the current branch."""
        ...

    @abstractmethod
    def list_review_candidate_issues(
        self, labels: Sequence[str], limit: int
    ) -> list[IssueSummary]:
        """List open Issues with any of the given labels."""
        ...

    @abstractmethod
    def get_pull_request_context(self, branch: str) -> PullRequestContext | None:
        """Return PR context for an open PR on the given branch."""
        ...

    @abstractmethod
    def list_issue_comments(self, issue_number: int) -> list[str]:
        """Return raw comment bodies for an Issue."""
        ...

    @abstractmethod
    def list_pr_comments(self, pr_number: int) -> list[str]:
        """Return raw comment bodies for a PR."""
        ...

    @abstractmethod
    def find_open_pr_by_head(self, branch: str) -> str | None:
        """Return PR URL if an open PR exists for the branch."""
        ...

    @abstractmethod
    def get_remote_base_sha(self, remote: str, base_branch: str) -> str:
        """Return the SHA of the remote base branch."""
        ...
