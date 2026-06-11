"""Interactive decision use case for `iar ask`.

Orchestrates context collection, planner call, strict JSON parsing,
whitelist validation, audit writing and allowed action dispatch.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.models.agent_decision import (
    DecisionAction,
    DecisionActionType,
    DecisionContext,
    DecisionExecutionResult,
    DecisionPlan,
    DecisionRiskLevel,
    InteractiveDecisionConfig,
)
from backend.core.shared.models.agent_deliberation import (
    DeliberationEvent,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    RepositoryRunContext,
)
from backend.core.use_cases.create_issue_from_prd import (
    IssueFromPrdRequest,
    create_issue_from_prd,
)
from backend.core.use_cases.run_agent_deliberation import (
    DeliberationRequest,
    create_default_session_id,
    run_agent_deliberation,
)
from backend.core.use_cases.run_agent_repositories_once import (
    run_agent_repositories_once,
)
from backend.core.use_cases.review_once import review_once

_logger = logging.getLogger(__name__)

_ALLOWED_ACTION_TYPES: set[DecisionActionType] = {
    DecisionActionType.SHOW_STATUS,
    DecisionActionType.RUN_DELIBERATION,
    DecisionActionType.CREATE_ISSUE_FROM_PRD,
    DecisionActionType.MARK_ISSUE_READY,
    DecisionActionType.RUN_ONCE_DRY_RUN,
    DecisionActionType.RUN_ONCE,
    DecisionActionType.REVIEW_ONCE_DRY_RUN,
    DecisionActionType.REVIEW_ONCE,
    DecisionActionType.NEEDS_CLARIFICATION,
    DecisionActionType.NO_OP,
}

_ACTION_TYPE_TO_RISK: dict[DecisionActionType, DecisionRiskLevel] = {
    DecisionActionType.SHOW_STATUS: DecisionRiskLevel.LOW,
    DecisionActionType.RUN_DELIBERATION: DecisionRiskLevel.LOW,
    DecisionActionType.CREATE_ISSUE_FROM_PRD: DecisionRiskLevel.MEDIUM,
    DecisionActionType.MARK_ISSUE_READY: DecisionRiskLevel.MEDIUM,
    DecisionActionType.RUN_ONCE_DRY_RUN: DecisionRiskLevel.LOW,
    DecisionActionType.RUN_ONCE: DecisionRiskLevel.HIGH,
    DecisionActionType.REVIEW_ONCE_DRY_RUN: DecisionRiskLevel.LOW,
    DecisionActionType.REVIEW_ONCE: DecisionRiskLevel.MEDIUM,
    DecisionActionType.NEEDS_CLARIFICATION: DecisionRiskLevel.LOW,
    DecisionActionType.NO_OP: DecisionRiskLevel.LOW,
}

_ACTION_TYPE_REQUIRES_CONFIRMATION: set[DecisionActionType] = {
    DecisionActionType.CREATE_ISSUE_FROM_PRD,
    DecisionActionType.MARK_ISSUE_READY,
    DecisionActionType.RUN_ONCE,
    DecisionActionType.REVIEW_ONCE,
}

_HIGH_RISK_ACTIONS: set[DecisionActionType] = {
    DecisionActionType.RUN_ONCE,
}

_FORBIDDEN_ACTIONS: set[str] = {
    "git_push",
    "git_merge",
    "git_reset",
    "daemon",
    "review_daemon",
    "shell",
    "arbitrary_shell",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate_prompt(prompt: str, max_chars: int) -> str:
    """Truncate prompt if it exceeds max_chars, preserving both ends."""
    if not isinstance(max_chars, int) or len(prompt) <= max_chars:
        return prompt
    keep_each = (max_chars - 50) // 2
    return (
        prompt[:keep_each]
        + "\n\n...[context truncated due to length]...\n\n"
        + prompt[-keep_each:]
    )


def _generate_decision_id() -> str:
    return datetime.now(timezone.utc).strftime("dec-%Y%m%d-%H%M%S-%f")[:-3]


def _read_prd_title(prd_path: Path) -> str:
    """Extract the first heading from a PRD file as its title."""
    try:
        text = prd_path.read_text(encoding="utf-8")
        for line in text.splitlines()[:20]:
            match = re.match(r"^#\s+(.+)", line)
            if match:
                return match.group(1).strip()
    except Exception:
        pass
    return prd_path.name


def _build_pending_prd_summary(repo_path: Path) -> str:
    """Scan tasks/pending/ for PRD files and return a concise summary."""
    pending_dir = repo_path / "tasks" / "pending"
    if not pending_dir.exists():
        return "No tasks/pending/ directory found."
    prd_files = sorted(pending_dir.glob("*.md"))
    if not prd_files:
        return "No pending PRD files found."
    lines: list[str] = []
    for prd_file in prd_files[:10]:
        title = _read_prd_title(prd_file)
        rel = prd_file.relative_to(repo_path).as_posix()
        lines.append(f"- {rel}: {title}")
    if len(prd_files) > 10:
        lines.append(f"... and {len(prd_files) - 10} more")
    return "\n".join(lines)


def _build_issue_summary(
    repo_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
) -> str:
    """Query GitHub for relevant issues and return a concise summary."""
    labels = [
        config.labels.ready,
        config.labels.waiting,
        config.labels.supervising,
        config.labels.review,
    ]
    lines: list[str] = []
    for label in labels:
        try:
            issues = github_client.list_issues_by_label(label, limit=5, state="open")
            if issues:
                lines.append(f"{label}:")
                for issue in issues:
                    lines.append(f"  #{issue.number}: {issue.title}")
        except Exception as exc:
            _logger.warning("Failed to list issues for label %s: %s", label, exc)
    if not lines:
        return "No relevant open issues found."
    return "\n".join(lines)


def build_decision_context(
    context: RepositoryRunContext,
    config: InteractiveDecisionConfig,
    github_client: IGitHubClient,
) -> DecisionContext:
    """Collect and truncate context for the planner agent."""
    repo_config = context.config
    config_summary = (
        f"labels: ready={repo_config.labels.ready}, "
        f"running={repo_config.labels.running}, "
        f"supervising={repo_config.labels.supervising}, "
        f"review={repo_config.labels.review}"
    )
    pending_prd_summary = _build_pending_prd_summary(context.repo_path)
    issue_summary = _build_issue_summary(
        context.repo_path,
        repo_config,
        github_client,
    )
    allowed_actions = ", ".join(sorted(a.value for a in _ALLOWED_ACTION_TYPES))
    forbidden_actions = ", ".join(sorted(_FORBIDDEN_ACTIONS))

    return DecisionContext(
        repo_id=context.repo_id,
        repo_path=context.repo_path,
        display_name=context.display_name,
        config_summary=config_summary,
        pending_prd_summary=pending_prd_summary,
        issue_summary=issue_summary,
        allowed_actions_summary=allowed_actions,
        forbidden_actions_summary=forbidden_actions,
    )


def _build_planner_prompt(
    user_prompt: str,
    decision_context: DecisionContext,
) -> str:
    """Build the constrained planner prompt with JSON schema instructions."""
    return (
        "You are a constrained decision planner for an agent runner CLI. "
        "Your job is to analyze the user's request and output a strict JSON object.\n\n"
        "User request:\n"
        f"{user_prompt}\n\n"
        "Repository context:\n"
        f"- repo_id: {decision_context.repo_id}\n"
        f"- repo_path: {decision_context.repo_path}\n"
        f"- config: {decision_context.config_summary}\n\n"
        "Pending PRDs:\n"
        f"{decision_context.pending_prd_summary}\n\n"
        "Relevant Issues:\n"
        f"{decision_context.issue_summary}\n\n"
        "Allowed actions:\n"
        f"{decision_context.allowed_actions_summary}\n\n"
        "Forbidden actions (never recommend these):\n"
        f"{decision_context.forbidden_actions_summary}\n\n"
        "Risk levels:\n"
        "- low: read-only or local-only operations\n"
        "- medium: creates/modifies GitHub Issues or labels\n"
        "- high: executes agent code changes\n\n"
        "Rules:\n"
        "1. Output ONLY valid JSON. No markdown code fences, no human prose outside JSON.\n"
        "2. Do NOT recommend shell commands as actions.\n"
        "3. Do NOT recommend forbidden actions.\n"
        "4. For create_issue_from_prd, default ready=false unless user explicitly asks to queue.\n"
        "5. For run_once, default max_issues=1.\n"
        "6. If the request is unclear, use needs_clarification.\n"
        "7. If no action is needed, use no_op.\n\n"
        "JSON schema:\n"
        "{\n"
        '  "decision_id": "dec-YYYYMMDD-HHMMSS-XXXX",\n'
        '  "user_prompt": "original prompt",\n'
        '  "intent_summary": "1-sentence summary",\n'
        '  "risk_level": "low|medium|high",\n'
        '  "actions": [\n'
        "    {\n"
        '      "action_id": "A1",\n'
        '      "action_type": "<allowed_action_type>",\n'
        '      "title": "human-readable title",\n'
        '      "rationale": "why this action",\n'
        '      "parameters": {"key": "value"},\n'
        '      "writes_external_state": true|false,\n'
        '      "confirmation_required": true|false\n'
        "    }\n"
        "  ],\n"
        '  "assumptions": ["assumption 1"],\n'
        '  "warnings": ["warning 1"],\n'
        '  "requires_confirmation": true|false\n'
        "}\n"
    )


def _parse_risk_level(value: str) -> DecisionRiskLevel:
    try:
        return DecisionRiskLevel(value.lower())
    except ValueError as exc:
        raise ValueError(f"Unknown risk_level: {value}") from exc


def _parse_action_type(value: str) -> DecisionActionType:
    try:
        return DecisionActionType(value.lower())
    except ValueError as exc:
        raise ValueError(f"Unknown action_type: {value}") from exc


def _parse_action(raw: dict[str, Any]) -> DecisionAction:
    """Parse a single action from raw JSON dict."""
    action_type = _parse_action_type(raw.get("action_type", ""))
    parameters = dict(raw.get("parameters", {}))
    return DecisionAction(
        action_id=str(raw.get("action_id", "")),
        action_type=action_type,
        title=str(raw.get("title", "")),
        rationale=str(raw.get("rationale", "")),
        parameters=parameters,
        writes_external_state=bool(raw.get("writes_external_state", False)),
        confirmation_required=bool(raw.get("confirmation_required", False)),
    )


def parse_decision_plan(
    user_prompt: str,
    planner_stdout: str,
) -> DecisionPlan:
    """Parse planner stdout into a strict DecisionPlan."""
    # Try to extract JSON from the stdout (in case there's extra text)
    text = planner_stdout.strip()
    # Remove markdown code fences if present
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # Find the first '{' and last '}'
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in planner output.")
    json_text = text[start : end + 1]

    data: dict[str, Any] = json.loads(json_text)

    decision_id = str(data.get("decision_id", _generate_decision_id()))
    risk_level = _parse_risk_level(data.get("risk_level", "low"))

    raw_actions = data.get("actions", [])
    if not isinstance(raw_actions, list):
        raise ValueError("'actions' must be a list.")
    actions = tuple(_parse_action(a) for a in raw_actions)

    return DecisionPlan(
        decision_id=decision_id,
        user_prompt=user_prompt,
        intent_summary=str(data.get("intent_summary", "")),
        risk_level=risk_level,
        actions=actions,
        assumptions=tuple(data.get("assumptions", [])),
        warnings=tuple(data.get("warnings", [])),
        requires_confirmation=bool(data.get("requires_confirmation", False)),
    )


def validate_decision_plan(
    plan: DecisionPlan,
    repo_path: Path,
    auto_confirm: bool,
) -> None:
    """Validate plan against whitelist, parameters, and risk policies.

    Raises:
        ValueError: If validation fails.
    """
    for action in plan.actions:
        if action.action_type not in _ALLOWED_ACTION_TYPES:
            raise ValueError(
                f"Action '{action.action_id}' has forbidden type: {action.action_type.value}"
            )

        # Write actions must require confirmation (defense in depth)
        if (
            action.action_type in _ACTION_TYPE_REQUIRES_CONFIRMATION
            and not action.confirmation_required
        ):
            raise ValueError(
                f"Action '{action.action_id}' ({action.action_type.value}) writes external state "
                f"and must have confirmation_required=true"
            )

        # Check forbidden action names in parameters (defense in depth)
        for param_key in action.parameters:
            if param_key.lower() in _FORBIDDEN_ACTIONS:
                raise ValueError(
                    f"Action '{action.action_id}' has forbidden parameter: {param_key}"
                )

        # Required parameter validation
        if action.action_type == DecisionActionType.CREATE_ISSUE_FROM_PRD:
            if "prd_path" not in action.parameters:
                raise ValueError(
                    f"Action '{action.action_id}' requires 'prd_path' parameter"
                )

        if action.action_type == DecisionActionType.MARK_ISSUE_READY:
            if "issue_number" not in action.parameters:
                raise ValueError(
                    f"Action '{action.action_id}' requires 'issue_number' parameter"
                )

        # PRD path validation
        prd_path_param = action.parameters.get("prd_path")
        if prd_path_param is not None:
            prd_path = Path(str(prd_path_param))
            if prd_path.is_absolute():
                try:
                    prd_path.resolve().relative_to(repo_path.resolve())
                except ValueError as exc:
                    raise ValueError(
                        f"Action '{action.action_id}' PRD path outside repo: {prd_path}"
                    ) from exc
            else:
                # Relative paths are validated to be within the repo
                resolved = (repo_path / prd_path).resolve()
                try:
                    resolved.relative_to(repo_path.resolve())
                except ValueError as exc:
                    raise ValueError(
                        f"Action '{action.action_id}' PRD path outside repo: {prd_path}"
                    ) from exc

        # Issue number validation
        issue_number = action.parameters.get("issue_number")
        if issue_number is not None:
            try:
                num = int(issue_number)
                if num <= 0:
                    raise ValueError(f"Issue number must be positive: {num}")
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"Action '{action.action_id}' has invalid issue_number: {issue_number}"
                ) from exc

        # High risk + auto_confirm check
        if auto_confirm and action.action_type in _HIGH_RISK_ACTIONS:
            raise ValueError(
                f"Action '{action.action_id}' is high-risk and cannot use --yes: "
                f"{action.action_type.value}"
            )

        # Auto-confirm only allowed for low/medium risk actions that permit it
        if auto_confirm and action.confirmation_required:
            expected_risk = _ACTION_TYPE_TO_RISK.get(action.action_type)
            if expected_risk == DecisionRiskLevel.HIGH:
                raise ValueError(
                    f"Action '{action.action_id}' requires confirmation and cannot use --yes"
                )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _plan_to_dict(plan: DecisionPlan) -> dict[str, Any]:
    return {
        "decision_id": plan.decision_id,
        "user_prompt": plan.user_prompt,
        "intent_summary": plan.intent_summary,
        "risk_level": plan.risk_level.value,
        "actions": [
            {
                "action_id": a.action_id,
                "action_type": a.action_type.value,
                "title": a.title,
                "rationale": a.rationale,
                "parameters": dict(a.parameters),
                "writes_external_state": a.writes_external_state,
                "confirmation_required": a.confirmation_required,
            }
            for a in plan.actions
        ],
        "assumptions": list(plan.assumptions),
        "warnings": list(plan.warnings),
        "requires_confirmation": plan.requires_confirmation,
    }


def _context_to_dict(decision_context: DecisionContext) -> dict[str, Any]:
    return {
        "repo_id": str(decision_context.repo_id),
        "repo_path": str(decision_context.repo_path),
        "display_name": str(decision_context.display_name),
        "config_summary": str(decision_context.config_summary),
        "pending_prd_summary": str(decision_context.pending_prd_summary),
        "issue_summary": str(decision_context.issue_summary),
        "allowed_actions_summary": str(decision_context.allowed_actions_summary),
        "forbidden_actions_summary": str(decision_context.forbidden_actions_summary),
    }


def _plan_to_markdown(plan: DecisionPlan) -> str:
    lines = [
        f"# Decision Plan: {plan.decision_id}",
        "",
        f"**Intent:** {plan.intent_summary}",
        f"**Risk:** {plan.risk_level.value}",
        "",
        "## Actions",
        "",
    ]
    for action in plan.actions:
        lines.append(f"### {action.action_id}: {action.title}")
        lines.append(f"- **Type:** {action.action_type.value}")
        lines.append(f"- **Rationale:** {action.rationale}")
        if action.parameters:
            lines.append(
                f"- **Parameters:** {json.dumps(dict(action.parameters), ensure_ascii=False)}"
            )
        lines.append(f"- **Writes External State:** {action.writes_external_state}")
        lines.append(f"- **Confirmation Required:** {action.confirmation_required}")
        lines.append("")
    if plan.assumptions:
        lines.append("## Assumptions")
        for assumption in plan.assumptions:
            lines.append(f"- {assumption}")
        lines.append("")
    if plan.warnings:
        lines.append("## Warnings")
        for warning in plan.warnings:
            lines.append(f"- {warning}")
        lines.append("")
    return "\n".join(lines)


def write_decision_audit(
    plan: DecisionPlan,
    decision_context: DecisionContext,
    output_dir: Path,
) -> None:
    """Write plan audit files to the decision output directory."""
    decision_dir = output_dir / plan.decision_id
    _write_json(decision_dir / "plan.json", _plan_to_dict(plan))
    (decision_dir / "plan.md").write_text(_plan_to_markdown(plan), encoding="utf-8")
    _write_json(
        decision_dir / "context-summary.json", _context_to_dict(decision_context)
    )


def write_execution_audit(
    result: DecisionExecutionResult,
    output_dir: Path,
) -> None:
    """Write execution audit files."""
    decision_dir = output_dir / result.decision_id
    data = {
        "decision_id": result.decision_id,
        "status": result.status,
        "summary": result.summary,
        "action_results": list(result.action_results),
        "executed_at": _now_iso(),
    }
    _write_json(decision_dir / "execution.json", data)
    lines = [
        f"# Execution Result: {result.decision_id}",
        "",
        f"**Status:** {result.status}",
        f"**Summary:** {result.summary}",
        "",
        "## Action Results",
        "",
    ]
    for ar in result.action_results:
        lines.append(
            f"- {ar.get('action_id', 'unknown')}: {ar.get('status', 'unknown')}"
        )
        if ar.get("error"):
            lines.append(f"  - Error: {ar['error']}")
    lines.append("")
    (decision_dir / "execution.md").write_text("\n".join(lines), encoding="utf-8")


def _confirm_action_interactive(action: DecisionAction, decision_id: str) -> bool:
    """Prompt user for confirmation in TTY mode."""
    if not sys.stdin.isatty():
        return False
    print(f"\nAction {action.action_id}: {action.title}")
    print(f"  Type: {action.action_type.value}")
    print(f"  Rationale: {action.rationale}")
    if action.writes_external_state:
        print("  ⚠️  This action writes external state.")
    response = input(f"Type '{decision_id}' to confirm execution: ")
    return response.strip() == decision_id


def _execute_show_status(
    action: DecisionAction,
    context: RepositoryRunContext,
    decision_context: DecisionContext,
) -> dict[str, str]:
    return {
        "action_id": action.action_id,
        "status": "completed",
        "output": (
            f"Repository: {context.repo_id} ({context.display_name})\n"
            f"Path: {context.repo_path}\n"
            f"Config: {decision_context.config_summary}\n"
            f"Pending PRDs:\n{decision_context.pending_prd_summary}\n"
            f"Issues:\n{decision_context.issue_summary}"
        ),
    }


def _execute_create_issue_from_prd(
    action: DecisionAction,
    context: RepositoryRunContext,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None,
) -> dict[str, str]:
    prd_path_str = str(action.parameters.get("prd_path", ""))
    prd_path = Path(prd_path_str)
    if not prd_path.is_absolute():
        prd_path = context.repo_path / prd_path

    ready = bool(action.parameters.get("ready", False))
    agent = str(action.parameters.get("agent", "auto"))
    issue_type = str(action.parameters.get("issue_type", "feature"))
    group = str(action.parameters.get("group", ""))

    request = IssueFromPrdRequest(
        repo_path=context.repo_path,
        prd_path=prd_path,
        issue_type=issue_type,
        queue_ready=ready,
        issue_agent=agent,
        labels_config=context.config.labels,
        group=group,
    )
    issue_url = create_issue_from_prd(
        request=request,
        github_client=github_client,
        process_runner=process_runner,
        content_generator=content_generator,
    )
    return {
        "action_id": action.action_id,
        "status": "completed",
        "output": f"Created issue: {issue_url}",
    }


def _execute_mark_issue_ready(
    action: DecisionAction,
    context: RepositoryRunContext,
    github_client: IGitHubClient,
) -> dict[str, str]:
    issue_number = int(action.parameters.get("issue_number", 0))
    github_client.edit_issue_labels(
        issue_number,
        add=[context.config.labels.ready],
    )
    return {
        "action_id": action.action_id,
        "status": "completed",
        "output": f"Marked issue #{issue_number} as ready",
    }


def _execute_run_once(
    action: DecisionAction,
    context: RepositoryRunContext,
    dry_run: bool,
    process_runner: IProcessRunner,
    github_client_factory: Callable[[Path], IGitHubClient],
    content_generator: IContentGenerator | None,
) -> dict[str, str]:
    agent = str(action.parameters.get("agent", "auto"))
    max_issues = int(action.parameters.get("max_issues", 1))

    exit_code = run_agent_repositories_once(
        contexts=[context],
        dry_run=dry_run,
        agent=agent,
        max_issues=max_issues,
        process_runner=process_runner,
        github_client_factory=github_client_factory,
        content_generator=content_generator,
    )
    status = "completed" if exit_code == 0 else "failed"
    return {
        "action_id": action.action_id,
        "status": status,
        "output": f"run_once exit_code={exit_code}",
    }


def _execute_review_once(
    action: DecisionAction,
    context: RepositoryRunContext,
    dry_run: bool,
    process_runner: IProcessRunner,
    github_client: IGitHubClient,
) -> dict[str, str]:
    agent = str(action.parameters.get("agent", "auto"))
    max_issues = int(action.parameters.get("max_issues", 1))

    exit_code = review_once(
        repo_path=context.repo_path,
        config=context.config,
        dry_run=dry_run,
        agent=agent,
        max_issues=max_issues,
        github_client=github_client,
        process_runner=process_runner,
    )
    status = "completed" if exit_code == 0 else "failed"
    return {
        "action_id": action.action_id,
        "status": status,
        "output": f"review_once exit_code={exit_code}",
    }


def _execute_run_deliberation(
    action: DecisionAction,
    context: RepositoryRunContext,
    config: InteractiveDecisionConfig,
    deliberation_deps: Mapping[str, Any] | None,
) -> dict[str, str]:
    from backend.core.shared.interfaces.agent_output_view import IAgentOutputView
    from backend.core.shared.models.agent_deliberation import DeliberationConfig
    from backend.core.shared.interfaces.agent_runner import IAgentTranscriptRunner

    prompt = str(action.parameters.get("prompt", context.config.prompts.default_phase))
    agents = str(action.parameters.get("agents", "architect,skeptic,implementer"))
    rounds = int(action.parameters.get("rounds", 2))
    synthesizer = str(action.parameters.get("synthesizer", "claude"))
    session_id = create_default_session_id()
    output_dir = Path(config.default_output_dir).parent / "deliberations" / session_id
    output_dir.mkdir(parents=True, exist_ok=True)

    request = DeliberationRequest(
        prompt=prompt,
        agents=tuple(a.strip() for a in agents.split(",") if a.strip()),
        rounds=rounds,
        synthesizer=synthesizer,
        output_dir=str(output_dir),
        session_id=session_id,
    )

    if deliberation_deps is None:
        return {
            "action_id": action.action_id,
            "status": "failed",
            "error": "Deliberation dependencies not provided.",
        }

    deliberation_config: DeliberationConfig = deliberation_deps["config"]
    transcript_runner: IAgentTranscriptRunner = deliberation_deps["transcript_runner"]
    event_sink: Callable[[DeliberationEvent], None] = deliberation_deps["event_sink"]
    output_view: IAgentOutputView = deliberation_deps["output_view"]

    result = run_agent_deliberation(
        request=request,
        config=deliberation_config,
        transcript_runner=transcript_runner,
        event_sink=event_sink,
        target_repo_path=context.repo_path,
        output_view=output_view,
    )
    return {
        "action_id": action.action_id,
        "status": "completed",
        "output": f"Deliberation complete: {result.output_dir}",
    }


def _execute_needs_clarification(action: DecisionAction) -> dict[str, str]:
    questions = action.parameters.get("questions", "No specific questions provided.")
    return {
        "action_id": action.action_id,
        "status": "completed",
        "output": f"Clarification needed: {questions}",
    }


def _execute_no_op(action: DecisionAction) -> dict[str, str]:
    reason = action.parameters.get("reason", "No action needed.")
    return {
        "action_id": action.action_id,
        "status": "completed",
        "output": f"No-op: {reason}",
    }


def execute_decision_plan(
    plan: DecisionPlan,
    context: RepositoryRunContext,
    decision_context: DecisionContext,
    config: InteractiveDecisionConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None,
    github_client_factory: Callable[[Path], IGitHubClient],
    auto_confirm: bool,
    deliberation_deps: Mapping[str, Any] | None = None,
) -> DecisionExecutionResult:
    """Execute a validated decision plan with optional confirmation."""
    action_results: list[dict[str, str]] = []
    has_failures = False

    for action in plan.actions:
        if action.confirmation_required and not auto_confirm:
            if not _confirm_action_interactive(action, plan.decision_id):
                action_results.append(
                    {
                        "action_id": action.action_id,
                        "status": "skipped",
                        "output": "User did not confirm",
                    }
                )
                has_failures = True
                continue

        try:
            if action.action_type == DecisionActionType.SHOW_STATUS:
                result = _execute_show_status(action, context, decision_context)
            elif action.action_type == DecisionActionType.CREATE_ISSUE_FROM_PRD:
                result = _execute_create_issue_from_prd(
                    action, context, github_client, process_runner, content_generator
                )
            elif action.action_type == DecisionActionType.MARK_ISSUE_READY:
                result = _execute_mark_issue_ready(action, context, github_client)
            elif action.action_type in (
                DecisionActionType.RUN_ONCE,
                DecisionActionType.RUN_ONCE_DRY_RUN,
            ):
                result = _execute_run_once(
                    action,
                    context,
                    dry_run=action.action_type == DecisionActionType.RUN_ONCE_DRY_RUN,
                    process_runner=process_runner,
                    github_client_factory=github_client_factory,
                    content_generator=content_generator,
                )
            elif action.action_type in (
                DecisionActionType.REVIEW_ONCE,
                DecisionActionType.REVIEW_ONCE_DRY_RUN,
            ):
                result = _execute_review_once(
                    action,
                    context,
                    dry_run=action.action_type
                    == DecisionActionType.REVIEW_ONCE_DRY_RUN,
                    process_runner=process_runner,
                    github_client=github_client,
                )
            elif action.action_type == DecisionActionType.RUN_DELIBERATION:
                result = _execute_run_deliberation(
                    action, context, config, deliberation_deps
                )
            elif action.action_type == DecisionActionType.NEEDS_CLARIFICATION:
                result = _execute_needs_clarification(action)
            elif action.action_type == DecisionActionType.NO_OP:
                result = _execute_no_op(action)
            else:
                result = {
                    "action_id": action.action_id,
                    "status": "failed",
                    "error": f"Unhandled action type: {action.action_type.value}",
                }
        except Exception as exc:
            result = {
                "action_id": action.action_id,
                "status": "failed",
                "error": str(exc),
            }

        if result.get("status") != "completed":
            has_failures = True
        action_results.append(result)

    status = "completed" if not has_failures else "failed"
    return DecisionExecutionResult(
        decision_id=plan.decision_id,
        status=status,
        summary=f"Executed {len(plan.actions)} actions, "
        f"{sum(1 for r in action_results if r.get('status') == 'completed')} succeeded.",
        action_results=tuple(action_results),
    )


def run_interactive_decision(
    *,
    user_prompt: str,
    context: RepositoryRunContext,
    config: InteractiveDecisionConfig,
    agent: str,
    plan_only: bool,
    execute: bool,
    auto_confirm: bool,
    output_dir: Path | None,
    planner_runner: IContentGenerator,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None,
    github_client_factory: Callable[[Path], IGitHubClient],
    deliberation_deps: Mapping[str, Any] | None = None,
) -> int:
    """Run the full interactive decision flow.

    Returns:
        Exit code (0 on success, 1 on failure).
    """
    if not config.enabled:
        _logger.error("Interactive decision is disabled in configuration.")
        return 1

    if auto_confirm and not config.allow_execute_yes:
        _logger.error("Auto-confirm (--yes) is disabled in configuration.")
        return 1

    if output_dir is None:
        output_dir = Path(config.default_output_dir)

    # 1. Build context
    decision_context = build_decision_context(context, config, github_client)

    # 2. Build prompt and call planner
    planner_prompt = _build_planner_prompt(user_prompt, decision_context)
    planner_prompt = _truncate_prompt(planner_prompt, config.max_context_chars)
    _logger.info("Running planner agent (%s) for decision...", agent)
    planner_result = planner_runner.generate(
        agent_name=agent,
        prompt=planner_prompt,
        cwd=context.repo_path,
        timeout=config.planner_timeout_seconds,
    )
    if planner_result.return_code != 0:
        _logger.error(
            "Planner agent failed with exit code %d", planner_result.return_code
        )
        return 1

    # 3. Parse plan
    try:
        plan = parse_decision_plan(user_prompt, planner_result.stdout)
    except Exception as exc:
        _logger.error("Failed to parse planner output: %s", exc)
        return 1

    # 4. Validate plan
    try:
        validate_decision_plan(plan, context.repo_path, auto_confirm)
    except ValueError as exc:
        _logger.error("Decision plan validation failed: %s", exc)
        return 1

    # 5. Write audit
    write_decision_audit(plan, decision_context, output_dir)
    _logger.info("Decision plan written to %s", output_dir / plan.decision_id)

    # Print plan summary
    print(f"\nDecision: {plan.decision_id}")
    print(f"Intent: {plan.intent_summary}")
    print(f"Risk: {plan.risk_level.value}")
    print("\nRecommended actions:")
    for action in plan.actions:
        state_marker = (
            " (writes external state)" if action.writes_external_state else ""
        )
        print(f"  [{action.action_id}] {action.action_type.value}{state_marker}")
        print(f"       {action.rationale}")
    if plan.warnings:
        print("\nWarnings:")
        for warning in plan.warnings:
            print(f"  - {warning}")
    if plan_only or not execute:
        print("\nNo changes were made.")
        print("\nTo execute in this terminal:")
        print(f'  uv run iar ask "{user_prompt}" --execute')
        return 0

    # 6. Execute
    # TTY confirmation for non-yes mode
    if not auto_confirm and plan.requires_confirmation:
        if not sys.stdin.isatty():
            _logger.error("TTY required for confirmation but not available.")
            return 1

    execution_result = execute_decision_plan(
        plan=plan,
        context=context,
        decision_context=decision_context,
        config=config,
        github_client=github_client,
        process_runner=process_runner,
        content_generator=content_generator,
        github_client_factory=github_client_factory,
        auto_confirm=auto_confirm,
        deliberation_deps=deliberation_deps,
    )

    write_execution_audit(execution_result, output_dir)
    print(f"\nExecution: {execution_result.status}")
    print(f"Summary: {execution_result.summary}")
    print(f"Audit: {output_dir / plan.decision_id / 'execution.md'}")

    return 0 if execution_result.status == "completed" else 1
