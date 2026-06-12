"""Agent Runner interactive decision domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping


class DecisionActionType(Enum):
    """Hard-coded whitelist of actions the planner may recommend."""

    SHOW_STATUS = "show_status"
    RUN_DELIBERATION = "run_deliberation"
    CREATE_ISSUE_FROM_PRD = "create_issue_from_prd"
    MARK_ISSUE_READY = "mark_issue_ready"
    RUN_ONCE_DRY_RUN = "run_once_dry_run"
    RUN_ONCE = "run_once"
    REVIEW_ONCE_DRY_RUN = "review_once_dry_run"
    REVIEW_ONCE = "review_once"
    NEEDS_CLARIFICATION = "needs_clarification"
    NO_OP = "no_op"


class DecisionRiskLevel(Enum):
    """Risk levels for decision plans and actions."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class DecisionAction:
    """A single recommended action within a decision plan."""

    action_id: str
    action_type: DecisionActionType
    title: str
    rationale: str
    parameters: Mapping[str, str | int | bool] = field(default_factory=dict)
    writes_external_state: bool = False
    confirmation_required: bool = False


@dataclass(frozen=True)
class DecisionPlan:
    """Structured plan output from the planner agent."""

    decision_id: str
    user_prompt: str
    intent_summary: str
    risk_level: DecisionRiskLevel
    actions: tuple[DecisionAction, ...]
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    requires_confirmation: bool = False


@dataclass(frozen=True)
class DecisionContext:
    """Context collected for the planner agent."""

    repo_id: str
    repo_path: Path
    display_name: str
    config_summary: str
    pending_prd_summary: str
    issue_summary: str
    allowed_actions_summary: str
    forbidden_actions_summary: str


@dataclass(frozen=True)
class DecisionExecutionResult:
    """Result of executing a decision plan."""

    decision_id: str
    status: str
    summary: str
    action_results: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class InteractiveDecisionConfig:
    """Core configuration for interactive decision feature."""

    enabled: bool = True
    default_agent: str = "codex"
    default_output_dir: str = "logs/agent-runner/decisions"
    planner_timeout_seconds: int = 120
    max_context_chars: int = 24000
    allow_execute_yes: bool = True
