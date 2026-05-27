"""Tests for generated content use case."""

from __future__ import annotations

from pathlib import Path


from backend.core.shared.models.agent_runner import (
    CommandResult,
    GeneratedContentConfig,
    GeneratedContentTargetConfig,
)
from backend.core.use_cases.generated_content import (
    IssueContext,
    PrContext,
    _parse_json_output,
    _parse_markdown_output,
    _validate_issue_body,
    _validate_pr_body,
    build_issue_context,
    generate_issue_content,
    generate_pr_content,
)
from tests.conftest import FakeContentGenerator, FakeProcessRunner


def test_validate_issue_body_passes_with_anchor() -> None:
    """Issue body with PRD path anchor should be valid."""
    body = "Some text\n\n- PRD path: `tasks/pending/example.md`\n"
    assert _validate_issue_body(body, "tasks/pending/example.md") is True


def test_validate_issue_body_fails_without_anchor() -> None:
    """Issue body missing PRD path anchor should be invalid."""
    assert _validate_issue_body("No anchor here.", "tasks/pending/example.md") is False


def test_validate_issue_body_fails_empty() -> None:
    """Empty Issue body should be invalid."""
    assert _validate_issue_body("", "tasks/pending/example.md") is False


def test_validate_pr_body_passes_with_closes() -> None:
    """PR body with Closes anchor should be valid."""
    assert _validate_pr_body("Closes #42\n\nSome description.", 42) is True


def test_validate_pr_body_fails_without_closes() -> None:
    """PR body missing Closes anchor should be invalid."""
    assert _validate_pr_body("No anchor here.", 42) is False


def test_validate_pr_body_fails_empty() -> None:
    """Empty PR body should be invalid."""
    assert _validate_pr_body("", 42) is False


def test_parse_json_output_extracts_title_and_body() -> None:
    """JSON output with title and body keys should be parsed."""
    title, body = _parse_json_output('{"title": "T", "body": "B"}')
    assert title == "T"
    assert body == "B"


def test_parse_json_output_returns_empty_for_invalid() -> None:
    """Invalid JSON should return empty strings."""
    title, body = _parse_json_output("not json")
    assert title == ""
    assert body == ""


def test_parse_markdown_output_extracts_title() -> None:
    """First non-empty line should become title."""
    title, body = _parse_markdown_output("# Title\n\nBody text.")
    assert title == "Title"
    assert body == "# Title\n\nBody text."


def test_generate_issue_content_disabled_uses_fallback() -> None:
    """When generated content is disabled, fallback should be returned."""
    config = GeneratedContentConfig(enabled=False)
    context = IssueContext(
        issue_type="feature",
        title="Title",
        prd_title="PRD Title",
        relative_prd_path="tasks/example.md",
        acceptance_items="- [ ] Item",
        prd_text="",
        prd_introduction="",
        prd_goals="",
        prd_requirement_shape="",
        prd_change_impact_tree="",
    )
    result = generate_issue_content(
        config=config,
        context=context,
        fallback_title="Fallback Title",
        fallback_body="Fallback Body",
    )
    assert result.title == "Fallback Title"
    assert result.body == "Fallback Body"
    assert result.source == "fallback"


def test_generate_issue_content_template_mode() -> None:
    """Template mode should render configured templates."""
    config = GeneratedContentConfig(
        enabled=True,
        issue_from_prd=GeneratedContentTargetConfig(
            enabled=True,
            mode="template",
            title_template="[{issue_type}] {prd_title}",
            body_template="## Summary\n\n{prd_introduction}\n\n- PRD path: `{relative_prd_path}`",
        ),
    )
    context = IssueContext(
        issue_type="feature",
        title="Title",
        prd_title="PRD Title",
        relative_prd_path="tasks/example.md",
        acceptance_items="- [ ] Item",
        prd_text="",
        prd_introduction="Intro text",
        prd_goals="",
        prd_requirement_shape="",
        prd_change_impact_tree="",
    )
    result = generate_issue_content(
        config=config,
        context=context,
        fallback_title="Fallback",
        fallback_body="Fallback Body",
    )
    assert result.title == "[feature] PRD Title"
    assert "- PRD path: `tasks/example.md`" in result.body
    assert result.source == "template"


