"""Tests for agent prompt construction.

Covers ``build_prompt``, ``build_recovery_prompt``,
``build_progress_continuation_prompt``, PRD anchor extraction and the
inlined PRD context block."""

from __future__ import annotations

from pathlib import Path


from backend.core.shared.models.agent_runner import (
    IssueSummary,
    PromptConfig,
)
from backend.core.use_cases.run_agent_once import (
    build_recovery_prompt,
    build_prompt,
    extract_prd_path,
)
from backend.core.use_cases.agent_runner_feedback import (
    build_progress_continuation_prompt,
)
from backend.core.use_cases.agent_runner_feedback import (
    _build_prd_context_block,
    _DEFAULT_PRD_INLINE_MAX_CHARS,
)
from tests.support.agent_runner import (
    make_prd_issue,
)


def test_build_prompt_uses_commit_request_proxy() -> None:
    """Prompt should route commit intent through the runner proxy."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    prompt = build_prompt(issue, Path("/worktree"), PromptConfig())
    assert "Do not merge main, delete branches, push, or create PRs" in prompt
    assert "Do not run `git commit`, `git reset`, `git checkout`" in prompt
    assert "mutates the git index" in prompt
    assert ".agent-runner/commit-request.json" in prompt
    assert "commit_message" in prompt


def test_build_prompt_fallback_to_default() -> None:
    """Empty prompt config should fall back to the built-in default template."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    prompt = build_prompt(issue, Path("/worktree"), PromptConfig())
    assert "Complete GitHub Issue #1: Test" in prompt
    assert "Execution rules:" in prompt


def test_build_prompt_uses_config_template() -> None:
    """Custom phase template in PromptConfig should override the default."""
    issue = IssueSummary(
        number=42,
        title="Custom",
        url="https://github.com/example/repo/issues/42",
        body="Custom body",
        labels=(),
    )
    custom_template = "Issue #{issue_number}: {issue_title}\n{issue_body}"
    prompt_config = PromptConfig(phases={"execution": custom_template})
    prompt = build_prompt(issue, Path("/worktree"), prompt_config)
    assert prompt == "Issue #42: Custom\nCustom body"


def test_build_prompt_replaces_all_placeholders() -> None:
    """All template placeholders should be replaced with issue values."""
    issue = IssueSummary(
        number=7,
        title="Replace Test",
        url="https://github.com/example/repo/issues/7",
        body="PRD path: `docs/prd.md`",
        labels=(),
    )
    template = (
        "num={issue_number} title={issue_title} url={issue_url} "
        "path={worktree_path} body={issue_body} prd={prd_line}"
    )
    prompt_config = PromptConfig(phases={"execution": template})
    prompt = build_prompt(issue, Path("/wt"), prompt_config)
    assert "num=7" in prompt
    assert "title=Replace Test" in prompt
    assert "url=https://github.com/example/repo/issues/7" in prompt
    assert "path=/wt" in prompt
    assert "body=PRD path: `docs/prd.md`" in prompt
    assert "prd=Also read the canonical PRD at `docs/prd.md`" in prompt


