"""Local Issue queue runner — single polling pass."""

from __future__ import annotations

import logging
import re
import shlex
import socket
from fnmatch import fnmatch
from collections.abc import Callable
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
)

_logger = logging.getLogger(__name__)


def format_command(template: str, *, issue_number: int) -> list[str]:
    """Format a configured command template for an Issue."""
    return shlex.split(template.format(issue_number=issue_number))


def choose_agent(issue: IssueSummary, config: AppConfig, override_agent: str) -> str:
    """Choose an AI agent for the Issue."""
    if override_agent != "auto":
        return override_agent
    for agent_name, label in config.labels.agent_labels.items():
        if label in issue.labels:
            return agent_name
    return (
        config.runner.default_agent
        if config.runner.default_agent != "auto"
        else "codex"
    )


def extract_prd_path(issue_body: str) -> str | None:
    """Extract a PRD path from an Issue body."""
    match = re.search(r"PRD path:\s*`([^`]+)`", issue_body)
    return match.group(1) if match else None


def build_prompt(issue: IssueSummary, worktree_path: Path) -> str:
    """Build the prompt sent to the local AI agent."""
    prd_path = extract_prd_path(issue.body)
    prd_line = (
        f"Also read the canonical PRD at `{prd_path}`."
        if prd_path
        else "If the Issue references a PRD, read it before editing."
    )
    return "\n".join(
        [
            f"Complete GitHub Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            f"Worktree: {worktree_path}",
            prd_line,
            "",
            "Issue body:",
            issue.body,
            "",
            "Execution rules:",
            "- Read AGENTS.md and follow repository instructions.",
            "- Only modify files inside the current worktree.",
            "- Do not merge main, delete branches, push, create PRs, or commit; the runner handles publishing.",
            "- Do not touch production systems or real business data.",
            "- Implement the requested task with focused tests and docs updates.",
            "- Finish with a concise summary, tests run, and remaining risk.",
        ]
    )


