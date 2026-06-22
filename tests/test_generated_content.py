"""Tests for generated content use case."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    CommandResult,
    GeneratedContentConfig,
    GeneratedContentTargetConfig,
)
from backend.core.use_cases.generated_content import (
    IssueContext,
    PrContext,
    PrdContext,
    _parse_json_output,
    _parse_markdown_output,
    _validate_issue_body,
    _validate_pr_body,
    build_issue_context,
    extract_first_h2_section,
    generate_issue_content,
    generate_pr_content,
    generate_prd_content,
    load_prd_skill_spec,
    resolve_prd_skill_path,
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


def test_extract_first_h2_section_extracts_first_section() -> None:
    """extract_first_h2_section should return content under the first ## heading."""
    prd_text = """# PRD: Example

## 1. Background and Goals

First section content.

## 2. Requirements

Second section content.
"""
    assert extract_first_h2_section(prd_text) == "First section content."


def test_extract_first_h2_section_returns_empty_when_no_h2() -> None:
    """extract_first_h2_section should return empty string when there is no ## heading."""
    assert extract_first_h2_section("# Only H1\n\nSome text.") == ""


def test_build_issue_context_falls_back_to_first_h2_section() -> None:
    """When no known introduction keyword matches, use the first ## section as introduction."""
    prd_text = """# PRD: Example

## 1. 背景与目标

This should be used as introduction.

## 2. 需求形态

Requirements here.
"""
    context = build_issue_context(
        issue_type="feature",
        title="[Feature] Example",
        relative_prd_path=Path("tasks/example.md"),
        prd_text=prd_text,
        acceptance_items=[],
    )
    assert context.prd_introduction == "This should be used as introduction."
    assert "Requirements here." in context.prd_requirement_shape


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


def test_build_prd_context_collects_issue_and_comments() -> None:
    """PRD context should include issue fields and formatted comments."""
    from backend.core.use_cases.generated_content import build_prd_context
    from backend.core.shared.models.agent_runner import IssueSummary

    issue = IssueSummary(
        number=42, title="Test", url="https://example.com", body="Body", labels=()
    )
    context = build_prd_context(
        issue=issue,
        comments=["first", "second"],
        existing_prd_text="old prd",
        repo_path=Path("."),
    )
    assert context.issue_number == 42
    assert context.issue_title == "Test"
    assert context.issue_body == "Body"
    assert "Comment:\nfirst" in context.issue_comments
    assert "Comment:\nsecond" in context.issue_comments
    assert context.existing_prd_text == "old prd"


def test_generate_prd_content_disabled_uses_fallback() -> None:
    """When generated content is disabled, fallback PRD should be returned."""
    from backend.core.use_cases.generated_content import (
        PrdContext,
        generate_prd_content,
    )

    config = GeneratedContentConfig(enabled=False)
    context = PrdContext(
        issue_number=1,
        issue_title="T",
        issue_body="B",
        issue_comments="",
        existing_prd_text="",
        repo_structure_summary="",
    )
    result = generate_prd_content(
        config=config,
        context=context,
        fallback_prd_text="fallback",
    )
    assert result.text == "fallback"
    assert result.source == "fallback"


def test_generate_prd_content_template_mode() -> None:
    """Template mode should render body_template as PRD."""
    from backend.core.use_cases.generated_content import (
        PrdContext,
        generate_prd_content,
    )

    config = GeneratedContentConfig(
        enabled=True,
        prd_from_issue=GeneratedContentTargetConfig(
            enabled=True,
            mode="template",
            body_template=(
                "# PRD: {issue_title}\n\n"
                "- GitHub Issue: #{issue_number}\n\n"
                "## Acceptance Checklist\n\n"
                "- [ ] item"
            ),
        ),
    )
    context = PrdContext(
        issue_number=3,
        issue_title="Feature",
        issue_body="",
        issue_comments="",
        existing_prd_text="",
        repo_structure_summary="",
    )
    result = generate_prd_content(
        config=config,
        context=context,
        fallback_prd_text="fallback",
    )
    assert result.text == (
        "# PRD: Feature\n\n"
        "- GitHub Issue: #3\n\n"
        "## Acceptance Checklist\n\n"
        "- [ ] item"
    )
    assert result.source == "template"


