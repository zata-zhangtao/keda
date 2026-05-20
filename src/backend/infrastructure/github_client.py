"""GitHub CLI client implementation."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from backend.infrastructure.process_runner import SubprocessRunner


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
    review: str = "agent/review"
    failed: str = "agent/failed"
    blocked: str = "agent/blocked"
    codex: str = "agent/codex"
    claude: str = "agent/claude"


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

    def sync_labels(self, labels: LabelConfig) -> None:
        """Create or update standard labels."""
        label_specs = [
            ("agent/ready", "0E8A16", "Issue is ready for a local AI runner to claim."),
            ("agent/running", "FBCA04", "Issue is currently being executed by a local AI runner."),
            ("agent/review", "1D76DB", "AI runner opened work for human review."),
            ("agent/failed", "D73A4A", "AI runner failed and posted details."),
            ("agent/blocked", "000000", "AI runner needs human input."),
            ("agent/codex", "5319E7", "Use Codex for local runner execution."),
            ("agent/claude", "BFDADC", "Use Claude Code for local runner execution."),
            ("source/prd", "0052CC", "Issue has a canonical PRD tracked in the repository."),
            ("type/feature", "1D76DB", "User-facing feature or capability work."),
            ("type/refactor", "5319E7", "Internal refactor or structural improvement."),
            ("type/bug", "D73A4A", "Broken behavior or regression fix."),
            ("status/backlog", "BFDADC", "Tracked work that is not in progress yet."),
        ]
        configured_names = {
            "agent/ready": labels.ready,
            "agent/running": labels.running,
            "agent/review": labels.review,
            "agent/failed": labels.failed,
            "agent/blocked": labels.blocked,
            "agent/codex": labels.codex,
            "agent/claude": labels.claude,
        }
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
        command = ["gh", "issue", "edit", str(issue_number)]
        for label in add:
            command.extend(["--add-label", label])
        for label in remove:
            command.extend(["--remove-label", label])
        self._runner.run(command, cwd=self.repo_path)

    def comment_issue(self, issue_number: int, body: str) -> None:
        """Post a Markdown comment to an Issue."""
        with tempfile.TemporaryDirectory(prefix="iar-comment-") as temp_dir:
            comment_path = Path(temp_dir) / "comment.md"
            comment_path.write_text(body, encoding="utf-8")
            self._runner.run(
                ["gh", "issue", "comment", str(issue_number), "--body-file", str(comment_path)],
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
            command = ["gh", "issue", "create", "--title", title, "--body-file", str(body_path)]
            for label in labels:
                command.extend(["--label", label])
            result = self._runner.run(command, cwd=self.repo_path)
        return result.stdout.strip().splitlines()[-1]

    def create_draft_pr(self, *, title: str, body: str, base_branch: str, cwd: Path) -> str:
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