def test_build_recovery_prompt_includes_failure_context() -> None:
    """Recovery prompt should give the agent enough detail to fix and retry."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    failure_summary = "\n".join(
        [
            "Verification after runner staged changes with git add -A failed.",
            "Command: `just test`",
            "stdout: failing stdout",
            "stderr: failing stderr",
        ]
    )

    prompt = build_recovery_prompt(
        issue,
        Path("/worktree"),
        recovery_attempt=1,
        max_recovery_attempts=2,
        failure_summary=failure_summary,
    )

    assert "Recovery attempt: 1/2" in prompt
    assert "Verification after runner staged changes with git add -A failed" in prompt
    assert "failing stdout" in prompt
    assert "failing stderr" in prompt
    assert "Do not run `git commit`, `git reset`, `git checkout`" in prompt
    assert "mutates the git index" in prompt
    assert ".agent-runner/commit-request.json" in prompt


def test_extract_prd_path_finds_backtick_path() -> None:
    """PRD path should be extracted from a line-start anchor."""
    body = "Some text\n- PRD path: `tasks/pending/example.md`\nMore text"
    assert extract_prd_path(body) == "tasks/pending/example.md"


def test_extract_prd_path_ignores_inline_mention() -> None:
    """Inline `PRD path:` in prose must not shadow the canonical anchor."""
    body = (
        "Add a core `create_prd_from_issue` workflow and an optional "
        "`PRD path:` anchor. The daemon detects `agent/rework-prd`.\n\n"
        "## Canonical PRD\n\n"
        "- PRD path: `tasks/pending/P2-FEAT-20260527-190923-prd-from-issue.md`\n"
    )
    assert extract_prd_path(body) == "tasks/pending/P2-FEAT-20260527-190923-prd-from-issue.md"


def test_extract_prd_path_returns_none_when_missing() -> None:
    """None should be returned when no PRD path is present."""
    assert extract_prd_path("No PRD here.") is None


def test_extract_prd_path_ignores_inline_code_anchor() -> None:
    """`PRD path:` inside prose must not be mistaken for the canonical anchor."""
    body = (
        "...an optional existing `PRD path:` anchor. The daemon or `run` path...\n"
        "\n"
        "## Canonical PRD\n"
        "- PRD path: `tasks/archive/P2-FEAT-20260527-190923-prd-from-issue.md`\n"
    )
    assert extract_prd_path(body) == "tasks/archive/P2-FEAT-20260527-190923-prd-from-issue.md"


def test_extract_prd_path_rejects_garbage_anchor() -> None:
    """Malformed anchors that do not look like relative paths must be ignored."""
    body = (
        "- PRD path: ` anchor. The daemon or `run` path`\n"
        "- PRD path: `tasks/pending/P2-FEAT-20260527-190923-prd-from-issue.md`\n"
    )
    assert extract_prd_path(body) == "tasks/pending/P2-FEAT-20260527-190923-prd-from-issue.md"


def test_build_prompt_separates_prd_change_log_from_checklist() -> None:
    """Prompt should distinguish PRD evolution from acceptance completion."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prompt = build_prompt(issue, Path("/worktree"), PromptConfig())
    assert "tasks/pending/example.md" in prompt
    assert "Acceptance Checklist" in prompt
    assert "Change Log" in prompt
    assert "Acceptance Checklist" in prompt
    # 归档规则必须双向表述：只说「别自己归档」会让下游 reviewer 把 runner 已完成的
    # 归档判成越权并回滚，随后撞上推送前的 archive 门禁（freshai Issue #99）。
    assert "never `git mv` it into" in prompt
    assert "never move it back to `tasks/pending/`" in prompt


def test_build_prompt_no_prd_path() -> None:
    """Prompt should give generic PRD advice when no canonical path is present."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Just a regular issue.",
        labels=(),
    )
    prompt = build_prompt(issue, Path("/worktree"), PromptConfig())
    assert "If the Issue references a PRD, read it before editing." in prompt


def test_build_recovery_prompt_separates_prd_change_log_from_checklist() -> None:
    """Recovery prompt should preserve the PRD evolution rule."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prompt = build_recovery_prompt(
        issue,
        Path("/worktree"),
        recovery_attempt=1,
        max_recovery_attempts=2,
        failure_summary="Something broke.",
    )
    assert "tasks/pending/example.md" in prompt
    assert "Acceptance Checklist" in prompt
    assert "Change Log" in prompt
    assert "never `git mv` it into" in prompt
    assert "never move it back to `tasks/pending/`" in prompt


def test_build_prompt_includes_change_log_format_example() -> None:
    """Prompt 必须内嵌可解析的 Change Log 格式样例，且声明表格不被解析。"""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prompt = build_prompt(issue, Path("/worktree"), PromptConfig())
    assert "### <short title of this change>" in prompt
    assert "- Type:" in prompt
    assert "Markdown tables are NOT parsed" in prompt


