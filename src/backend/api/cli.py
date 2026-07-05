"""Command-line interface for issue-agent-runner.

NOTE: This argparse-based parser is still the execution backend for
``backend.api.cli_typer``. When adding or changing CLI options, defaults, or
argument structure, keep ``cli_typer.py`` in sync so the actual ``iar`` entry
point and its help text stay consistent.

After the line-split refactor the per-subcommand bodies of
``_run_parsed_command`` live in :mod:`backend.api.cli_parsed_commands`. The
top-level dispatch table maps each ``parsed.command`` to its focused
handler; the helpers and the public ``main()`` entrypoint stay here.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from backend.api.cli_console import error_console
from backend.api.cli_helpers import (
    _handle_not_initialized_error,
    _resolve_default_daemon_target,
)
from backend.api.cli_parsed_commands import (
    ParsedCommandContext,
    dispatch_parsed_command,
)
from backend.engines.agent_runner.factory import (
    create_github_client,
    create_process_runner,
    logger,
)
from backend.engines.agent_runner.repository_local import (
    IARRepositoryNotInitializedError,
)

if TYPE_CHECKING:
    from backend.core.shared.interfaces.agent_runner import IGitHubClient


def _resolve_loop_target_repository(
    *,
    loop_repo_id: str | None,
    loop_repo_path: str | None,
    runner_settings,
) -> "object | None":
    """Resolve the repository that the loop command targets.

    Loops may carry their own ``repo_id`` (resolved from the recipe when
    not overridden on the CLI). When the user did not pass ``--repo-id``
    or ``--repo``, we infer the target from the current working directory
    the same way ``iar daemon`` does, falling back to ``None`` so the
    per-loop ``repo_id`` is honored.
    """
    if loop_repo_path:
        contexts = resolve_repository_targets(
            runner_settings,
            repo_path_override=loop_repo_path,
            fallback_path=loop_repo_path,
        )
        return contexts[0] if contexts else None
    if loop_repo_id:
        contexts = resolve_repository_targets(
            runner_settings,
            repo_id=loop_repo_id,
        )
        return contexts[0] if contexts else None
    cwd_target = _resolve_default_daemon_target()
    if cwd_target.repo_id:
        contexts = resolve_repository_targets(
            runner_settings,
            repo_id=cwd_target.repo_id,
        )
        return contexts[0] if contexts else None
    return None


def _run_loop_command(parsed: argparse.Namespace, process_runner) -> int:
    """Dispatch ``iar loop ...`` and ``iar loop-daemon`` commands."""
    from backend.api.cli_loop import build_schedule_from_args, logger as loop_logger

    runner_settings = get_agent_runner_settings()

    def _state_store_factory():
        return create_loop_state_store()

    def _github_client_factory(repo_path: Path) -> "IGitHubClient":
        return create_github_client(repo_path, process_runner)

    def _content_generator_factory(repo_path: Path):
        return create_content_generator(process_runner)

    def _repo_resolver(task):
        """Resolve the on-disk path of the repository a loop targets."""
        explicit_repo_id = getattr(parsed, "loop_repo_id", None)
        explicit_repo = getattr(parsed, "loop_repo", None)
        context = _resolve_loop_target_repository(
            loop_repo_id=explicit_repo_id or task.repo_id,
            loop_repo_path=explicit_repo,
            runner_settings=runner_settings,
        )
        if context is None:
            raise ValueError(
                f"Loop '{task.id}' targets repo_id {task.repo_id!r} which is "
                "not registered. Run `iar registry list` or pass "
                "`--repo-id` / `--repo` to target a specific repository."
            )
        return context.repo_path

    command = parsed.command

    if command == "loop create":
        return run_loop_create_command(parsed, state_store_factory=_state_store_factory)

    if command == "loop list":
        return run_loop_list_command(state_store_factory=_state_store_factory)

    if command == "loop cancel":
        return run_loop_cancel_command(parsed, state_store_factory=_state_store_factory)

    # The remaining commands need clock + repo resolution. Validate the
    # --cron/--every combination up front so the user gets a friendly
    # error before we touch the loop state.
    if command == "loop run":
        try:
            build_schedule_from_args(parsed)
        except ValueError as exc:
            loop_logger.error("%s", exc)
            return 1
        return run_loop_run_now_command(
            parsed,
            state_store_factory=_state_store_factory,
            github_client_factory=_github_client_factory,
            process_runner=process_runner,
            clock=create_loop_clock(),
            repo_resolver=_repo_resolver,
            content_generator_factory=_content_generator_factory,
            labels_config=None,
        )

    if command == "loop-daemon":
        return run_loop_daemon_command(
            parsed,
            state_store_factory=_state_store_factory,
            github_client_factory=_github_client_factory,
            process_runner=process_runner,
            clock=create_loop_clock(),
            repo_resolver=_repo_resolver,
            content_generator_factory=_content_generator_factory,
            labels_config=None,
        )

    loop_logger.error("Unsupported loop command: %s", command)
    return 1


def _run_parsed_command(parsed: argparse.Namespace) -> int:
    """Run a command after CLI arguments have been parsed.

    Pre-dispatch validation runs here (deprecated flag warnings,
    ``--repo``/``--repo-id`` exclusivity, the cwd-based default for
    daemon / review-daemon / logs), then the matching handler from
    :mod:`backend.api.cli_parsed_commands` is invoked through
    :func:`dispatch_parsed_command`.
    """
    from backend.api.cli_utils import _format_cli_exception

    if parsed.config:
        logger.warning("The --config flag is deprecated. Use config.toml or env vars instead.")

    repo_id: str | None = getattr(parsed, "repo_id", None)
    repo_override: str | None = getattr(parsed, "repo", None)

    if repo_id is not None and repo_override is not None:
        logger.error("--repo and --repo-id are mutually exclusive.")
        return 1

    # daemon / review-daemon 在未指定仓库时：
    # 1. cwd 命中唯一 enabled 注册仓 → 仅处理该仓（与 --repo-id 等价）
    # 2. cwd 命中 disabled 注册仓 → 报错
    # 3. cwd 命中多个 enabled 注册仓 → 报错，要求显式选择
    # 4. cwd 未命中任何注册仓或未初始化 → 报错，不再回退到 --all
    if parsed.command in ("daemon", "review-daemon", "logs"):
        if (
            repo_id is None
            and repo_override is None
            and not getattr(parsed, "all_repositories", False)
        ):
            default_target = _resolve_default_daemon_target()
            if default_target.error:
                logger.error(default_target.error)
                return 1
            repo_id = default_target.repo_id

    process_runner = create_process_runner()
    runner_settings = get_agent_runner_settings()

    def github_client_factory(repo_path: Path) -> "IGitHubClient":
        return create_github_client(repo_path, process_runner)

    parsed_ctx = ParsedCommandContext(
        parsed=parsed,
        process_runner=process_runner,
        runner_settings=runner_settings,
        repo_id=repo_id,
        repo_override=repo_override,
        github_client_factory=github_client_factory,
    )

    try:
        exit_code = dispatch_parsed_command(parsed_ctx)
    except IARRepositoryNotInitializedError as exc:
        return _handle_not_initialized_error(exc)
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
        error_detail = _format_cli_exception(exc)
        logger.error("iar failed:\n%s", error_detail)
        error_console.print("[red]iar failed:[/]")
        error_console.print(error_detail, markup=False)
        return 1

    if exit_code is None:
        logger.error("Unsupported command: %s", parsed.command)
        return 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Run the Typer-powered CLI."""
    from backend.api.cli_typer import main as typer_main

    return typer_main(argv)


