"""``iar ask`` / ``iar repl`` / ``iar deliberate`` handlers.

Extracted from :mod:`backend.api.cli`'s monolithic ``_run_parsed_command``
dispatcher.
"""

from __future__ import annotations

from pathlib import Path

from backend.api.cli_console import console

from backend.api.cli_parsed_context import ParsedCommandContext
from backend.api import cli as _cli
from backend.core.use_cases.interactive_decision import run_interactive_decision
from backend.core.shared.models.agent_deliberation import DeliberationSession
from backend.engines.agent_runner.factory import logger
from backend.engines.agent_runner.failure_resolver import AgentFailureResolver
from backend.engines.agent_runner.live_terminal import create_output_view


def run_ask_command(ctx: ParsedCommandContext) -> int:
    """``iar ask``: natural-language decision entrypoint."""
    contexts = _cli._resolve_cli_repository_targets(
        parsed=ctx.parsed,
        runner_settings=ctx.runner_settings,
        repo_id=ctx.repo_id,
        repo_override=ctx.repo_override,
    )
    for context in contexts:
        _cli.require_iar_repository_initialized(context.repo_path, ctx.process_runner)
    if len(contexts) != 1:
        logger.error(
            "ask requires exactly one target repository. Use --repo or --repo-id to specify."
        )
        return 1
    context = contexts[0]
    _cli._ensure_gh_auth_or_prompt(context.repo_path, ctx.process_runner)
    github_client = _cli.create_github_client(context.repo_path, ctx.process_runner)
    planner_runner = _cli.create_planner_runner(ctx.process_runner)
    content_generator = _cli.create_content_generator(ctx.process_runner)
    agent = ctx.parsed.agent
    if agent == "auto":
        agent = context.config.interactive_decision.default_agent
    output_dir = None
    if ctx.parsed.output:
        output_dir = Path(ctx.parsed.output)
    deliberation_config = context.config.deliberation
    transcript_runner = _cli.create_transcript_runner(ctx.process_runner)
    output_view = create_output_view()
    event_sink = _cli.create_event_sink(
        Path(context.config.interactive_decision.default_output_dir),
        output_view,
    )
    return run_interactive_decision(
        user_prompt=ctx.parsed.prompt,
        context=context,
        config=context.config.interactive_decision,
        agent=agent,
        plan_only=ctx.parsed.plan_only,
        execute=ctx.parsed.execute,
        auto_confirm=ctx.parsed.yes,
        output_dir=output_dir,
        planner_runner=planner_runner,
        github_client=github_client,
        process_runner=ctx.process_runner,
        content_generator=content_generator,
        github_client_factory=ctx.github_client_factory,
        deliberation_deps={
            "config": deliberation_config,
            "transcript_runner": transcript_runner,
            "event_sink": event_sink,
            "output_view": output_view,
        },
    )


def run_repl_command(ctx: ParsedCommandContext) -> int:
    """``iar repl``: interactive REPL session."""
    contexts = _cli._resolve_cli_repository_targets(
        parsed=ctx.parsed,
        runner_settings=ctx.runner_settings,
        repo_id=ctx.repo_id,
        repo_override=ctx.repo_override,
    )
    for context in contexts:
        _cli.require_iar_repository_initialized(context.repo_path, ctx.process_runner)
    if len(contexts) != 1:
        logger.error(
            "repl requires exactly one target repository. Use --repo or --repo-id to specify."
        )
        return 1
    context = contexts[0]
    _cli._ensure_gh_auth_or_prompt(context.repo_path, ctx.process_runner)
    github_client = _cli.create_github_client(context.repo_path, ctx.process_runner)
    agent_override = getattr(ctx.parsed, "agent", None)
    if agent_override == "auto":
        logger.error(
            "`--agent auto` is not supported by the REPL entrypoint; "
            "use `claude`, `codex`, or `kimi`. "
            "Falling back to [agent_runner.repl].default_agent."
        )
        agent_override = None
    effective_agent = agent_override or context.config.repl.default_agent
    content_generator = _cli.create_content_generator(ctx.process_runner, read_only=False)
    command_executor = _cli.create_repl_command_executor(
        process_runner=ctx.process_runner,
        config=context.config.repl,
    )
    inputs = _cli.ReplSessionInputs(
        context=context,
        agent=effective_agent,
        config=context.config.repl,
    )
    deps = _cli.ReplSessionDeps(
        process_runner=ctx.process_runner,
        content_generator=content_generator,
        command_executor=command_executor,
        github_client=github_client,
    )
    return _cli.run_repl_session(inputs, deps)


