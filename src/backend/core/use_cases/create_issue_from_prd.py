"""Create GitHub Issues from local PRD Markdown files."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient
from backend.core.shared.models.agent_runner import LabelConfig

_logger = logging.getLogger(__name__)

ISSUE_LINE_RE = re.compile(r"^- GitHub Issue:\s*\S+\s*$")


def extract_title(prd_text: str, fallback_title: str) -> str:
    """Extract a title from a PRD document."""

    for line in prd_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return re.sub(r"^PRD[:：]\s*", "", stripped[2:]).strip() or fallback_title
    return fallback_title


def extract_acceptance_items(prd_text: str) -> list[str]:
    """Extract acceptance checklist items from a PRD."""

    items: list[str] = []
    in_acceptance = False
    for line in prd_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_acceptance = bool(re.search(r"acceptance|验收", stripped, re.IGNORECASE))
            continue
        if in_acceptance and stripped.startswith("- ["):
            items.append(re.sub(r"^- \[[ xX]\]", "- [ ]", stripped))
    return items or [
        "- [ ] Review the canonical PRD acceptance checklist",
        "- [ ] Implement the linked task",
        "- [ ] Run the required verification",
    ]


def build_issue_body(
    *, relative_prd_path: Path, title: str, acceptance_items: list[str]
) -> str:
    """Build the Issue body from PRD metadata."""

    return "\n".join(
        [
            "## Summary",
            "",
            f"Tracked implementation task for `{title}`.",
            "",
            "## Canonical PRD",
            "",
            f"- PRD path: `{relative_prd_path.as_posix()}`",
            "",
            "## Acceptance Summary",
            "",
            *acceptance_items,
            "",
            "## Delivery Notes",
            "",
            "- Recommended branch: `task/<issue-number>-<slug>`",
            "- Worktree command: `just worktree --issue <issue-number>`",
            "- PR should include: `Closes #<issue-number>`",
            "",
        ]
    )


def create_issue_from_prd(
    *,
    repo_path: Path,
    prd_path: Path,
    issue_type: str,
    title_override: str | None = None,
    queue_ready: bool = True,
    issue_agent: str = "auto",
    labels_config: LabelConfig | None = None,
    force: bool = False,
    github_client: IGitHubClient,
) -> str:
    """Create a GitHub Issue from a PRD and write the URL back to the PRD.

    Args:
        repo_path: Target repository path.
        prd_path: Path to the PRD file (relative to repo_path or absolute).
        issue_type: Issue type (feature, refactor, bug).
        title_override: Optional title override.
        queue_ready: Whether to add the ready label.
        issue_agent: Agent routing label (auto, codex, claude, none).
        labels_config: Optional label configuration.
        force: Whether to overwrite an existing Issue link.
        github_client: Client for interacting with GitHub.

    Returns:
        URL of the created GitHub Issue.
    """

    absolute_prd_path = (
        (repo_path / prd_path).resolve() if not prd_path.is_absolute() else prd_path
    )
    relative_prd_path = absolute_prd_path.relative_to(repo_path.resolve())
    prd_text = absolute_prd_path.read_text(encoding="utf-8")
    if not force and any(ISSUE_LINE_RE.match(line) for line in prd_text.splitlines()):
        raise ValueError(
            "PRD already has a GitHub Issue link. Use --force to replace it."
        )

    fallback_title = absolute_prd_path.stem.split("-prd-", maxsplit=1)[-1].replace(
        "-", " "
    )
    title = (
        title_override
        or f"[{issue_type.title()}] {extract_title(prd_text, fallback_title)}"
    )
    effective_labels_config = labels_config or LabelConfig()
    labels = [f"type/{issue_type}", "status/backlog", "source/prd"]
    if queue_ready:
        labels.append(effective_labels_config.ready)
    if issue_agent == "codex":
        labels.append(effective_labels_config.codex)
    elif issue_agent == "claude":
        labels.append(effective_labels_config.claude)
    elif issue_agent not in {"auto", "none"}:
        raise ValueError("issue_agent must be one of: auto, codex, claude, none")
    body = build_issue_body(
        relative_prd_path=relative_prd_path,
        title=title,
        acceptance_items=extract_acceptance_items(prd_text),
    )
    issue_url = github_client.create_issue(title=title, body=body, labels=labels)
    link_line = f"- GitHub Issue: {issue_url}"

    updated_lines: list[str] = []
    link_written = False
    for line in prd_text.splitlines():
        if ISSUE_LINE_RE.match(line):
            if force:
                updated_lines.append(link_line)
                link_written = True
            continue
        updated_lines.append(line)
        if not link_written and line.startswith("# "):
            updated_lines.extend(["", link_line])
            link_written = True
    if not link_written:
        updated_lines.insert(0, link_line)

    absolute_prd_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    _logger.info("Created GitHub Issue: %s", issue_url)
    return issue_url