# Backward-compatible re-exports. After the line-split refactor these
# helpers live in dedicated modules; tests and downstream callers can
# keep importing them through ``backend.api.cli``. The actual symbols are
# in :mod:`backend.api.cli_reexports` so that ``cli_parsed_commands`` and
# ``cli`` share a single module-level name binding — that way a test
# ``patch("backend.api.cli.run_agent_daemon")`` correctly shadows the
# function the handler will call.
from backend.api.cli_reexports import (  # noqa: E402,F401
    DaemonAlreadyRunningError,
    DeliberationRequest,
    IssueFromPrdRequest,
    ReplSessionDeps,
    ReplSessionInputs,
    _ensure_gh_auth_or_prompt,
    _expand_prd_paths,
    _prompt_and_publish_prd_if_needed,
    _resolve_cli_repository_targets,
    _resolve_run_trigger,
    acquire_daemon_locks,
    create_content_generator,
    create_default_session_id,
    create_event_sink,
    create_issue_from_prd,
    create_loop_clock,
    create_loop_state_store,
    create_planner_runner,
    create_repl_command_executor,
    create_transcript_runner,
    daemon_lock_dir,
    get_agent_runner_settings,
    release_daemon_locks,
    require_iar_repository_initialized,
    resolve_issue_from_prd_target,
    resolve_prd_paths,
    resolve_repository_targets,
    review_once,
    run_agent_daemon,
    run_agent_deliberation,
    run_agent_repositories_once,
    run_loop_cancel_command,
    run_loop_create_command,
    run_loop_daemon_command,
    run_loop_list_command,
    run_loop_run_now_command,
    run_repl_session,
    run_review_daemon,
    sync_labels,
    write_deliberation_outputs,
)


if __name__ == "__main__":
    raise SystemExit(main())