def run_deliberate_command(ctx: ParsedCommandContext) -> int:
    """``iar deliberate``: multi-agent deliberation session."""
    contexts = _cli._resolve_cli_repository_targets(
        parsed=ctx.parsed,
        runner_settings=ctx.runner_settings,
        repo_id=ctx.repo_id,
        repo_override=ctx.repo_override,
    )
    if len(contexts) != 1:
        logger.error(
            "deliberate requires exactly one target repository. Use --repo or --repo-id to specify."
        )
        return 1
    context = contexts[0]
    _cli.require_iar_repository_initialized(context.repo_path, ctx.process_runner)
    deliberation_settings = context.config.deliberation
    output_dir = ctx.parsed.output or deliberation_settings.default_output_dir
    rounds = (
        ctx.parsed.rounds if ctx.parsed.rounds is not None else deliberation_settings.default_rounds
    )
    synthesizer = ctx.parsed.synthesizer or deliberation_settings.default_synthesizer
    agents = tuple(a.strip() for a in ctx.parsed.agents.split(",") if a.strip())
    session_id = ctx.parsed.session_id or _cli.create_default_session_id()
    output_path = Path(output_dir) / session_id
    request = _cli.DeliberationRequest(
        prompt=ctx.parsed.prompt,
        agents=agents,
        rounds=rounds,
        synthesizer=synthesizer,
        output_dir=str(output_path),
        session_id=session_id,
    )
    deliberation_config = context.config.deliberation
    transcript_runner = _cli.create_transcript_runner(ctx.process_runner)
    output_path.mkdir(parents=True, exist_ok=True)
    output_view = create_output_view()
    event_sink = _cli.create_event_sink(output_path, output_view)
    resolver = AgentFailureResolver()
    result = _cli.run_agent_deliberation(
        request=request,
        config=deliberation_config,
        transcript_runner=transcript_runner,
        event_sink=event_sink,
        target_repo_path=context.repo_path,
        output_view=output_view,
        resolver=resolver.resolve,
    )
    selected_profile_ids = tuple(
        dict.fromkeys(
            profile_id for outputs in result.agent_outputs.values() for profile_id in outputs
        )
    )
    profiles_by_id = {profile.profile_id: profile for profile in deliberation_config.profiles}
    session_profiles = tuple(
        profiles_by_id[profile_id]
        for profile_id in selected_profile_ids
        if profile_id in profiles_by_id
    )
    session = DeliberationSession(
        session_id=result.session_id,
        prompt=result.prompt,
        profiles=session_profiles,
        rounds=request.rounds,
        synthesizer=request.synthesizer,
        output_dir=output_path,
        started_at=result.started_at,
        finished_at=result.finished_at,
    )
    _cli.write_deliberation_outputs(result, session, output_path)
    console.print(f"\n[green]Deliberation complete:[/] {output_path}")
    if result.failed_agents:
        for failure in result.failed_agents:
            logger.warning(
                "Deliberation agent failed: profile=%s attempted=%s fallback=%s reason=%s",
                failure.profile_id,
                failure.attempted_agent,
                failure.fallback_agent,
                failure.reason,
            )
            console.print(
                f"[yellow]Agent '{failure.profile_id}' failed "
                f"(attempted={failure.attempted_agent}, "
                f"fallback={failure.fallback_agent or 'none'}, "
                f"reason={failure.reason}).[/]"
            )
    strict_mode = ctx.parsed.strict or not deliberation_config.continue_on_agent_error
    all_participants_failed = len(selected_profile_ids) > 0 and all(
        profile_id in {f.profile_id for f in result.failed_agents}
        for profile_id in selected_profile_ids
    )
    synthesizer_failed = any(
        failure.profile_id == "synthesizer" for failure in result.failed_agents
    )
    if strict_mode and result.failed_agents:
        return 1
    if all_participants_failed:
        return 1
    if synthesizer_failed and not any(
        failure.profile_id != "synthesizer" for failure in result.failed_agents
    ):
        return 1
    return 0


__all__ = ["run_ask_command", "run_deliberate_command", "run_repl_command"]
