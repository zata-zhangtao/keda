"""CLI argument parser construction.

This module builds the argparse parser used by both the direct
``backend.api.cli`` entrypoint and the Typer front-end in
``backend.api.cli_typer``.
"""

from __future__ import annotations

import argparse


def add_common_options(parser: argparse.ArgumentParser) -> None:
    """Allow global options before or after the effective subcommand."""
    parser.add_argument(
        "--repo", default=argparse.SUPPRESS, help="Target repository path."
    )
    parser.add_argument(
        "--repo-id", default=argparse.SUPPRESS, help="Target configured repository ID."
    )
    parser.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="Deprecated: config is loaded from config.toml and env vars.",
    )


def add_all_repositories_option(parser: argparse.ArgumentParser) -> None:
    """Allow explicit multi-repository selection for configured repositories."""
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_repositories",
        help="Process all enabled configured repositories.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(prog="iar")
    parser.add_argument("--repo", default=None, help="Target repository path.")
    parser.add_argument(
        "--repo-id", default=None, help="Target configured repository ID."
    )
    parser.add_argument(
        "--config",
        help="Deprecated: config is loaded from config.toml and env vars.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="Create repository-local .iar.toml config."
    )
    init_parser.add_argument("--dry-run", action="store_true")
    init_parser.add_argument("--force", action="store_true")
    init_parser.add_argument("--id", dest="repository_id")
    init_parser.add_argument("--display-name")
    init_parser.add_argument("--remote")
    init_parser.add_argument("--base-branch")
    init_parser.add_argument(
        "--copy-skills",
        dest="copy_skills",
        choices=("true", "false"),
        default="true",
        help="Copy bundled skills (prd, code-reviewer) into .claude/skills/.",
    )
    init_parser.add_argument(
        "--skip-skills",
        action="store_true",
        help="Skip bundled skill copy (equivalent to --copy-skills=false).",
    )

    labels_parser = subparsers.add_parser("labels", help="Manage GitHub labels.")
    labels_subparsers = labels_parser.add_subparsers(
        dest="labels_command", required=True
    )
    labels_sync_parser = labels_subparsers.add_parser(
        "sync", help="Sync standard labels to the repository."
    )
    add_common_options(labels_sync_parser)
    add_all_repositories_option(labels_sync_parser)

    issue_parser = subparsers.add_parser(
        "issue", help="Create and manage GitHub Issues."
    )
    issue_subparsers = issue_parser.add_subparsers(dest="issue_command", required=True)
    issue_create_parser = issue_subparsers.add_parser(
        "create", help="Create a GitHub Issue from a PRD file."
    )
    issue_create_parser.set_defaults(command="issue create")
    issue_create_parser.add_argument("prd_path")
    issue_create_parser.add_argument(
        "--type", choices=("feature", "refactor", "bug"), default="feature"
    )
    issue_create_parser.add_argument("--title")
    issue_create_parser.add_argument(
        "--ready",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Add the ready label so a runner can pick the Issue up.",
    )
    issue_create_parser.add_argument(
        "--agent",
        choices=("auto", "codex", "claude", "kimi", "none"),
        default="auto",
        help="Optional agent routing label to add to the Issue.",
    )
    issue_create_parser.add_argument(
        "--publish-prd",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Commit and push only the target PRD before adding the ready label "
            "(default: on; pass --no-publish-prd to defer publishing to the "
            "interactive prompt)."
        ),
    )
    issue_create_parser.add_argument("--force", action="store_true")
    issue_create_parser.add_argument(
        "--depends-on",
        action="append",
        type=int,
        default=[],
        help="Upstream Issue number this Issue depends on (repeatable).",
    )
    issue_create_parser.add_argument(
        "--depends-on-group",
        action="append",
        type=str,
        default=[],
        help="Upstream group label this Issue depends on (repeatable).",
    )
    add_common_options(issue_create_parser)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument(
        "--agent", choices=("auto", "codex", "claude", "kimi"), default="auto"
    )
    run_parser.add_argument("--max-issues", type=int)
    add_common_options(run_parser)
    add_all_repositories_option(run_parser)

    daemon_parser = subparsers.add_parser("daemon")
    daemon_parser.add_argument("--interval", type=int, default=None)
    daemon_parser.add_argument(
        "--agent", choices=("auto", "codex", "claude", "kimi"), default="auto"
    )
    daemon_parser.add_argument("--max-issues", type=int)
    add_common_options(daemon_parser)
    add_all_repositories_option(daemon_parser)

    review_parser = subparsers.add_parser("review")
    review_parser.add_argument("--dry-run", action="store_true")
    review_parser.add_argument(
        "--agent", choices=("auto", "codex", "claude", "kimi"), default="auto"
    )
    review_parser.add_argument("--max-issues", type=int)
    add_common_options(review_parser)
    add_all_repositories_option(review_parser)

    review_daemon_parser = subparsers.add_parser("review-daemon")
    review_daemon_parser.add_argument("--interval", type=int, default=None)
    review_daemon_parser.add_argument(
        "--agent", choices=("auto", "codex", "claude", "kimi"), default="auto"
    )
    review_daemon_parser.add_argument("--max-issues", type=int)
    add_common_options(review_daemon_parser)
    add_all_repositories_option(review_daemon_parser)

    recover_parser = subparsers.add_parser(
        "recover",
        help="Resume a failed publish operation for an Issue.",
    )
    recover_parser.add_argument(
        "--issue",
        type=int,
        required=True,
        help="Issue number to recover publish for.",
    )
    recover_parser.add_argument(
        "--branch",
        default=None,
        help="Explicitly confirm the current branch name.",
    )
    add_common_options(recover_parser)

    blocked_continue_parser = subparsers.add_parser(
        "blocked-continue",
        help="Resume a blocked Issue after resolving forbidden paths.",
    )
    blocked_continue_parser.add_argument(
        "--issue",
        type=int,
        required=True,
        help="Issue number to continue.",
    )
    blocked_continue_parser.add_argument(
        "--agent",
        choices=("auto", "codex", "claude", "kimi"),
        default="auto",
        help="Agent runner to use.",
    )
    add_common_options(blocked_continue_parser)

    ask_parser = subparsers.add_parser(
        "ask", help="Ask the agent runner to decide the next safe action."
    )
    ask_parser.add_argument("prompt", help="Natural language request.")
    ask_parser.add_argument(
        "--agent",
        choices=("auto", "codex", "claude", "kimi"),
        default="auto",
        help="Planner agent to use.",
    )
    ask_parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Only generate plan without executing.",
    )
    ask_parser.add_argument(
        "--execute",
        action="store_true",
        help="Allow execution after confirmation.",
    )
    ask_parser.add_argument(
        "--yes",
        action="store_true",
        help="Auto-confirm non-interactive execution.",
    )
    ask_parser.add_argument(
        "--output",
        default=None,
        help="Output directory for decision audit.",
    )
    add_common_options(ask_parser)

    deliberate_parser = subparsers.add_parser(
        "deliberate", help="Run a multi-agent deliberation session."
    )
    deliberate_parser.add_argument(
        "prompt", help="The requirement or question to deliberate."
    )
    deliberate_parser.add_argument(
        "--agents",
        default="architect,skeptic,implementer",
        help="Comma-separated participant profile IDs.",
    )
    deliberate_parser.add_argument(
        "--rounds", type=int, default=None, help="Number of discussion rounds."
    )
    deliberate_parser.add_argument(
        "--synthesizer", default=None, help="Agent to run synthesis."
    )
    deliberate_parser.add_argument(
        "--output",
        default=None,
        help="Output directory for deliberation files.",
    )
    deliberate_parser.add_argument(
        "--session-id", default=None, help="Optional session ID for reproducibility."
    )
    add_common_options(deliberate_parser)

    worktree_parser = subparsers.add_parser(
        "worktree",
        help="Manage iAR-owned Git worktrees for the current repository.",
    )
    worktree_subparsers = worktree_parser.add_subparsers(
        dest="worktree_command", required=True
    )
    worktree_create_parser = worktree_subparsers.add_parser(
        "create", help="Create a worktree at .iar-worktrees/<branch>."
    )
    worktree_create_parser.add_argument(
        "--branch", required=True, help="Branch name to create."
    )
    worktree_create_parser.add_argument(
        "--base-branch", required=True, help="Existing branch to fork from."
    )
    worktree_path_parser = worktree_subparsers.add_parser(
        "path", help="Print the absolute worktree path for a branch."
    )
    worktree_path_parser.add_argument(
        "--branch", required=True, help="Branch name to resolve."
    )
    worktree_remove_parser = worktree_subparsers.add_parser(
        "remove", help="Remove a worktree and prune Git metadata."
    )
    worktree_remove_parser.add_argument(
        "--branch", required=True, help="Branch name whose worktree to remove."
    )
    worktree_cleanup_parser = worktree_subparsers.add_parser(
        "cleanup",
        help="Delete stale local issue branches whose Issue is closed.",
    )
    worktree_cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview cleanup without deleting anything.",
    )
    worktree_cleanup_parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete eligible branches and worktrees.",
    )
    worktree_cleanup_parser.add_argument(
        "--force",
        action="store_true",
        help="Also delete dirty or unmerged eligible branches.",
    )

    registry_parser = subparsers.add_parser(
        "registry", help="Manage the repository registry in config.toml."
    )
    registry_subparsers = registry_parser.add_subparsers(
        dest="registry_command", required=True
    )
    registry_scan_parser = registry_subparsers.add_parser(
        "scan", help="Discover IAR-initialized git repositories under a path."
    )
    registry_scan_parser.add_argument(
        "scan_root",
        nargs="?",
        default=".",
        help="Directory to scan (default: current directory).",
    )
    registry_sync_parser = registry_subparsers.add_parser(
        "sync",
        help="Discover and register all IAR repositories under a path.",
    )
    registry_sync_parser.add_argument(
        "scan_root",
        nargs="?",
        default=".",
        help="Directory to scan (default: current directory).",
    )
    registry_sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print candidates without writing to config.toml.",
    )

    workflow_parser = subparsers.add_parser(
        "workflow",
        help="Install and manage bundled workflow templates.",
    )
    workflow_subparsers = workflow_parser.add_subparsers(
        dest="workflow_command", required=True
    )
    workflow_install_parser = workflow_subparsers.add_parser(
        "install",
        help="Install a bundled workflow template into the current repository.",
    )
    workflow_install_parser.set_defaults(command="workflow install")
    workflow_install_parser.add_argument(
        "name", help="Workflow template name (e.g. 'preview')."
    )
    workflow_install_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing template files and [preview] section.",
    )
    workflow_install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the install plan without writing anything.",
    )
    add_common_options(workflow_install_parser)

    takeover_parser = subparsers.add_parser(
        "takeover",
        help="Take over GitHub repositories: clone, init, register, and start daemons.",
    )
    takeover_parser.add_argument(
        "--owner",
        default=None,
        help="GitHub user or organization whose repositories to list.",
    )
    takeover_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of repositories to fetch from GitHub (default: 100).",
    )
    takeover_parser.add_argument(
        "--clone-root",
        default=None,
        help="Directory where repositories will be cloned (default: ~/.iar/repos).",
    )
    takeover_parser.add_argument(
        "--repos",
        nargs="+",
        default=[],
        help="Non-interactive mode: list of owner/repo names to take over.",
    )
    takeover_parser.add_argument(
        "--no-start",
        action="store_true",
        help="Take over repositories without starting daemon processes.",
    )
    takeover_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the takeover plan without making changes.",
    )

    return parser
