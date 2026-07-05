"""Re-exports of use-case entry points and helpers used by the CLI.

After the line-split refactor :mod:`backend.api.cli` is a thin dispatcher
that delegates each parsed command to a focused module under
:mod:`backend.api.cli_parsed_commands`. The original ``_run_parsed_command``
referenced dozens of use-case functions and infrastructure helpers as
module-level names (e.g. ``run_agent_daemon``, ``require_iar_repository_initialized``,
``create_github_client``); the test suite patches those names on
``backend.api.cli`` (e.g. ``patch("backend.api.cli.run_agent_daemon")``).

To keep those patches effective without making ``cli.py`` import every
module eagerly (which would reintroduce a circular dependency), this
module re-exports the symbols that tests look up. Both
:mod:`backend.api.cli` and the per-command modules under
:mod:`backend.api.cli_parsed_commands` import from here so a single
``patch("backend.api.cli.X")`` call still shadows the function used by
the handler.
"""

from __future__ import annotations

from backend.api.cli_helpers import (
    _ensure_gh_auth_or_prompt,
    _resolve_cli_repository_targets,
    _resolve_run_trigger,
)
from backend.api.cli_prd_utils import (
    _expand_prd_paths,
    _prompt_and_publish_prd_if_needed,
)
from backend.core.use_cases.create_issue_from_prd import (
    IssueFromPrdRequest,
    create_issue_from_prd,
    resolve_prd_paths,
)
from backend.core.use_cases.daemon_single_instance import (
    DaemonAlreadyRunningError,
    acquire_daemon_locks,
    daemon_lock_dir,
    release_daemon_locks,
)
from backend.core.use_cases.run_agent_daemon import run_agent_daemon
from backend.core.use_cases.run_agent_repositories_once import (
    run_agent_repositories_once,
)
from backend.core.use_cases.review_daemon import run_review_daemon
from backend.core.use_cases.review_once import review_once
from backend.core.use_cases.repl_session import (  # noqa: E402,F401
    ReplSessionDeps,
    ReplSessionInputs,
    run_repl_session,
)
from backend.core.use_cases.run_agent_deliberation import (  # noqa: E402,F401
    DeliberationRequest,
    create_default_session_id,
    run_agent_deliberation,
)
from backend.core.use_cases.sync_labels import sync_labels
from backend.api.cli_loop import (  # noqa: E402,F401
    run_loop_cancel_command,
    run_loop_create_command,
    run_loop_daemon_command,
    run_loop_list_command,
    run_loop_run_now_command,
)
from backend.engines.agent_runner.factory import (  # noqa: E402,F401
    resolve_repository_targets,
)
from backend.engines.agent_runner.factory import (
    create_content_generator,
    create_event_sink,
    create_github_client,
    create_loop_clock,
    create_loop_state_store,
    create_planner_runner,
    create_process_runner,
    create_repl_command_executor,
    create_transcript_runner,
    get_agent_runner_settings,
    resolve_issue_from_prd_target,
    write_deliberation_outputs,
)
from backend.engines.agent_runner.repository_local import (
    require_iar_repository_initialized,
)

__all__ = [
    "DaemonAlreadyRunningError",
    "DeliberationRequest",
    "IssueFromPrdRequest",
    "ReplSessionDeps",
    "ReplSessionInputs",
    "_ensure_gh_auth_or_prompt",
    "_expand_prd_paths",
    "_prompt_and_publish_prd_if_needed",
    "_resolve_cli_repository_targets",
    "_resolve_run_trigger",
    "acquire_daemon_locks",
    "create_content_generator",
    "create_default_session_id",
    "create_event_sink",
    "create_github_client",
    "create_issue_from_prd",
    "create_loop_clock",
    "create_loop_state_store",
    "create_planner_runner",
    "create_process_runner",
    "create_repl_command_executor",
    "create_transcript_runner",
    "daemon_lock_dir",
    "get_agent_runner_settings",
    "release_daemon_locks",
    "require_iar_repository_initialized",
    "resolve_issue_from_prd_target",
    "resolve_prd_paths",
    "resolve_repository_targets",
    "resolve_repository_targets",
    "review_once",
    "run_agent_daemon",
    "run_agent_deliberation",
    "run_agent_repositories_once",
    "run_loop_cancel_command",
    "run_loop_create_command",
    "run_loop_daemon_command",
    "run_loop_list_command",
    "run_loop_run_now_command",
    "run_repl_session",
    "run_review_daemon",
    "sync_labels",
    "write_deliberation_outputs",
]