def test_generate_issue_content_template_missing_anchor_fallback() -> None:
    """Template output missing PRD path anchor should fallback."""
    config = GeneratedContentConfig(
        enabled=True,
        issue_from_prd=GeneratedContentTargetConfig(
            enabled=True,
            mode="template",
            title_template="Title",
            body_template="No anchor here.",
        ),
    )
    context = IssueContext(
        issue_type="feature",
        title="Title",
        prd_title="PRD Title",
        relative_prd_path="tasks/example.md",
        acceptance_items="",
        prd_text="",
        prd_introduction="",
        prd_goals="",
        prd_requirement_shape="",
        prd_change_impact_tree="",
    )
    result = generate_issue_content(
        config=config,
        context=context,
        fallback_title="Fallback Title",
        fallback_body="Fallback Body",
    )
    assert result.title == "Fallback Title"
    assert result.body == "Fallback Body"
    assert result.source == "fallback"


def test_generate_issue_content_agent_mode_json() -> None:
    """Agent mode with JSON output should parse and validate."""
    generator = FakeContentGenerator(
        response='{"title": "AI Title", "body": "- PRD path: `tasks/example.md`\\n\\nDetails."}'
    )
    config = GeneratedContentConfig(
        enabled=True,
        issue_from_prd=GeneratedContentTargetConfig(
            enabled=True,
            mode="agent",
            output="json",
            prompt="Generate Issue for {relative_prd_path}",
        ),
    )
    context = IssueContext(
        issue_type="feature",
        title="Title",
        prd_title="PRD Title",
        relative_prd_path="tasks/example.md",
        acceptance_items="",
        prd_text="",
        prd_introduction="",
        prd_goals="",
        prd_requirement_shape="",
        prd_change_impact_tree="",
    )
    result = generate_issue_content(
        config=config,
        context=context,
        fallback_title="Fallback",
        fallback_body="Fallback Body",
        generator=generator,
        cwd=Path("."),
    )
    assert result.title == "AI Title"
    assert "- PRD path: `tasks/example.md`" in result.body
    assert result.source == "agent"


def test_generate_issue_content_agent_invalid_json_fallback() -> None:
    """Agent mode with invalid JSON should fallback."""
    generator = FakeContentGenerator(response="not json")
    config = GeneratedContentConfig(
        enabled=True,
        issue_from_prd=GeneratedContentTargetConfig(
            enabled=True,
            mode="agent",
            output="json",
            prompt="Generate Issue",
        ),
    )
    context = IssueContext(
        issue_type="feature",
        title="Title",
        prd_title="PRD Title",
        relative_prd_path="tasks/example.md",
        acceptance_items="",
        prd_text="",
        prd_introduction="",
        prd_goals="",
        prd_requirement_shape="",
        prd_change_impact_tree="",
    )
    result = generate_issue_content(
        config=config,
        context=context,
        fallback_title="Fallback",
        fallback_body="Fallback Body",
        generator=generator,
        cwd=Path("."),
    )
    assert result.title == "Fallback"
    assert result.body == "Fallback Body"
    assert result.source == "fallback"


def test_generate_issue_content_agent_fallback_to_template() -> None:
    """Agent failure with fallback=template should render templates before hard fallback."""
    generator = FakeContentGenerator(response="not json")
    config = GeneratedContentConfig(
        enabled=True,
        fallback="template",
        issue_from_prd=GeneratedContentTargetConfig(
            enabled=True,
            mode="agent",
            output="json",
            title_template="[Template] {prd_title}",
            body_template="- PRD path: `{relative_prd_path}`\n\nTemplate body.",
            prompt="Generate Issue",
        ),
    )
    context = IssueContext(
        issue_type="feature",
        title="Title",
        prd_title="PRD Title",
        relative_prd_path="tasks/example.md",
        acceptance_items="",
        prd_text="",
        prd_introduction="",
        prd_goals="",
        prd_requirement_shape="",
        prd_change_impact_tree="",
    )
    result = generate_issue_content(
        config=config,
        context=context,
        fallback_title="Fallback",
        fallback_body="Fallback Body",
        generator=generator,
        cwd=Path("."),
    )
    assert result.title == "[Template] PRD Title"
    assert "- PRD path: `tasks/example.md`" in result.body
    assert "Template body." in result.body
    assert result.source == "template"


