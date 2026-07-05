"""Typer commands for the agent-driven decision flows.

Holds :func:`ask_command` (natural-language decision entrypoint),
:func:`repl_command` (interactive REPL), and
:func:`deliberate_command` (multi-agent deliberation).
"""

from __future__ import annotations

from typing import Annotated

import typer

from backend.api.cli_typer_app import (
    ConfigOption,
    RepoIdOption,
    RepoOption,
    RunAgentChoice,
    _enum_value,
    _run_typer_command,
    _run_typer_repository_command,
    _typer_selector_options,
    app,
)


@app.command("ask")
def ask_command(
    ctx: typer.Context,
    prompt: Annotated[str, typer.Argument(help="Natural language request.")],
    agent: Annotated[
        RunAgentChoice,
        typer.Option("--agent", help="Planner agent to use."),
    ] = RunAgentChoice.auto,
    plan_only: Annotated[
        bool,
        typer.Option("--plan-only", help="Only generate plan without executing."),
    ] = False,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Allow execution after confirmation."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Auto-confirm non-interactive execution."),
    ] = False,
    output: Annotated[
        str | None,
        typer.Option("--output", help="Output directory for decision audit."),
    ] = None,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
) -> int:
    """Ask the agent runner to decide the next safe action."""
    selector_options = _typer_selector_options(ctx, repo=repo, repo_id=repo_id, config=config)
    return _run_typer_command(
        "ask",
        **selector_options,
        prompt=prompt,
        agent=_enum_value(agent),
        plan_only=plan_only,
        execute=execute,
        yes=yes,
        output=output,
    )


@app.command("repl")
def repl_command(
    ctx: typer.Context,
    agent: Annotated[
        RunAgentChoice,
        typer.Option(
            "--agent",
            help="Override the REPL agent (defaults to [agent_runner.repl].default_agent).",
        ),
    ] = RunAgentChoice.claude,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
) -> int:
    """Run the interactive REPL session."""
    selector_options = _typer_selector_options(ctx, repo=repo, repo_id=repo_id, config=config)
    return _run_typer_command(
        "repl",
        **selector_options,
        agent=_enum_value(agent),
    )


@app.command("deliberate")
def deliberate_command(
    ctx: typer.Context,
    prompt: Annotated[str, typer.Argument(help="Requirement or question.")],
    agents: Annotated[
        str,
        typer.Option("--agents", help="Comma-separated participant profile IDs."),
    ] = "architect,skeptic,implementer",
    rounds: Annotated[
        int | None, typer.Option("--rounds", help="Number of discussion rounds.")
    ] = None,
    synthesizer: Annotated[
        str | None,
        typer.Option("--synthesizer", help="Agent to run synthesis."),
    ] = None,
    output: Annotated[str | None, typer.Option("--output", help="Output directory.")] = None,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Optional session ID for reproducibility."),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Return non-zero exit code if any agent fails."),
    ] = False,
    repo: RepoOption = None,
    repo_id: RepoIdOption = None,
    config: ConfigOption = None,
) -> int:
    """Run a multi-agent deliberation session."""
    return _run_typer_repository_command(
        ctx,
        "deliberate",
        repo=repo,
        repo_id=repo_id,
        config=config,
        prompt=prompt,
        agents=agents,
        rounds=rounds,
        synthesizer=synthesizer,
        output=output,
        session_id=session_id,
        strict=strict,
    )


__all__ = ["ask_command", "deliberate_command", "repl_command"]
