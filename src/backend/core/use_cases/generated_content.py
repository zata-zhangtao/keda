"""Generated content for GitHub Issues and PRs."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import (
    GeneratedContentConfig,
    GeneratedContentTargetConfig,
    GeneratedIssueContent,
    GeneratedPrContent,
)

_logger = logging.getLogger(__name__)

_MAX_TITLE_LENGTH = 256


@dataclass(frozen=True)
class IssueContext:
    """Context variables for Issue generation."""

    issue_type: str
    title: str
    prd_title: str
    relative_prd_path: str
    acceptance_items: str
    prd_text: str
    prd_introduction: str
    prd_goals: str
    prd_requirement_shape: str
    prd_change_impact_tree: str


@dataclass(frozen=True)
class PrContext:
    """Context variables for PR generation."""

    issue_number: int
    issue_title: str
    issue_body: str
    branch: str
    base_branch: str
    commit_log: str
    commit_messages: str
    diff_stat: str
    git_diff_stat: str


def _extract_prd_section(prd_text: str, section_keywords: tuple[str, ...]) -> str:
    """Extract content from a PRD section by keyword."""
    lines = prd_text.splitlines()
    in_section = False
    section_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if in_section:
                break
            heading = line[3:].strip().lower()
            if any(keyword in heading for keyword in section_keywords):
                in_section = True
                continue
        if in_section:
            section_lines.append(line)
    return "\n".join(section_lines).strip()


def build_issue_context(
    *,
    issue_type: str,
    title: str,
    relative_prd_path: Path,
    prd_text: str,
    acceptance_items: list[str],
) -> IssueContext:
    """Build context variables for Issue content generation."""
    introduction = _extract_prd_section(prd_text, ("introduction", "intro", "概述"))
    goals = _extract_prd_section(prd_text, ("goal", "目标"))
    requirement_shape = _extract_prd_section(prd_text, ("requirement", "需求", "shape"))
    change_impact_tree = _extract_prd_section(
        prd_text, ("change impact", "impact tree", "变更影响")
    )
    prd_title_text = title
    for line in prd_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            prd_title_text = re.sub(r"^PRD[:：]\s*", "", stripped[2:]).strip()
            break
    return IssueContext(
        issue_type=issue_type,
        title=title,
        prd_title=prd_title_text,
        relative_prd_path=relative_prd_path.as_posix(),
        acceptance_items="\n".join(acceptance_items),
        prd_text=prd_text,
        prd_introduction=introduction,
        prd_goals=goals,
        prd_requirement_shape=requirement_shape,
        prd_change_impact_tree=change_impact_tree,
    )


def _render_template(template: str, context: IssueContext | PrContext) -> str:
    """Render a template string with context variables."""
    return template.format(**context.__dict__)


def _truncate_text(text: str, max_chars: int) -> str:
    """Truncate text to max_chars with an ellipsis."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _validate_issue_body(body: str, relative_prd_path: str) -> bool:
    """Validate that Issue body contains the required PRD path anchor."""
    if not body or not body.strip():
        return False
    anchor = f"- PRD path: `{relative_prd_path}`"
    return anchor in body


def _validate_pr_body(body: str, issue_number: int) -> bool:
    """Validate that PR body contains the required Closes anchor."""
    if not body or not body.strip():
        return False
    closes_pattern = rf"Closes\s*#\s*{issue_number}"
    return bool(re.search(closes_pattern, body))


def _run_content_generator(
    generator: IContentGenerator,
    agent_name: str,
    prompt: str,
    cwd: Path,
    timeout_seconds: int,
) -> str:
    """Run the content generator and return raw output text."""
    result = generator.generate(
        agent_name=agent_name,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout_seconds,
    )
    if result.return_code != 0:
        _logger.warning(
            "Content generator exited with code %d: %s",
            result.return_code,
            result.stderr,
        )
        return ""
    return result.stdout.strip()


def _parse_json_output(output_text: str) -> tuple[str, str]:
    """Parse JSON output for title and body."""
    text = output_text.strip()
    # Handle markdown code block wrapping (e.g. ```json\n{...}\n```)
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            title = str(data.get("title", "")).strip()
            body = str(data.get("body", "")).strip()
            return title, body
    except json.JSONDecodeError:
        pass
    return "", ""


def _parse_markdown_output(output_text: str) -> tuple[str, str]:
    """Parse markdown output, extracting title from first non-empty line."""
    lines = output_text.splitlines()
    title = ""
    body = output_text
    for line in lines:
        stripped = line.strip()
        if stripped:
            title = stripped.lstrip("# ").strip()
            body = output_text
            break
    return title, body