def test_generate_prd_content_invalid_output_fallback() -> None:
    """Template output missing required PRD structure should fallback."""
    from backend.core.use_cases.generated_content import (
        PrdContext,
        generate_prd_content,
    )

    config = GeneratedContentConfig(
        enabled=True,
        prd_from_issue=GeneratedContentTargetConfig(
            enabled=True,
            mode="template",
            body_template="No structure here.",
        ),
    )
    context = PrdContext(
        issue_number=1,
        issue_title="T",
        issue_body="",
        issue_comments="",
        existing_prd_text="",
        repo_structure_summary="",
    )
    result = generate_prd_content(
        config=config,
        context=context,
        fallback_prd_text="fallback",
    )
    assert result.text == "fallback"
    assert result.source == "fallback"


def test_generate_prd_content_agent_mode() -> None:
    """Agent mode should use generator output when valid."""
    from backend.core.use_cases.generated_content import (
        PrdContext,
        generate_prd_content,
    )

    generator = FakeContentGenerator(
        response=(
            "# PRD: AI Title\n\n"
            "- GitHub Issue: #1\n\n"
            "## Acceptance Checklist\n\n"
            "- [ ] AI item\n"
        )
    )
    config = GeneratedContentConfig(
        enabled=True,
        prd_from_issue=GeneratedContentTargetConfig(
            enabled=True,
            mode="agent",
            output="markdown",
            prompt="Generate PRD for {issue_title}",
        ),
    )
    context = PrdContext(
        issue_number=1,
        issue_title="Title",
        issue_body="Body",
        issue_comments="",
        existing_prd_text="",
        repo_structure_summary="",
    )
    result = generate_prd_content(
        config=config,
        context=context,
        fallback_prd_text="fallback",
        generator=generator,
        cwd=Path("."),
    )
    assert "# PRD: AI Title" in result.text
    assert result.source == "agent"


def test_generate_prd_content_agent_invalid_uses_fallback() -> None:
    """Invalid agent output should fall back."""
    from backend.core.use_cases.generated_content import (
        PrdContext,
        generate_prd_content,
    )

    generator = FakeContentGenerator(response="not a prd")
    config = GeneratedContentConfig(
        enabled=True,
        prd_from_issue=GeneratedContentTargetConfig(
            enabled=True,
            mode="agent",
            output="markdown",
            prompt="Generate",
        ),
    )
    context = PrdContext(
        issue_number=1,
        issue_title="Title",
        issue_body="Body",
        issue_comments="",
        existing_prd_text="",
        repo_structure_summary="",
    )
    result = generate_prd_content(
        config=config,
        context=context,
        fallback_prd_text="fallback",
        generator=generator,
        cwd=Path("."),
    )
    assert result.text == "fallback"
    assert result.source == "fallback"


_VALID_AGENT_PRD = "# PRD: Generated\n\n- GitHub Issue: #1\n\n## 1. Goals\n\nbody\n"


def _prd_context() -> PrdContext:
    return PrdContext(
        issue_number=1,
        issue_title="Generated",
        issue_body="Body",
        issue_comments="",
        existing_prd_text="",
        repo_structure_summary="src/",
    )


def _agent_prd_config() -> GeneratedContentConfig:
    return GeneratedContentConfig(
        enabled=True,
        prd_from_issue=GeneratedContentTargetConfig(
            enabled=True,
            mode="agent",
            output="markdown",
            agent="claude",
            prompt="You are a technical product manager. PRD for {issue_title}",
        ),
    )