def test_generate_pr_content_disabled_uses_fallback() -> None:
    """When PR generation is disabled, fallback should be returned."""
    config = GeneratedContentConfig(enabled=False)
    context = PrContext(
        issue_number=42,
        issue_title="Title",
        issue_body="Body",
        branch="issue-42",
        base_branch="main",
        commit_log="commit 1",
        commit_messages="commit 1",
        diff_stat="1 file changed",
        git_diff_stat="1 file changed",
    )
    result = generate_pr_content(
        config=config,
        context=context,
        fallback_title="Fallback Title",
        fallback_body="Fallback Body",
    )
    assert result.title == "Fallback Title"
    assert result.body == "Fallback Body"
    assert result.source == "fallback"


def test_generate_pr_content_template_mode() -> None:
    """Template mode should render PR templates."""
    config = GeneratedContentConfig(
        enabled=True,
        draft_pr=GeneratedContentTargetConfig(
            enabled=True,
            mode="template",
            title_template="[Agent] {issue_title}",
            body_template="Closes #{issue_number}\n\n{diff_stat}",
        ),
    )
    context = PrContext(
        issue_number=42,
        issue_title="Title",
        issue_body="Body",
        branch="issue-42",
        base_branch="main",
        commit_log="commit 1",
        commit_messages="commit 1",
        diff_stat="1 file changed",
        git_diff_stat="1 file changed",
    )
    result = generate_pr_content(
        config=config,
        context=context,
        fallback_title="Fallback",
        fallback_body="Fallback Body",
    )
    assert result.title == "[Agent] Title"
    assert "Closes #42" in result.body
    assert result.source == "template"


def test_generate_pr_content_missing_closes_fallback() -> None:
    """PR body missing Closes anchor should fallback."""
    config = GeneratedContentConfig(
        enabled=True,
        draft_pr=GeneratedContentTargetConfig(
            enabled=True,
            mode="template",
            title_template="Title",
            body_template="No closes here.",
        ),
    )
    context = PrContext(
        issue_number=42,
        issue_title="Title",
        issue_body="Body",
        branch="issue-42",
        base_branch="main",
        commit_log="",
        commit_messages="",
        diff_stat="",
        git_diff_stat="",
    )
    result = generate_pr_content(
        config=config,
        context=context,
        fallback_title="Fallback Title",
        fallback_body="Fallback Body",
    )
    assert result.title == "Fallback Title"
    assert result.body == "Fallback Body"
    assert result.source == "fallback"


def test_generate_pr_content_agent_mode_markdown() -> None:
    """Agent mode with markdown output should parse and validate."""
    generator = FakeContentGenerator(
        response="Closes #42\n\n## Summary\n\nThis PR implements the feature."
    )
    config = GeneratedContentConfig(
        enabled=True,
        draft_pr=GeneratedContentTargetConfig(
            enabled=True,
            mode="agent",
            output="markdown",
            prompt="Generate PR for #{issue_number}",
        ),
    )
    context = PrContext(
        issue_number=42,
        issue_title="Title",
        issue_body="Body",
        branch="issue-42",
        base_branch="main",
        commit_log="",
        commit_messages="",
        diff_stat="",
        git_diff_stat="",
    )
    result = generate_pr_content(
        config=config,
        context=context,
        fallback_title="Fallback",
        fallback_body="Fallback Body",
        generator=generator,
        cwd=Path("."),
    )
    assert result.title == "Closes #42"
    assert "Closes #42" in result.body
    assert result.source == "agent"