def generate_issue_content(
    *,
    config: GeneratedContentConfig,
    context: IssueContext,
    fallback_title: str,
    fallback_body: str,
    generator: IContentGenerator | None = None,
    cwd: Path | None = None,
) -> GeneratedIssueContent:
    """Generate Issue title and body with fallback."""
    target = config.issue_from_prd
    if not config.enabled or not target.enabled:
        return GeneratedIssueContent(
            title=fallback_title, body=fallback_body, source="fallback"
        )

    generated_title = ""
    generated_body = ""

    if target.mode == "template":
        if target.title_template:
            try:
                generated_title = _render_template(target.title_template, context)
            except (KeyError, ValueError):
                pass
        if target.body_template:
            try:
                generated_body = _render_template(target.body_template, context)
            except (KeyError, ValueError):
                pass
    elif target.mode == "agent" and generator is not None and cwd is not None:
        agent_name = target.agent if target.agent != "auto" else config.default_agent
        prompt = _render_template(target.prompt, context)
        prompt = _truncate_text(prompt, config.max_input_chars)
        output_text = _run_content_generator(
            generator, agent_name, prompt, cwd, target.timeout_seconds
        )
        if target.output == "json":
            generated_title, generated_body = _parse_json_output(output_text)
        else:
            generated_title, generated_body = _parse_markdown_output(output_text)

    if generated_title:
        generated_title = generated_title[:_MAX_TITLE_LENGTH]
    if generated_body:
        generated_body = generated_body[: config.max_input_chars]

    if (
        generated_title
        and generated_body
        and _validate_issue_body(generated_body, context.relative_prd_path)
    ):
        return GeneratedIssueContent(
            title=generated_title, body=generated_body, source=target.mode
        )

    return GeneratedIssueContent(
        title=fallback_title, body=fallback_body, source="fallback"
    )


def _get_commit_log(
    worktree_path: Path,
    base_branch: str,
    process_runner: IProcessRunner,
) -> str:
    """Get commit messages from base_branch to HEAD."""
    result = process_runner.run(
        ["git", "log", f"{base_branch}..HEAD", "--pretty=format:%s"],
        cwd=worktree_path,
        check=False,
    )
    return result.stdout.strip()


def _get_diff_stat(
    worktree_path: Path,
    base_branch: str,
    process_runner: IProcessRunner,
) -> str:
    """Get diff stat from base_branch to HEAD."""
    result = process_runner.run(
        ["git", "diff", "--stat", f"{base_branch}...HEAD"],
        cwd=worktree_path,
        check=False,
    )
    return result.stdout.strip()


def build_pr_context(
    *,
    issue: object,
    branch: str,
    base_branch: str,
    worktree_path: Path,
    process_runner: IProcessRunner,
    target_config: GeneratedContentTargetConfig,
) -> PrContext:
    """Build context variables for PR content generation."""
    commit_log = ""
    diff_stat = ""
    if target_config.include_commit_log:
        commit_log = _get_commit_log(worktree_path, base_branch, process_runner)
    if target_config.include_diff_stat:
        diff_stat = _get_diff_stat(worktree_path, base_branch, process_runner)
    return PrContext(
        issue_number=issue.number,
        issue_title=issue.title,
        issue_body=issue.body,
        branch=branch,
        base_branch=base_branch,
        commit_log=commit_log,
        commit_messages=commit_log,
        diff_stat=diff_stat,
        git_diff_stat=diff_stat,
    )


def generate_pr_content(
    *,
    config: GeneratedContentConfig,
    context: PrContext,
    fallback_title: str,
    fallback_body: str,
    generator: IContentGenerator | None = None,
    cwd: Path | None = None,
) -> GeneratedPrContent:
    """Generate PR title and body with fallback."""
    target = config.draft_pr
    if not config.enabled or not target.enabled:
        return GeneratedPrContent(
            title=fallback_title, body=fallback_body, source="fallback"
        )

    generated_title = ""
    generated_body = ""

    if target.mode == "template":
        if target.title_template:
            try:
                generated_title = _render_template(target.title_template, context)
            except (KeyError, ValueError):
                pass
        if target.body_template:
            try:
                generated_body = _render_template(target.body_template, context)
            except (KeyError, ValueError):
                pass
    elif target.mode == "agent" and generator is not None and cwd is not None:
        agent_name = target.agent if target.agent != "auto" else config.default_agent
        prompt = _render_template(target.prompt, context)
        prompt = _truncate_text(prompt, config.max_input_chars)
        output_text = _run_content_generator(
            generator, agent_name, prompt, cwd, target.timeout_seconds
        )
        if target.output == "json":
            generated_title, generated_body = _parse_json_output(output_text)
        else:
            generated_title, generated_body = _parse_markdown_output(output_text)

    if generated_title:
        generated_title = generated_title[:_MAX_TITLE_LENGTH]
    if generated_body:
        generated_body = generated_body[: config.max_input_chars]

    if generated_body and _validate_pr_body(generated_body, context.issue_number):
        return GeneratedPrContent(
            title=generated_title or fallback_title,
            body=generated_body,
            source=target.mode,
        )

    return GeneratedPrContent(
        title=fallback_title, body=fallback_body, source="fallback"
    )