def create_or_reuse_worktree(
    repo_path: Path,
    issue: IssueSummary,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> Path:
    """Create or reuse a worktree for the Issue."""
    create_result = process_runner.run(
        format_command(config.worktree.create_command, issue_number=issue.number),
        cwd=repo_path,
        check=False,
    )
    if create_result.return_code != 0:
        process_runner.run(
            format_command(config.worktree.reuse_command, issue_number=issue.number),
            cwd=repo_path,
        )
    path_result = process_runner.run(
        format_command(config.worktree.path_command, issue_number=issue.number),
        cwd=repo_path,
    )
    return Path(path_result.stdout.strip()).resolve()


def _build_claude_command(prompt: str, worktree_path: Path) -> list[str]:  # noqa: ARG001
    return ["claude", "--permission-mode", "dontAsk", "-p", prompt]


def _build_kimi_command(prompt: str, worktree_path: Path) -> list[str]:  # noqa: ARG001
    return ["kimi", "--prompt", prompt]


def _build_codex_command(prompt: str, worktree_path: Path) -> list[str]:
    return [
        "codex",
        "--cd",
        str(worktree_path),
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "exec",
        prompt,
    ]


_AGENT_COMMAND_BUILDERS: dict[str, Callable[[str, Path], list[str]]] = {
    "claude": _build_claude_command,
    "kimi": _build_kimi_command,
}


def run_agent(
    agent_name: str,
    issue: IssueSummary,
    worktree_path: Path,
    process_runner: IProcessRunner,
) -> CommandResult:
    """Run Codex or Claude Code in non-interactive mode."""
    prompt = build_prompt(issue, worktree_path)
    builder = _AGENT_COMMAND_BUILDERS.get(agent_name)
    if builder is not None:
        command = builder(prompt, worktree_path)
    else:
        command = _build_codex_command(prompt, worktree_path)
    return process_runner.run(command, cwd=worktree_path, capture_output=False)


def run_verification(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> list[CommandResult]:
    """Run configured verification commands."""
    return [
        process_runner.run(shlex.split(command), cwd=worktree_path)
        for command in config.runner.verification_commands
    ]


def has_changes(worktree_path: Path, process_runner: IProcessRunner) -> bool:
    """Return whether the worktree has uncommitted changes."""
    result = process_runner.run(["git", "status", "--porcelain"], cwd=worktree_path)
    return bool(result.stdout.strip())


def list_changed_paths(
    worktree_path: Path, process_runner: IProcessRunner
) -> list[str]:
    """List changed paths in a worktree."""
    status_result = process_runner.run(
        ["git", "status", "--porcelain"], cwd=worktree_path
    )
    changed_paths: list[str] = []
    for status_line in status_result.stdout.splitlines():
        if not status_line:
            continue
        raw_path_text = status_line[3:]
        if " -> " in raw_path_text:
            changed_paths.extend(raw_path_text.split(" -> ", maxsplit=1))
        else:
            changed_paths.append(raw_path_text)
    return changed_paths


def validate_safe_changes(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> None:
    """Refuse to publish changes to configured forbidden paths."""
    blocked_paths: list[str] = []
    for changed_path_text in list_changed_paths(worktree_path, process_runner):
        changed_path_name = Path(changed_path_text).name
        for forbidden_pattern in config.safety.forbidden_path_patterns:
            if fnmatch(changed_path_text, forbidden_pattern) or fnmatch(
                changed_path_name,
                forbidden_pattern,
            ):
                blocked_paths.append(changed_path_text)
                break
    if blocked_paths:
        blocked_paths_text = ", ".join(sorted(set(blocked_paths)))
        raise RuntimeError(f"Refusing to publish forbidden paths: {blocked_paths_text}")


def publish_changes(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> tuple[str, str]:
    """Commit, push, and create a draft PR."""
    branch = process_runner.run(
        ["git", "branch", "--show-current"], cwd=worktree_path
    ).stdout.strip()
    validate_safe_changes(worktree_path, config, process_runner)
    process_runner.run(["git", "add", "-A"], cwd=worktree_path)
    process_runner.run(
        ["git", "commit", "-m", f"agent: complete issue #{issue.number}"],
        cwd=worktree_path,
    )
    process_runner.run(
        ["git", "push", "-u", config.git.remote, branch], cwd=worktree_path
    )
    pr_body = f"Closes #{issue.number}\n\nGenerated by issue-agent-runner.\n"
    pr_url = github_client.create_draft_pr(
        title=f"[Agent] {issue.title}",
        body=pr_body,
        base_branch=config.git.base_branch,
        cwd=worktree_path,
    )
    return branch, pr_url


def run_once(
    *,
    repo_path: Path,
    config: AppConfig,
    dry_run: bool,
    agent: str,
    max_issues: int,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> int:
    """Run one polling pass.

    Args:
        repo_path: Target repository path.
        config: Application configuration.
        dry_run: If True, only list ready issues without processing.
        agent: Agent override (auto, codex, claude).
        max_issues: Maximum issues to process.
        github_client: Client for interacting with GitHub.
        process_runner: Runner for executing subprocess commands.

    Returns:
        Exit code (0 on success, 1 if any issue failed).
    """
    issues = github_client.list_ready_issues(config.labels.ready, max_issues)
    if not issues:
        _logger.info("No open Issues found with label %s.", config.labels.ready)
        return 0

    exit_code = 0
    for issue in issues:
        selected_agent = choose_agent(issue, config, agent)
        if dry_run:
            _logger.info(
                "DRY RUN: would process Issue #%d with %s: %s",
                issue.number,
                selected_agent,
                issue.title,
            )
            continue
        try:
            github_client.edit_issue_labels(
                issue.number, add=[config.labels.running], remove=[config.labels.ready]
            )
            github_client.comment_issue(
                issue.number,
                f"## Agent Runner Claimed\n\n- Host: `{socket.gethostname()}`\n- Agent: `{selected_agent}`\n",
            )
            worktree_path = create_or_reuse_worktree(
                repo_path, issue, config, process_runner
            )
            run_agent(selected_agent, issue, worktree_path, process_runner)
            verification_results = run_verification(
                worktree_path, config, process_runner
            )
            if not has_changes(worktree_path, process_runner):
                raise RuntimeError("Agent completed but produced no git changes.")
            branch, pr_url = publish_changes(
                issue, worktree_path, config, github_client, process_runner
            )
            github_client.edit_issue_labels(
                issue.number, add=[config.labels.review], remove=[config.labels.running]
            )
            verification_lines = "\n".join(
                f"- `{' '.join(result.command)}`: exit {result.return_code}"
                for result in verification_results
            )
            github_client.comment_issue(
                issue.number,
                "\n".join(
                    [
                        "## Agent Runner Result",
                        "",
                        f"- Branch: `{branch}`",
                        f"- Draft PR: {pr_url}",
                        "",
                        "Verification:",
                        verification_lines,
                    ]
                ),
            )
            _logger.info("Completed Issue #%d: %s", issue.number, issue.title)
        except Exception as exc:  # noqa: BLE001 - report queue failures and continue.
            exit_code = 1
            github_client.edit_issue_labels(
                issue.number, add=[config.labels.failed], remove=[config.labels.running]
            )
            github_client.comment_issue(
                issue.number, f"## Agent Runner Failed\n\n```text\n{exc}\n```\n"
            )
            _logger.error("Failed Issue #%d: %s", issue.number, exc)
    return exit_code
