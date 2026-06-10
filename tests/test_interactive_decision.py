"""Tests for the interactive decision use case."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.shared.models.agent_decision import (
    DecisionAction,
    DecisionActionType,
    DecisionContext,
    DecisionPlan,
    DecisionRiskLevel,
    InteractiveDecisionConfig,
)
from backend.core.shared.models.agent_runner import AppConfig
from backend.core.use_cases.interactive_decision import (
    execute_decision_plan,
    parse_decision_plan,
    run_interactive_decision,
    validate_decision_plan,
    write_decision_audit,
    write_execution_audit,
)


@pytest.fixture
def mock_context(tmp_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.repo_id = "test-repo"
    ctx.display_name = "Test Repo"
    ctx.repo_path = tmp_path
    ctx.config = AppConfig()
    return ctx


@pytest.fixture
def mock_github_client() -> MagicMock:
    client = MagicMock()
    client.list_issues_by_label.return_value = []
    return client


@pytest.fixture
def mock_planner_runner() -> MagicMock:
    runner = MagicMock()
    return runner


@pytest.fixture
def mock_process_runner() -> MagicMock:
    runner = MagicMock()
    return runner


def test_parse_decision_plan_valid() -> None:
    """Should parse a valid JSON plan."""
    raw = json.dumps(
        {
            "decision_id": "dec-20260101-000000-0000",
            "user_prompt": "test",
            "intent_summary": "Do nothing",
            "risk_level": "low",
            "actions": [
                {
                    "action_id": "A1",
                    "action_type": "no_op",
                    "title": "No action",
                    "rationale": "Nothing to do",
                    "parameters": {},
                    "writes_external_state": False,
                    "confirmation_required": False,
                }
            ],
            "assumptions": [],
            "warnings": [],
            "requires_confirmation": False,
        }
    )
    plan = parse_decision_plan("test", raw)
    assert plan.decision_id == "dec-20260101-000000-0000"
    assert plan.risk_level == DecisionRiskLevel.LOW
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == DecisionActionType.NO_OP


def test_parse_decision_plan_with_markdown_fences() -> None:
    """Should strip markdown fences before parsing."""
    raw = (
        "```json\n"
        + json.dumps(
            {
                "decision_id": "dec-20260101-000000-0001",
                "user_prompt": "test",
                "intent_summary": "Do nothing",
                "risk_level": "low",
                "actions": [
                    {
                        "action_id": "A1",
                        "action_type": "no_op",
                        "title": "No action",
                        "rationale": "Nothing to do",
                        "parameters": {},
                        "writes_external_state": False,
                        "confirmation_required": False,
                    }
                ],
                "assumptions": [],
                "warnings": [],
                "requires_confirmation": False,
            }
        )
        + "\n```"
    )
    plan = parse_decision_plan("test", raw)
    assert plan.decision_id == "dec-20260101-000000-0001"


def test_parse_decision_plan_invalid_json() -> None:
    """Should raise on invalid JSON."""
    with pytest.raises(ValueError, match="No JSON object found"):
        parse_decision_plan("test", "not json")


def test_parse_decision_plan_unknown_action_type() -> None:
    """Should raise on unknown action type."""
    raw = json.dumps(
        {
            "decision_id": "dec-20260101-000000-0002",
            "user_prompt": "test",
            "intent_summary": "Bad action",
            "risk_level": "low",
            "actions": [
                {
                    "action_id": "A1",
                    "action_type": "git_push",
                    "title": "Push",
                    "rationale": "Bad",
                    "parameters": {},
                    "writes_external_state": True,
                    "confirmation_required": True,
                }
            ],
            "assumptions": [],
            "warnings": [],
            "requires_confirmation": False,
        }
    )
    with pytest.raises(ValueError, match="Unknown action_type"):
        parse_decision_plan("test", raw)


def test_validate_decision_plan_unknown_action() -> None:
    """Should reject plans with unknown action types."""
    plan = DecisionPlan(
        decision_id="dec-1",
        user_prompt="test",
        intent_summary="test",
        risk_level=DecisionRiskLevel.LOW,
        actions=(
            DecisionAction(
                action_id="A1",
                action_type=DecisionActionType.NO_OP,
                title="test",
                rationale="test",
            ),
        ),
    )
    validate_decision_plan(plan, Path("/tmp/repo"), auto_confirm=False)


def test_validate_decision_plan_high_risk_with_yes() -> None:
    """Should reject high-risk actions with --yes."""
    plan = DecisionPlan(
        decision_id="dec-1",
        user_prompt="test",
        intent_summary="test",
        risk_level=DecisionRiskLevel.HIGH,
        actions=(
            DecisionAction(
                action_id="A1",
                action_type=DecisionActionType.RUN_ONCE,
                title="Run once",
                rationale="test",
                writes_external_state=True,
                confirmation_required=True,
            ),
        ),
    )
    with pytest.raises(ValueError, match="high-risk and cannot use --yes"):
        validate_decision_plan(plan, Path("/tmp/repo"), auto_confirm=True)


def test_validate_decision_plan_prd_path_outside_repo() -> None:
    """Should reject PRD paths outside the repo."""
    plan = DecisionPlan(
        decision_id="dec-1",
        user_prompt="test",
        intent_summary="test",
        risk_level=DecisionRiskLevel.MEDIUM,
        actions=(
            DecisionAction(
                action_id="A1",
                action_type=DecisionActionType.CREATE_ISSUE_FROM_PRD,
                title="Create issue",
                rationale="test",
                parameters={"prd_path": "/etc/passwd"},
            ),
        ),
    )
    with pytest.raises(ValueError, match="PRD path outside repo"):
        validate_decision_plan(plan, Path("/tmp/repo"), auto_confirm=False)


def test_validate_decision_plan_prd_path_traversal_absolute() -> None:
    """Should reject absolute PRD paths with traversal components."""
    plan = DecisionPlan(
        decision_id="dec-1",
        user_prompt="test",
        intent_summary="test",
        risk_level=DecisionRiskLevel.MEDIUM,
        actions=(
            DecisionAction(
                action_id="A1",
                action_type=DecisionActionType.CREATE_ISSUE_FROM_PRD,
                title="Create issue",
                rationale="test",
                parameters={"prd_path": "/tmp/repo/../etc/passwd"},
            ),
        ),
    )
    with pytest.raises(ValueError, match="PRD path outside repo"):
        validate_decision_plan(plan, Path("/tmp/repo"), auto_confirm=False)


def test_validate_decision_plan_missing_prd_path() -> None:
    """Should reject create_issue_from_prd without prd_path."""
    plan = DecisionPlan(
        decision_id="dec-1",
        user_prompt="test",
        intent_summary="test",
        risk_level=DecisionRiskLevel.MEDIUM,
        actions=(
            DecisionAction(
                action_id="A1",
                action_type=DecisionActionType.CREATE_ISSUE_FROM_PRD,
                title="Create issue",
                rationale="test",
                parameters={},
            ),
        ),
    )
    with pytest.raises(ValueError, match="requires 'prd_path' parameter"):
        validate_decision_plan(plan, Path("/tmp/repo"), auto_confirm=False)


def test_validate_decision_plan_missing_issue_number() -> None:
    """Should reject mark_issue_ready without issue_number."""
    plan = DecisionPlan(
        decision_id="dec-1",
        user_prompt="test",
        intent_summary="test",
        risk_level=DecisionRiskLevel.MEDIUM,
        actions=(
            DecisionAction(
                action_id="A1",
                action_type=DecisionActionType.MARK_ISSUE_READY,
                title="Mark ready",
                rationale="test",
                parameters={},
            ),
        ),
    )
    with pytest.raises(ValueError, match="requires 'issue_number' parameter"):
        validate_decision_plan(plan, Path("/tmp/repo"), auto_confirm=False)


def test_validate_decision_plan_invalid_issue_number() -> None:
    """Should reject invalid issue numbers."""
    plan = DecisionPlan(
        decision_id="dec-1",
        user_prompt="test",
        intent_summary="test",
        risk_level=DecisionRiskLevel.MEDIUM,
        actions=(
            DecisionAction(
                action_id="A1",
                action_type=DecisionActionType.MARK_ISSUE_READY,
                title="Mark ready",
                rationale="test",
                parameters={"issue_number": -1},
            ),
        ),
    )
    with pytest.raises(ValueError, match="invalid issue_number"):
        validate_decision_plan(plan, Path("/tmp/repo"), auto_confirm=False)


def test_write_decision_audit_creates_files(
    tmp_path: Path,
) -> None:
    """Should write plan.json, plan.md, and context-summary.json."""
    plan = DecisionPlan(
        decision_id="dec-test",
        user_prompt="test",
        intent_summary="test",
        risk_level=DecisionRiskLevel.LOW,
        actions=(),
    )
    decision_context = DecisionContext(
        repo_id="repo",
        repo_path=tmp_path,
        display_name="Repo",
        config_summary="",
        pending_prd_summary="",
        issue_summary="",
        allowed_actions_summary="",
        forbidden_actions_summary="",
    )
    write_decision_audit(plan, decision_context, tmp_path)
    decision_dir = tmp_path / "dec-test"
    assert (decision_dir / "plan.json").exists()
    assert (decision_dir / "plan.md").exists()
    assert (decision_dir / "context-summary.json").exists()


def test_write_execution_audit_creates_files(
    tmp_path: Path,
) -> None:
    """Should write execution.json and execution.md."""
    result = execute_decision_plan(
        plan=DecisionPlan(
            decision_id="dec-test",
            user_prompt="test",
            intent_summary="test",
            risk_level=DecisionRiskLevel.LOW,
            actions=(
                DecisionAction(
                    action_id="A1",
                    action_type=DecisionActionType.NO_OP,
                    title="No op",
                    rationale="test",
                ),
            ),
        ),
        context=MagicMock(),
        decision_context=MagicMock(),
        config=InteractiveDecisionConfig(),
        github_client=MagicMock(),
        process_runner=MagicMock(),
        content_generator=None,
        github_client_factory=lambda path: MagicMock(),
        auto_confirm=False,
    )
    write_execution_audit(result, tmp_path)
    decision_dir = tmp_path / "dec-test"
    assert (decision_dir / "execution.json").exists()
    assert (decision_dir / "execution.md").exists()


def test_execute_decision_plan_skipped_action_counts_as_failure() -> None:
    """Skipped confirmation actions should result in failed status."""
    result = execute_decision_plan(
        plan=DecisionPlan(
            decision_id="dec-test",
            user_prompt="test",
            intent_summary="test",
            risk_level=DecisionRiskLevel.MEDIUM,
            actions=(
                DecisionAction(
                    action_id="A1",
                    action_type=DecisionActionType.CREATE_ISSUE_FROM_PRD,
                    title="Create issue",
                    rationale="test",
                    parameters={"prd_path": "tasks/pending/example.md"},
                    writes_external_state=True,
                    confirmation_required=True,
                ),
            ),
            requires_confirmation=True,
        ),
        context=MagicMock(),
        decision_context=MagicMock(),
        config=InteractiveDecisionConfig(),
        github_client=MagicMock(),
        process_runner=MagicMock(),
        content_generator=None,
        github_client_factory=lambda path: MagicMock(),
        auto_confirm=False,
    )
    assert result.status == "failed"
    assert result.action_results[0]["status"] == "skipped"


def test_run_interactive_decision_plan_only(
    tmp_path: Path,
    mock_context: MagicMock,
    mock_planner_runner: MagicMock,
    mock_github_client: MagicMock,
    mock_process_runner: MagicMock,
) -> None:
    """Plan-only mode should write audit and return 0."""
    mock_planner_runner.generate.return_value = MagicMock(
        return_code=0,
        stdout=json.dumps(
            {
                "decision_id": "dec-20260101-000000-0003",
                "user_prompt": "test",
                "intent_summary": "Do nothing",
                "risk_level": "low",
                "actions": [
                    {
                        "action_id": "A1",
                        "action_type": "no_op",
                        "title": "No action",
                        "rationale": "Nothing to do",
                        "parameters": {},
                        "writes_external_state": False,
                        "confirmation_required": False,
                    }
                ],
                "assumptions": [],
                "warnings": [],
                "requires_confirmation": False,
            }
        ),
    )
    exit_code = run_interactive_decision(
        user_prompt="test",
        context=mock_context,
        config=InteractiveDecisionConfig(default_output_dir=str(tmp_path)),
        agent="codex",
        plan_only=True,
        execute=False,
        auto_confirm=False,
        output_dir=tmp_path,
        planner_runner=mock_planner_runner,
        github_client=mock_github_client,
        process_runner=mock_process_runner,
        content_generator=None,
        github_client_factory=lambda path: MagicMock(),
    )
    assert exit_code == 0
    assert (tmp_path / "dec-20260101-000000-0003" / "plan.json").exists()


def test_run_interactive_decision_planner_failure(
    tmp_path: Path,
    mock_context: MagicMock,
    mock_planner_runner: MagicMock,
    mock_github_client: MagicMock,
    mock_process_runner: MagicMock,
) -> None:
    """Should return 1 when planner fails."""
    mock_planner_runner.generate.return_value = MagicMock(
        return_code=1,
        stdout="",
    )
    exit_code = run_interactive_decision(
        user_prompt="test",
        context=mock_context,
        config=InteractiveDecisionConfig(default_output_dir=str(tmp_path)),
        agent="codex",
        plan_only=True,
        execute=False,
        auto_confirm=False,
        output_dir=tmp_path,
        planner_runner=mock_planner_runner,
        github_client=mock_github_client,
        process_runner=mock_process_runner,
        content_generator=None,
        github_client_factory=lambda path: MagicMock(),
    )
    assert exit_code == 1


def test_run_interactive_decision_validation_failure(
    tmp_path: Path,
    mock_context: MagicMock,
    mock_planner_runner: MagicMock,
    mock_github_client: MagicMock,
    mock_process_runner: MagicMock,
) -> None:
    """Should return 1 when plan validation fails."""
    mock_planner_runner.generate.return_value = MagicMock(
        return_code=0,
        stdout=json.dumps(
            {
                "decision_id": "dec-20260101-000000-0004",
                "user_prompt": "test",
                "intent_summary": "Bad action",
                "risk_level": "high",
                "actions": [
                    {
                        "action_id": "A1",
                        "action_type": "run_once",
                        "title": "Run",
                        "rationale": "test",
                        "parameters": {},
                        "writes_external_state": True,
                        "confirmation_required": True,
                    }
                ],
                "assumptions": [],
                "warnings": [],
                "requires_confirmation": True,
            }
        ),
    )
    exit_code = run_interactive_decision(
        user_prompt="test",
        context=mock_context,
        config=InteractiveDecisionConfig(default_output_dir=str(tmp_path)),
        agent="codex",
        plan_only=False,
        execute=True,
        auto_confirm=True,
        output_dir=tmp_path,
        planner_runner=mock_planner_runner,
        github_client=mock_github_client,
        process_runner=mock_process_runner,
        content_generator=None,
        github_client_factory=lambda path: MagicMock(),
    )
    assert exit_code == 1