def test_build_progress_continuation_prompt_mentions_existing_progress() -> None:
    """续作 prompt 要明确"已有提交、不要从零开始/回退"并引用 PRD 路径。"""
    from backend.core.use_cases.run_agent_once import (
        build_progress_continuation_prompt,
    )

    prompt = build_progress_continuation_prompt(
        make_prd_issue("tasks/pending/feature.md"),
        Path("/tmp/wt/issue-123"),
    )

    assert "Continue GitHub Issue #123" in prompt
    assert "already contains committed progress" in prompt
    assert "do not revert existing" in prompt.lower()
    assert "tasks/pending/feature.md" in prompt
    assert "commit-request.json" in prompt


# ---------------------------------------------------------------------------
# PRD inlining tests (deliberate-async-discussion PRD, Section 5 / FR-12/13)
# ---------------------------------------------------------------------------


def _write_prd(worktree_path: Path, relative_path: str, content: str) -> None:
    prd_path = worktree_path / relative_path
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(content, encoding="utf-8")


def test_build_prd_context_block_inlines_existing_prd(tmp_path: Path) -> None:
    """When the worktree contains the PRD, the helper inlines its full body."""
    _write_prd(tmp_path, "tasks/pending/example.md", "# Example PRD\n\nBody line.\n")
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="- PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    block = _build_prd_context_block(issue, tmp_path)
    assert "--- BEGIN PRD ---" in block
    assert "--- END PRD ---" in block
    assert "Body line." in block
    assert "tasks/pending/example.md" in block
    assert "Acceptance Checklist" in block
    assert "tasks/archive/" in block


def test_build_prd_context_block_truncates_oversized_prd(tmp_path: Path) -> None:
    """PRDs above the ceiling are tail-truncated with a pointer note."""
    big = "x" * 5000
    _write_prd(tmp_path, "tasks/pending/big.md", big)
    issue = IssueSummary(
        number=2,
        title="T",
        url="U",
        body="- PRD path: `tasks/pending/big.md`",
        labels=(),
    )
    block = _build_prd_context_block(issue, tmp_path, max_chars=200)
    assert "--- BEGIN PRD ---" in block
    assert "truncated" in block
    assert "tasks/pending/big.md" in block
    # The first 200 chars of the body are present, the rest is dropped.
    assert big[:200] in block
    assert big[300:] not in block


def test_build_prd_context_block_falls_back_when_prd_missing(tmp_path: Path) -> None:
    """Missing PRD file → pointer line only, no BEGIN/END marker."""
    issue = IssueSummary(
        number=3,
        title="T",
        url="U",
        body="- PRD path: `tasks/pending/missing.md`",
        labels=(),
    )
    block = _build_prd_context_block(issue, tmp_path)
    assert "BEGIN PRD" not in block
    assert "tasks/pending/missing.md" in block
    assert "Acceptance Checklist" in block


def test_build_prd_context_block_handles_no_prd_anchor(tmp_path: Path) -> None:
    """Issue without a PRD anchor gets the generic pointer line."""
    issue = IssueSummary(
        number=4,
        title="T",
        url="U",
        body="No PRD here.",
        labels=(),
    )
    block = _build_prd_context_block(issue, tmp_path)
    assert "BEGIN PRD" not in block
    assert "If the Issue references a PRD" in block


def test_build_prd_context_block_handles_archived_prd(tmp_path: Path) -> None:
    """Archive-path PRDs inline the same way but don't mention tasks/archive/."""
    _write_prd(tmp_path, "tasks/archive/done.md", "archived body\n")
    issue = IssueSummary(
        number=5,
        title="T",
        url="U",
        body="- PRD path: `tasks/archive/done.md`",
        labels=(),
    )
    block = _build_prd_context_block(issue, tmp_path)
    assert "--- BEGIN PRD ---" in block
    assert "archived body" in block
    # No archive instruction for already-archived PRDs.
    assert "move the PRD from `tasks/pending/" not in block


