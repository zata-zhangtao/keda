"""Typer command tree for the issue-agent-runner CLI.

NOTE: This Typer frontend delegates actual execution to ``backend.api.cli``
(``_run_parsed_command``). When adding or changing CLI options, defaults, or
argument structure, keep ``cli.py`` in sync so tests, help text, and the real
entry point behave the same way.

After the line-split refactor the command implementations live in focused
per-domain modules (``cli_typer_init`` / ``cli_typer_runner`` / ...) under
:mod:`backend.api`. They are imported for side effects by
:mod:`backend.api.cli_typer_app` to register every Typer command against
:data:`app` before :func:`main` is called.

This module re-exports the symbols test code and downstream callers
historically imported from ``cli_typer`` so existing import paths keep
working.
"""

from __future__ import annotations

from backend.api.cli_typer_app import (
    AllRepositoriesOption,
    ConfigOption,
    ConcurrencyOption,
    DaemonIntervalOption,
    IssueAgentChoice,
    IssueTypeChoice,
    LogsKindChoice,
    MaxIssuesOption,
    RepoIdOption,
    RepoOption,
    RunAgentChoice,
    RunAgentOption,
    app,
    completion_app,
    daemon_app,
    issue_app,
    labels_app,
    loop_app,
    main,
    registry_app,
    worktree_app,
    workflow_app,
)

__all__ = [
    "AllRepositoriesOption",
    "ConfigOption",
    "ConcurrencyOption",
    "DaemonIntervalOption",
    "IssueAgentChoice",
    "IssueTypeChoice",
    "LogsKindChoice",
    "MaxIssuesOption",
    "RepoIdOption",
    "RepoOption",
    "RunAgentChoice",
    "RunAgentOption",
    "app",
    "completion_app",
    "daemon_app",
    "issue_app",
    "labels_app",
    "loop_app",
    "main",
    "registry_app",
    "worktree_app",
    "workflow_app",
]
