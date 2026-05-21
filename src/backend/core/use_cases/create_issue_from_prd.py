"""Create GitHub Issues from local PRD Markdown files."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import LabelConfig

_logger = logging.getLogger(__name__)

ISSUE_LINE_RE = re.compile(r"^- GitHub Issue:\s*\S+\s*$")
ISSUE_NUMBER_RE = re.compile(r"/issues/(?P<issue_number>\d+)(?:\D*$|$)")


@dataclass(frozen=True)
class IssueFromPrdRequest:
    """Input values for creating a GitHub Issue from a PRD."""

    repo_path: Path
    prd_path: Path
    issue_type: str
    title_override: str | None = None
    queue_ready: bool = False
    issue_agent: str = "auto"
    labels_config: LabelConfig | None = None
    force: bool = False
    publish_prd: bool = False
    git_remote: str = "origin"
    git_base_branch: str = "main"


@dataclass(frozen=True)
class PrdPublishContext:
    """Git publishing values for one target PRD file."""

    repo_path: Path
    relative_prd_path: Path
    git_remote: str
    current_branch: str


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


def resolve_prd_paths(repo_path: Path, prd_path: Path) -> tuple[Path, Path]:
    """Resolve absolute and repository-relative PRD paths.

    Args:
        repo_path: Target repository path.
        prd_path: Path to the PRD file.

    Returns:
        Absolute PRD path and repository-relative PRD path.
    """

    absolute_prd_path = (
        (repo_path / prd_path).resolve() if not prd_path.is_absolute() else prd_path
    )
    relative_prd_path = absolute_prd_path.relative_to(repo_path.resolve())
    return absolute_prd_path, relative_prd_path


def build_issue_labels(
    request: IssueFromPrdRequest, effective_labels_config: LabelConfig
) -> list[str]:
    """Build labels for the initial GitHub Issue creation.

    Args:
        request: Issue creation request.
        effective_labels_config: Label names to apply.

    Returns:
        Initial labels for the Issue.
    """

    labels = [f"type/{request.issue_type}", "status/backlog", "source/prd"]
    if request.queue_ready and not request.publish_prd:
        labels.append(effective_labels_config.ready)
    if request.issue_agent in effective_labels_config.agent_labels:
        labels.append(effective_labels_config.agent_labels[request.issue_agent])
    elif request.issue_agent not in {"auto", "none"}:
        allowed = ", ".join(
            [*effective_labels_config.agent_labels.keys(), "auto", "none"]
        )
        raise ValueError(f"issue_agent must be one of: {allowed}")
    return labels


def parse_issue_number(issue_url: str) -> int:
    """Parse the GitHub Issue number from an Issue URL.

    Args:
        issue_url: GitHub Issue URL returned by the client.

    Returns:
        Parsed Issue number.

    Raises:
        ValueError: If the URL does not contain an Issue number.
    """

    issue_number_match = ISSUE_NUMBER_RE.search(issue_url)
    if issue_number_match is None:
        raise ValueError(f"Could not parse GitHub Issue number from URL: {issue_url}")
    return int(issue_number_match.group("issue_number"))


def write_issue_link(
    *, prd_text: str, absolute_prd_path: Path, issue_url: str, force: bool
) -> None:
    """Write the GitHub Issue URL back into the PRD.

    Args:
        prd_text: Original PRD text.
        absolute_prd_path: PRD file path to update.
        issue_url: Created GitHub Issue URL.
        force: Whether to replace an existing Issue URL.
    """

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


def current_git_branch(repo_path: Path, process_runner: IProcessRunner) -> str:
    """Return the current Git branch name.

    Args:
        repo_path: Target repository path.
        process_runner: Runner for executing Git commands.

    Returns:
        Current branch name.

    Raises:
        RuntimeError: If the repository is in detached HEAD state.
    """

    branch_result = process_runner.run(
        ["git", "branch", "--show-current"], cwd=repo_path
    )
    current_branch = branch_result.stdout.strip()
    if not current_branch:
        raise RuntimeError("Cannot publish a PRD from a detached HEAD checkout.")
    return current_branch


def validate_ready_publish_branch(
    *, current_branch: str, git_base_branch: str, queue_ready: bool
) -> None:
    """Validate that ready PRDs are published from the runner base branch.

    Args:
        current_branch: Current Git branch name.
        git_base_branch: Configured runner base branch.
        queue_ready: Whether the user asked to add the ready label.

    Raises:
        RuntimeError: If a ready PRD would be published from the wrong branch.
    """

    if queue_ready and current_branch != git_base_branch:
        raise RuntimeError(
            "Cannot publish a ready PRD from branch "
            f"'{current_branch}'. Switch to base branch '{git_base_branch}' "
            "or use --no-ready."
        )


def validate_staged_changes_are_prd_only(
    repo_path: Path, relative_prd_path: Path, process_runner: IProcessRunner
) -> None:
    """Refuse publishing when non-target files are already staged.

    Args:
        repo_path: Target repository path.
        relative_prd_path: PRD path relative to repo_path.
        process_runner: Runner for executing Git commands.

    Raises:
        RuntimeError: If any staged path is not the target PRD file.
    """

    staged_result = process_runner.run(
        ["git", "diff", "--cached", "--name-only", "--"], cwd=repo_path
    )
    target_prd_path_text = relative_prd_path.as_posix()
    staged_path_texts = [
        staged_line.strip()
        for staged_line in staged_result.stdout.splitlines()
        if staged_line.strip()
    ]
    non_target_staged_paths = [
        staged_path_text
        for staged_path_text in staged_path_texts
        if staged_path_text != target_prd_path_text
    ]
    if non_target_staged_paths:
        staged_paths_text = ", ".join(sorted(non_target_staged_paths))
        raise RuntimeError(
            "Refusing to publish PRD because Git index contains staged changes "
            f"outside target PRD: {staged_paths_text}"
        )


def build_prd_commit_message(relative_prd_path: Path) -> str:
    """Build the PRD publish commit message.

    Args:
        relative_prd_path: PRD path relative to the repository.

    Returns:
        Commit message for publishing the PRD.
    """

    prd_slug = relative_prd_path.stem.split("-prd-", maxsplit=1)[-1]
    return f"docs(prd): publish {prd_slug}"


def publish_prd_file(
    publish_context: PrdPublishContext, process_runner: IProcessRunner
) -> None:
    """Stage, commit, and push only the target PRD file.

    Args:
        publish_context: Git publishing values.
        process_runner: Runner for executing Git commands.
    """

    relative_prd_path_text = publish_context.relative_prd_path.as_posix()
    process_runner.run(
        ["git", "add", "--", relative_prd_path_text], cwd=publish_context.repo_path
    )
    process_runner.run(
        [
            "git",
            "commit",
            "-m",
            build_prd_commit_message(publish_context.relative_prd_path),
            "--",
            relative_prd_path_text,
        ],
        cwd=publish_context.repo_path,
    )
    process_runner.run(
        ["git", "push", publish_context.git_remote, publish_context.current_branch],
        cwd=publish_context.repo_path,
    )


def create_issue_from_prd(
    *,
    request: IssueFromPrdRequest,
    github_client: IGitHubClient,
    process_runner: IProcessRunner | None = None,
) -> str:
    """Create a GitHub Issue from a PRD and write the URL back to the PRD.

    Args:
        request: Issue creation request.
        github_client: Client for interacting with GitHub.
        process_runner: Optional runner for PRD publishing Git commands.

    Returns:
        URL of the created GitHub Issue.

    Raises:
        ValueError: If the PRD already has a GitHub Issue link or inputs are invalid.
        RuntimeError: If PRD publishing cannot be completed.
    """

    absolute_prd_path, relative_prd_path = resolve_prd_paths(
        request.repo_path, request.prd_path
    )
    prd_text = absolute_prd_path.read_text(encoding="utf-8")
    if not request.force and any(
        ISSUE_LINE_RE.match(line) for line in prd_text.splitlines()
    ):
        raise ValueError(
            "PRD already has a GitHub Issue link. Use --force to replace it."
        )

    fallback_title = absolute_prd_path.stem.split("-prd-", maxsplit=1)[-1].replace(
        "-", " "
    )
    title = (
        request.title_override
        or f"[{request.issue_type.title()}] {extract_title(prd_text, fallback_title)}"
    )
    effective_labels_config = request.labels_config or LabelConfig()
    labels = build_issue_labels(request, effective_labels_config)

    publish_context: PrdPublishContext | None = None
    if request.publish_prd:
        if process_runner is None:
            raise ValueError("process_runner is required when publish_prd=True.")
        current_branch = current_git_branch(request.repo_path, process_runner)
        validate_ready_publish_branch(
            current_branch=current_branch,
            git_base_branch=request.git_base_branch,
            queue_ready=request.queue_ready,
        )
        validate_staged_changes_are_prd_only(
            request.repo_path, relative_prd_path, process_runner
        )
        publish_context = PrdPublishContext(
            repo_path=request.repo_path,
            relative_prd_path=relative_prd_path,
            git_remote=request.git_remote,
            current_branch=current_branch,
        )

    body = build_issue_body(
        relative_prd_path=relative_prd_path,
        title=title,
        acceptance_items=extract_acceptance_items(prd_text),
    )
    issue_url = github_client.create_issue(title=title, body=body, labels=labels)
    write_issue_link(
        prd_text=prd_text,
        absolute_prd_path=absolute_prd_path,
        issue_url=issue_url,
        force=request.force,
    )
    if publish_context is not None:
        publish_prd_file(publish_context, process_runner)
        if request.queue_ready:
            github_client.edit_issue_labels(
                parse_issue_number(issue_url), add=[effective_labels_config.ready]
            )
    _logger.info("Created GitHub Issue: %s", issue_url)
    return issue_url