def test_generate_pr_content_agent_fallback_to_template() -> None:
    """Agent failure with fallback=template should render PR templates before hard fallback."""
    generator = FakeContentGenerator(response="invalid markdown")
    config = GeneratedContentConfig(
        enabled=True,
        fallback="template",
        draft_pr=GeneratedContentTargetConfig(
            enabled=True,
            mode="agent",
            output="markdown",
            title_template="[Template] {issue_title}",
            body_template="Closes #{issue_number}\n\nTemplate PR body.",
            prompt="Generate PR",
        ),
    )
    context = PrContext(
        issue_number=42,
        issue_title="Title",
        issue_body="Body",
        branch="issue-42",
        base_branch="main",
        commit_log="",
        commit_messages="",
        diff_stat="",
        git_diff_stat="",
    )
    result = generate_pr_content(
        config=config,
        context=context,
        fallback_title="Fallback",
        fallback_body="Fallback Body",
        generator=generator,
        cwd=Path("."),
    )
    assert result.title == "[Template] Title"
    assert "Closes #42" in result.body
    assert "Template PR body." in result.body
    assert result.source == "template"


def test_build_issue_context_extracts_sections() -> None:
    """Issue context should extract PRD sections correctly."""
    prd_text = """# PRD: Example

## Introduction

This is the intro.

## Goals

- Goal 1
- Goal 2

## Acceptance Checklist

- [x] done
"""
    context = build_issue_context(
        issue_type="feature",
        title="[Feature] Example",
        relative_prd_path=Path("tasks/example.md"),
        prd_text=prd_text,
        acceptance_items=["- [x] done"],
    )
    assert context.issue_type == "feature"
    assert context.prd_title == "Example"
    assert context.relative_prd_path == "tasks/example.md"
    assert context.prd_introduction == "This is the intro."
    assert "Goal 1" in context.prd_goals


def test_build_pr_context_collects_git_info() -> None:
    """PR context should collect commit log and diff stat."""
    worktree_path = Path("/tmp/fake-worktree")
    process_runner = FakeProcessRunner(
        responses={
            ("git", "log", "main..HEAD", "--pretty=format:%s"): CommandResult(
                command=("git", "log", "main..HEAD", "--pretty=format:%s"),
                return_code=0,
                stdout="commit one\ncommit two",
                stderr="",
            ),
            ("git", "diff", "--stat", "main...HEAD"): CommandResult(
                command=("git", "diff", "--stat", "main...HEAD"),
                return_code=0,
                stdout="1 file changed, 10 insertions",
                stderr="",
            ),
        }
    )
    from backend.core.shared.models.agent_runner import IssueSummary

    issue = IssueSummary(
        number=42, title="Test", url="https://example.com", body="Body", labels=()
    )
    from backend.core.use_cases.generated_content import build_pr_context

    target_config = GeneratedContentTargetConfig(
        include_commit_log=True, include_diff_stat=True
    )
    context = build_pr_context(
        issue=issue,
        branch="issue-42",
        base_branch="main",
        worktree_path=worktree_path,
        process_runner=process_runner,
        target_config=target_config,
    )
    assert context.issue_number == 42
    assert context.issue_title == "Test"
    assert context.commit_log == "commit one\ncommit two"
    assert context.commit_messages == "commit one\ncommit two"
    assert context.diff_stat == "1 file changed, 10 insertions"
    assert context.git_diff_stat == "1 file changed, 10 insertions"


def test_generate_issue_content_empty_title_fallback() -> None:
    """Empty generated title should trigger fallback."""
    config = GeneratedContentConfig(
        enabled=True,
        issue_from_prd=GeneratedContentTargetConfig(
            enabled=True,
            mode="template",
            title_template="",
            body_template="- PRD path: `{relative_prd_path}`",
        ),
    )
    context = IssueContext(
        issue_type="feature",
        title="Title",
        prd_title="PRD Title",
        relative_prd_path="tasks/example.md",
        acceptance_items="",
        prd_text="",
        prd_introduction="",
        prd_goals="",
        prd_requirement_shape="",
        prd_change_impact_tree="",
    )
    result = generate_issue_content(
        config=config,
        context=context,
        fallback_title="Fallback Title",
        fallback_body="Fallback Body",
    )
    assert result.title == "Fallback Title"
    assert result.body == "Fallback Body"
    assert result.source == "fallback"