def test_build_prompt_inlines_prd(tmp_path: Path) -> None:
    """``build_prompt`` should embed the worktree's PRD body, not just a pointer."""
    _write_prd(tmp_path, "tasks/pending/feature.md", "# Feature PRD\n\nDetail A.\n")
    issue = IssueSummary(
        number=10,
        title="Feature",
        url="https://example/repo/issues/10",
        body="- PRD path: `tasks/pending/feature.md`",
        labels=(),
    )
    prompt = build_prompt(issue, tmp_path, PromptConfig())
    assert "Detail A." in prompt
    assert "BEGIN PRD" in prompt
    assert "tasks/pending/feature.md" in prompt


def test_build_prompt_inlines_prd_uses_default_ceiling(tmp_path: Path) -> None:
    """The default ceiling is large enough for normal PRDs (no truncation)."""
    body = "line\n" * 1000
    _write_prd(tmp_path, "tasks/pending/normal.md", body)
    issue = IssueSummary(
        number=11,
        title="Normal",
        url="https://example/repo/issues/11",
        body="- PRD path: `tasks/pending/normal.md`",
        labels=(),
    )
    prompt = build_prompt(issue, tmp_path, PromptConfig())
    assert "truncated" not in prompt
    assert body in prompt
    # Sanity-check the default ceiling is at least the published value.
    assert _DEFAULT_PRD_INLINE_MAX_CHARS >= 20000


def test_build_recovery_prompt_inlines_prd(tmp_path: Path) -> None:
    """``build_recovery_prompt`` should also inline the PRD body."""
    _write_prd(tmp_path, "tasks/pending/feature.md", "# Feature PRD\n\nRecover hint.\n")
    issue = IssueSummary(
        number=12,
        title="Feature",
        url="https://example/repo/issues/12",
        body="- PRD path: `tasks/pending/feature.md`",
        labels=(),
    )
    prompt = build_recovery_prompt(
        issue,
        tmp_path,
        recovery_attempt=1,
        max_recovery_attempts=2,
        failure_summary="Something broke.",
    )
    assert "Recover hint." in prompt
    assert "BEGIN PRD" in prompt


def test_build_recovery_prompt_no_prd_uses_fallback(tmp_path: Path) -> None:
    """Without a PRD anchor, recovery prompt uses the existing fallback line."""
    issue = IssueSummary(
        number=13,
        title="No PRD",
        url="https://example/repo/issues/13",
        body="Plain body",
        labels=(),
    )
    prompt = build_recovery_prompt(
        issue,
        tmp_path,
        recovery_attempt=1,
        max_recovery_attempts=2,
        failure_summary="x",
    )
    assert "If the Issue references a PRD" in prompt


def test_build_progress_continuation_prompt_inlines_prd(tmp_path: Path) -> None:
    """``build_progress_continuation_prompt`` should also inline the PRD body."""
    _write_prd(tmp_path, "tasks/pending/feature.md", "# Feature PRD\n\nContinue.\n")
    issue = IssueSummary(
        number=14,
        title="Feature",
        url="https://example/repo/issues/14",
        body="- PRD path: `tasks/pending/feature.md`",
        labels=(),
    )
    prompt = build_progress_continuation_prompt(issue, tmp_path)
    assert "Continue." in prompt
    assert "BEGIN PRD" in prompt


def test_build_prompt_no_prd_keeps_pointer_line(tmp_path: Path) -> None:
    """Without a PRD anchor, build_prompt keeps the existing generic line."""
    issue = IssueSummary(
        number=15,
        title="No PRD",
        url="https://example/repo/issues/15",
        body="no PRD anchor here",
        labels=(),
    )
    prompt = build_prompt(issue, tmp_path, PromptConfig())
    assert "If the Issue references a PRD" in prompt
    assert "BEGIN PRD" not in prompt


def test_build_prd_context_block_handles_unicode_prd(tmp_path: Path) -> None:
    """Reading the PRD uses UTF-8 explicitly (project rule)."""
    _write_prd(tmp_path, "tasks/pending/cn.md", "# 中文 PRD\n\n详细描述。\n")
    issue = IssueSummary(
        number=16,
        title="中文",
        url="https://example/repo/issues/16",
        body="- PRD path: `tasks/pending/cn.md`",
        labels=(),
    )
    block = _build_prd_context_block(issue, tmp_path)
    assert "中文 PRD" in block
    assert "详细描述。" in block