def test_generate_prd_content_agent_prompt_uses_skill_spec(tmp_path: Path) -> None:
    """Agent prompt should be built from the prd skill spec (single source)."""
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(
        "# PRD Generator (Architecture-First)\n\n## Output Contract\n"
        "Follow the required PRD structure.\n",
        encoding="utf-8",
    )
    generator = FakeContentGenerator(response=_VALID_AGENT_PRD)
    result = generate_prd_content(
        config=_agent_prd_config(),
        context=_prd_context(),
        fallback_prd_text="fallback",
        generator=generator,
        cwd=tmp_path,
        prd_skill_path=skill_file,
    )
    assert result.source == "agent"
    assert result.text == _VALID_AGENT_PRD.strip()
    # The captured prompt embeds the skill spec, not the hardcoded template.
    sent_prompt = generator.prompts[0]
    assert "PRD Generator (Architecture-First)" in sent_prompt
    assert "Output Contract" in sent_prompt
    assert "technical product manager" not in sent_prompt
    # And it still carries the PRD input context.
    assert "GitHub Issue #1: Generated" in sent_prompt


def test_generate_prd_content_agent_prompt_falls_back_when_skill_missing(
    tmp_path: Path,
) -> None:
    """When the skill is unreachable, the agent prompt falls back to target.prompt."""
    missing_skill = tmp_path / "nope" / "SKILL.md"
    generator = FakeContentGenerator(response=_VALID_AGENT_PRD)
    result = generate_prd_content(
        config=_agent_prd_config(),
        context=_prd_context(),
        fallback_prd_text="fallback",
        generator=generator,
        cwd=tmp_path,
        prd_skill_path=missing_skill,
    )
    assert result.source == "agent"
    sent_prompt = generator.prompts[0]
    assert "technical product manager" in sent_prompt
    assert "PRD Generator (Architecture-First)" not in sent_prompt


def test_load_prd_skill_spec_reads_and_handles_missing(tmp_path: Path) -> None:
    """load_prd_skill_spec reads an explicit path and returns None when unreachable."""
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("spec body", encoding="utf-8")
    assert load_prd_skill_spec(skill_file) == "spec body"
    assert load_prd_skill_spec(tmp_path / "missing.md") is None


def test_resolve_prd_skill_path_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit path wins, then IAR_PRD_SKILL_PATH env, then the ~/.claude default."""
    explicit = tmp_path / "explicit.md"
    assert resolve_prd_skill_path(explicit) == explicit
    monkeypatch.setenv("IAR_PRD_SKILL_PATH", str(tmp_path / "env.md"))
    assert resolve_prd_skill_path() == tmp_path / "env.md"
    monkeypatch.delenv("IAR_PRD_SKILL_PATH", raising=False)
    assert (
        resolve_prd_skill_path()
        == Path.home() / ".claude" / "skills" / "prd" / "SKILL.md"
    )


def test_generate_prd_content_agent_fallback_to_template() -> None:
    """Agent failure with fallback=template should render template before hard fallback."""
    from backend.core.use_cases.generated_content import (
        PrdContext,
        generate_prd_content,
    )

    generator = FakeContentGenerator(response="invalid")
    config = GeneratedContentConfig(
        enabled=True,
        fallback="template",
        prd_from_issue=GeneratedContentTargetConfig(
            enabled=True,
            mode="agent",
            output="markdown",
            body_template=(
                "# PRD: {issue_title}\n\n"
                "- GitHub Issue: #{issue_number}\n\n"
                "## Acceptance Checklist\n\n"
                "- [ ] template item"
            ),
            prompt="Generate",
        ),
    )
    context = PrdContext(
        issue_number=1,
        issue_title="Title",
        issue_body="Body",
        issue_comments="",
        existing_prd_text="",
        repo_structure_summary="",
    )
    result = generate_prd_content(
        config=config,
        context=context,
        fallback_prd_text="fallback",
        generator=generator,
        cwd=Path("."),
    )
    assert "- [ ] template item" in result.text
    assert result.source == "template"


def test_resolve_generation_agent_auto_resolves_to_claude() -> None:
    """auto/auto 收敛到 claude；显式 target / 默认值按优先级生效。"""
    from backend.core.use_cases.generated_content import _resolve_generation_agent

    assert _resolve_generation_agent("auto", "auto") == "claude"
    assert _resolve_generation_agent("auto", "codex") == "codex"
    assert _resolve_generation_agent("kimi", "auto") == "kimi"
    assert _resolve_generation_agent("claude", "codex") == "claude"
