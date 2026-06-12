"""Tests for the Realistic Validation evidence gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PullRequestContext,
    ValidationConfig,
)
from backend.core.use_cases.agent_runner_events import format_event_marker
from backend.core.use_cases.agent_runner_publish import publish_changes
from backend.core.use_cases.agent_runner_validation import (
    EvidenceUpload,
    ValidationEvidenceError,
    build_evidence_comment,
    build_issue_validation_section,
    build_validation_checklist_block,
    build_validation_prompt_line,
    cleanup_closed_issue_evidence_branches,
    collect_evidence_coverage_problems,
    ensure_evidence_dir_excluded,
    ensure_no_evidence_paths_in_changes,
    ensure_validation_evidence_ready,
    evidence_branch_name,
    evidence_format_check_required,
    demanded_evidence_kinds,
    extract_evidence_format_markers,
    extract_evidence_format_waiver_reason,
    extract_realistic_validation_items,
    extract_validation_waiver_reason,
    format_evidence_format_waiver_marker,
    format_validation_waiver_marker,
    has_validation_waiver_marker,
    list_evidence_files,
    parse_latest_evidence_marker,
    parse_pr_number,
    parse_validation_checklist_state,
    process_validation_gate,
    publish_validation_evidence,
    reset_validation_checklist,
    upload_evidence_branch,
    validation_required,
)
from backend.core.use_cases.create_issue_from_prd import (
    IssueFromPrdRequest,
    create_issue_from_prd,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner


_PRD_WITH_VALIDATION = """# PRD: Demo

## 1. Introduction & Goals

### Realistic Validation

除单元测试和集成测试外，本 PRD 要求真实入口验证。

- [x] **行为 A 真实验证**：通过 `demo run` 验证输出。
- [ ] **行为 B 真实验证**：通过 `demo serve` 验证页面。

## 2. Requirement Shape
"""

_PRD_WITH_WAIVER = """# PRD: Docs only

### Realistic Validation

Validation Waiver: 纯文档变更，无可执行表面（operator 已确认）。

## 2. Requirement Shape
"""

_ISSUE_BODY_WITH_VALIDATION = """## Summary

Tracked task.

## Realistic Validation

The executing agent MUST run each item.

- [ ] **行为 A 真实验证**：通过 `demo run` 验证输出。
- [ ] **行为 B 真实验证**：通过 `demo serve` 验证页面。
"""


def _issue(body: str = _ISSUE_BODY_WITH_VALIDATION, number: int = 42) -> IssueSummary:
    return IssueSummary(
        number=number,
        title="Demo",
        url=f"https://github.com/example/repo/issues/{number}",
        body=body,
        labels=("agent/review",),
    )


# ---------------------------------------------------------------------------
# Markdown 解析与物化
# ---------------------------------------------------------------------------


def test_extract_items_normalizes_checked_state() -> None:
    """Checked PRD items become unchecked issue items."""
    items = extract_realistic_validation_items(_PRD_WITH_VALIDATION)
    assert len(items) == 2
    assert all(item.startswith("- [ ] ") for item in items)


def test_extract_items_stops_at_next_section() -> None:
    """Items outside the validation section are ignored."""
    text = "\n".join(
        [
            "### Realistic Validation",
            "- [ ] inside",
            "### Delivery Dependencies",
            "- [ ] outside",
        ]
    )
    assert extract_realistic_validation_items(text) == ["- [ ] inside"]


def test_extract_waiver_reason() -> None:
    """Explicit waiver declarations are parsed; absent ones return None."""
    waiver_reason = extract_validation_waiver_reason(_PRD_WITH_WAIVER)
    assert waiver_reason is not None
    assert "operator" in waiver_reason
    assert extract_validation_waiver_reason(_PRD_WITH_VALIDATION) is None


def test_waiver_marker_roundtrip() -> None:
    """Formatted waiver markers are detected by the parser."""
    marker = format_validation_waiver_marker('reason with "quotes"')
    assert has_validation_waiver_marker(f"## Body\n\n{marker}\n")
    assert not has_validation_waiver_marker("plain body")


def test_build_issue_validation_section_with_items_and_waiver() -> None:
    """Issue section carries either the checklist or the waiver marker."""
    checklist_section = build_issue_validation_section(
        checklist_items=["- [ ] item"], waiver_reason=None
    )
    assert "## Realistic Validation" in checklist_section
    assert "- [ ] item" in checklist_section

    waiver_section = build_issue_validation_section(
        checklist_items=[], waiver_reason="docs only"
    )
    assert "iar:validation-waived" in waiver_section
    assert "- [ ]" not in waiver_section


def test_validation_required_rules() -> None:
    """Required only when enabled, items exist, and no waiver marker."""
    config = AppConfig()
    assert validation_required(_ISSUE_BODY_WITH_VALIDATION, config)
    assert not validation_required("no checklist here", config)
    waived_body = _ISSUE_BODY_WITH_VALIDATION + format_validation_waiver_marker("ok")
    assert not validation_required(waived_body, config)
    disabled_config = AppConfig(validation=ValidationConfig(enabled=False))
    assert not validation_required(_ISSUE_BODY_WITH_VALIDATION, disabled_config)


def test_build_validation_prompt_line() -> None:
    """Prompt line appears only for evidence-requiring issues."""
    config = AppConfig()
    prompt_line = build_validation_prompt_line(_issue(), config)
    assert ".iar/evidence" in prompt_line
    assert build_validation_prompt_line(_issue(body="plain"), config) == ""


# ---------------------------------------------------------------------------
# 证据目录与门禁
# ---------------------------------------------------------------------------


def test_list_evidence_files_filters_hidden_and_dirs(tmp_path: Path) -> None:
    """Only first-level regular non-hidden files count as evidence."""
    config = AppConfig()
    evidence_dir = tmp_path / ".iar" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "rv-1-shot.png").write_bytes(b"png")
    (evidence_dir / ".hidden").write_text("x", encoding="utf-8")
    (evidence_dir / "nested").mkdir()
    (evidence_dir / "nested" / "deep.png").write_bytes(b"png")

    evidence_files = list_evidence_files(tmp_path, config)
    assert [file.name for file in evidence_files] == ["rv-1-shot.png"]


def test_ensure_validation_evidence_ready_raises_without_evidence(
    tmp_path: Path,
) -> None:
    """Required validation with empty evidence dir fails the gate."""
    with pytest.raises(ValidationEvidenceError):
        ensure_validation_evidence_ready(_issue(), tmp_path, AppConfig())


def test_ensure_validation_evidence_ready_passes_with_evidence(
    tmp_path: Path,
) -> None:
    """Per-item evidence satisfies the gate; waived issues skip it entirely."""
    config = AppConfig()
    evidence_dir = tmp_path / ".iar" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "rv-1.png").write_bytes(b"png")
    (evidence_dir / "rv-2-serve.txt").write_text("$ demo serve", encoding="utf-8")
    ensure_validation_evidence_ready(_issue(), tmp_path, config)

    ensure_validation_evidence_ready(
        _issue(body="no checklist"), tmp_path / "missing", config
    )


def test_ensure_validation_evidence_ready_rejects_uncovered_item(
    tmp_path: Path,
) -> None:
    """Every checklist item must have its own rv-<n>-* evidence file."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "rv-1-run.txt").write_text("$ demo run", encoding="utf-8")

    with pytest.raises(ValidationEvidenceError) as exc_info:
        ensure_validation_evidence_ready(_issue(), tmp_path, AppConfig())
    assert "item 2" in str(exc_info.value)
    assert "rv-2" in str(exc_info.value)


def test_ensure_validation_evidence_ready_rejects_missing_screenshot(
    tmp_path: Path,
) -> None:
    """Items demanding screenshots (截图) must carry image evidence."""
    issue_body = "\n".join(
        [
            "## Realistic Validation",
            "",
            "- [ ] **登录页真实验证**：浏览器操作登录页（截图留证）。",
            "- [ ] **CLI 真实验证**：通过 `demo run` 验证输出。",
        ]
    )
    evidence_dir = tmp_path / ".iar" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "rv-1-login.txt").write_text("fake log", encoding="utf-8")
    (evidence_dir / "rv-2-cli.txt").write_text("$ demo run", encoding="utf-8")

    with pytest.raises(ValidationEvidenceError) as exc_info:
        ensure_validation_evidence_ready(_issue(body=issue_body), tmp_path, AppConfig())
    assert "screenshot" in str(exc_info.value)
    assert "rv-1" in str(exc_info.value)

    (evidence_dir / "rv-1-login.png").write_bytes(b"png")
    ensure_validation_evidence_ready(_issue(body=issue_body), tmp_path, AppConfig())


def test_ensure_validation_evidence_ready_matches_named_formats(
    tmp_path: Path,
) -> None:
    """Items naming pdf/word/txt formats demand matching file suffixes."""
    issue_body = "\n".join(
        [
            "## Realistic Validation",
            "",
            "- [ ] **导出真实验证**：导出 PDF 报告并核对内容。",
            "- [ ] **Word 导出真实验证**：生成 Word 文档并人工检查排版。",
            "- [ ] **CLI 真实验证**：终端输出保存为 .txt。",
        ]
    )
    evidence_dir = tmp_path / ".iar" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "rv-1-report.txt").write_text("not a pdf", encoding="utf-8")
    (evidence_dir / "rv-2-doc.txt").write_text("not a docx", encoding="utf-8")
    (evidence_dir / "rv-3-cli.txt").write_text("$ demo export", encoding="utf-8")

    with pytest.raises(ValidationEvidenceError) as exc_info:
        ensure_validation_evidence_ready(_issue(body=issue_body), tmp_path, AppConfig())
    error_text = str(exc_info.value)
    assert "PDF" in error_text
    assert "Word" in error_text
    assert "rv-3" not in error_text

    (evidence_dir / "rv-1-report.pdf").write_bytes(b"%PDF")
    (evidence_dir / "rv-2-doc.docx").write_bytes(b"PK")
    ensure_validation_evidence_ready(_issue(body=issue_body), tmp_path, AppConfig())


def test_format_check_disabled_by_config_keeps_non_empty_gate(
    tmp_path: Path,
) -> None:
    """Config off: per-item matching skipped, empty dir still rejected."""
    relaxed_config = AppConfig(validation=ValidationConfig(evidence_format_check=False))
    with pytest.raises(ValidationEvidenceError):
        ensure_validation_evidence_ready(_issue(), tmp_path, relaxed_config)

    evidence_dir = tmp_path / ".iar" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "anything.txt").write_text("output", encoding="utf-8")
    ensure_validation_evidence_ready(_issue(), tmp_path, relaxed_config)


def test_format_check_disabled_by_issue_marker(tmp_path: Path) -> None:
    """An iar:evidence-format-waived marker skips per-item matching."""
    config = AppConfig()
    evidence_dir = tmp_path / ".iar" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "rv-1-run.txt").write_text("$ demo run", encoding="utf-8")

    with pytest.raises(ValidationEvidenceError):
        ensure_validation_evidence_ready(_issue(), tmp_path, config)

    waived_body = _ISSUE_BODY_WITH_VALIDATION + format_evidence_format_waiver_marker(
        "局部豁免理由"
    )
    ensure_validation_evidence_ready(_issue(body=waived_body), tmp_path, config)


def test_extract_evidence_format_waiver_reason() -> None:
    """The format waiver line parses only inside the validation section."""
    prd_text = "\n".join(
        [
            "### Realistic Validation",
            "",
            "Evidence Format Waiver: 证据为外部系统回执，格式不固定。",
            "",
            "- [ ] **行为 A 真实验证**：通过 `demo run` 验证输出。",
        ]
    )
    waiver_reason = extract_evidence_format_waiver_reason(prd_text)
    assert waiver_reason is not None
    assert "外部系统" in waiver_reason
    assert extract_evidence_format_waiver_reason(_PRD_WITH_VALIDATION) is None


def test_build_issue_validation_section_with_format_waiver() -> None:
    """The format waiver materializes alongside the checklist, not instead."""
    section = build_issue_validation_section(
        checklist_items=["- [ ] item"],
        waiver_reason=None,
        format_waiver_reason="格式不固定",
    )
    assert "iar:evidence-format-waived" in section
    assert "- [ ] item" in section
    config = AppConfig()
    assert validation_required(section, config)
    assert not evidence_format_check_required(section, config)


def test_collect_evidence_coverage_problems_matches_items() -> None:
    """Coverage problems name the item number and demanded evidence kind."""
    checklist_items = [
        "- [ ] 帧流登录真实验证：浏览器操作（截图留证）。",
        "- [ ] WebSocket 端点真实验证：pytest 真实入口。",
    ]
    problems = collect_evidence_coverage_problems(
        checklist_items, [Path("rv-1-frame.txt")]
    )
    assert len(problems) == 2
    assert "screenshot" in problems[0]
    assert "item 2" in problems[1]

    assert (
        collect_evidence_coverage_problems(
            checklist_items, [Path("rv-1-frame.png"), Path("rv-2-ws.txt")]
        )
        == []
    )


def test_extract_evidence_format_markers() -> None:
    """Markers are parsed into a {item_number: kind} mapping."""
    body = (
        "<!-- iar:evidence-format item=1 kind=screenshot -->\n"
        "<!-- iar:evidence-format item=2 kind=txt -->\n"
        "<!-- iar:evidence-format item=3 kind=none -->"
    )
    markers = extract_evidence_format_markers(body)
    assert markers == {1: "screenshot", 2: "txt"}


def test_extract_evidence_format_markers_ignores_unknown_kinds() -> None:
    """Unknown or 'none' kinds are skipped so future extensions are safe."""
    body = (
        "<!-- iar:evidence-format item=1 kind=screenshot -->\n"
        "<!-- iar:evidence-format item=2 kind=unknown -->"
    )
    markers = extract_evidence_format_markers(body)
    assert markers == {1: "screenshot"}


def test_demanded_evidence_kinds_prefers_marker() -> None:
    """When a marker exists for the item, it overrides regex matching."""
    item_text = "浏览器操作登录页（截图留证）。"
    body = "<!-- iar:evidence-format item=1 kind=txt -->"
    kinds = demanded_evidence_kinds(item_text, issue_body=body, item_number=1)
    assert len(kinds) == 1
    assert kinds[0].label == "plain-text capture (.txt/.log)"


def test_demanded_evidence_kinds_fallback_to_regex() -> None:
    """Without a marker, the function falls back to regex keyword matching."""
    item_text = "浏览器操作登录页（截图留证）。"
    kinds = demanded_evidence_kinds(item_text)
    assert len(kinds) == 1
    assert "screenshot" in kinds[0].label.lower()


def test_collect_evidence_coverage_problems_with_markers() -> None:
    """Coverage problems respect markers when issue_body is provided."""
    checklist_items = [
        "- [ ] item 1",
        "- [ ] item 2",
    ]
    body = "<!-- iar:evidence-format item=1 kind=screenshot -->"
    problems = collect_evidence_coverage_problems(
        checklist_items, [Path("rv-1-run.txt")], issue_body=body
    )
    assert len(problems) == 2
    assert "screenshot" in problems[0]
    assert "item 2" in problems[1]

    # Correct format satisfies the marker
    assert (
        collect_evidence_coverage_problems(
            checklist_items,
            [Path("rv-1-run.png"), Path("rv-2-cli.txt")],
            issue_body=body,
        )
        == []
    )


def test_ensure_evidence_dir_excluded_is_idempotent(tmp_path: Path) -> None:
    """The exclude line is appended once, preserving existing content."""
    exclude_path = tmp_path / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True)
    exclude_path.write_text("existing-rule\n", encoding="utf-8")
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "--git-path", "info/exclude"): CommandResult(
                command=("git", "rev-parse", "--git-path", "info/exclude"),
                return_code=0,
                stdout=str(exclude_path),
                stderr="",
            )
        }
    )
    config = AppConfig()

    ensure_evidence_dir_excluded(tmp_path, config, fake_runner)
    ensure_evidence_dir_excluded(tmp_path, config, fake_runner)

    exclude_lines = exclude_path.read_text(encoding="utf-8").splitlines()
    assert exclude_lines.count("/.iar/evidence/") == 1
    assert "existing-rule" in exclude_lines


def test_ensure_no_evidence_paths_in_changes_blocks_leak(tmp_path: Path) -> None:
    """Evidence paths in the diff refuse publication."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain", "-z"): CommandResult(
                command=("git", "status", "--porcelain", "-z"),
                return_code=0,
                stdout="A  .iar/evidence/rv-1.png\0M  src/app.py\0",
                stderr="",
            )
        }
    )
    with pytest.raises(RuntimeError, match="evidence"):
        ensure_no_evidence_paths_in_changes(tmp_path, AppConfig(), fake_runner)


# ---------------------------------------------------------------------------
# PR body 勾选清单区块
# ---------------------------------------------------------------------------


def test_checklist_block_roundtrip_and_reset() -> None:
    """The block parses its own output and resets ticked boxes."""
    block = build_validation_checklist_block(["- [ ] item A", "- [ ] item B"])
    pr_body = f"Closes #42\n\n{block}\n"

    unchecked_state = parse_validation_checklist_state(pr_body)
    assert unchecked_state is not None
    assert (unchecked_state.total, unchecked_state.unchecked_count) == (2, 2)

    ticked_body = pr_body.replace("- [ ] item A", "- [x] item A").replace(
        "- [ ] item B", "- [X] item B"
    )
    ticked_state = parse_validation_checklist_state(ticked_body)
    assert ticked_state is not None
    assert ticked_state.checked_count == 2
    assert ticked_state.unchecked_count == 0

    reset_body = reset_validation_checklist(ticked_body)
    reset_state = parse_validation_checklist_state(reset_body)
    assert reset_state is not None
    assert reset_state.unchecked_count == 2
    # 区块外的内容保持不变
    assert reset_body.startswith("Closes #42")


def test_parse_checklist_state_without_block() -> None:
    """PR bodies without the marker block return None."""
    assert parse_validation_checklist_state("plain body") is None


# ---------------------------------------------------------------------------
# 证据上传与 PR 证据评论
# ---------------------------------------------------------------------------


def _evidence_worktree(tmp_path: Path) -> tuple[Path, Path]:
    evidence_dir = tmp_path / ".iar" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "rv-1-shot.png").write_bytes(b"png")
    (evidence_dir / "rv-2-cli.txt").write_text("$ demo run\nok\n", encoding="utf-8")
    return tmp_path, evidence_dir


def test_upload_evidence_branch_uses_orphan_plumbing(tmp_path: Path) -> None:
    """Evidence is pushed via hash-object/mktree/commit-tree without parents."""
    worktree_path, evidence_dir = _evidence_worktree(tmp_path)
    config = AppConfig()
    responses = {
        (
            "git",
            "hash-object",
            "-w",
            "--",
            str(evidence_dir / "rv-1-shot.png"),
        ): CommandResult(("git",), 0, "blob1\n", ""),
        (
            "git",
            "hash-object",
            "-w",
            "--",
            str(evidence_dir / "rv-2-cli.txt"),
        ): CommandResult(("git",), 0, "blob2\n", ""),
        ("git", "mktree"): CommandResult(("git", "mktree"), 0, "tree1\n", ""),
        (
            "git",
            "commit-tree",
            "tree1",
            "-m",
            "Realistic Validation evidence for issue #42",
        ): CommandResult(("git",), 0, "commit1\n", ""),
    }
    fake_runner = FakeProcessRunner(responses=responses)

    upload = upload_evidence_branch(
        issue=_issue(),
        worktree_path=worktree_path,
        config=config,
        process_runner=fake_runner,
    )

    assert upload is not None
    assert upload.branch == "iar-evidence/issue-42"
    assert upload.commit_sha == "commit1"
    assert upload.file_names == ("rv-1-shot.png", "rv-2-cli.txt")
    # mktree 输入两行 blob 条目
    mktree_input = fake_runner.input_texts[fake_runner.calls.index(["git", "mktree"])]
    assert mktree_input is not None
    assert "100644 blob blob1\trv-1-shot.png" in mktree_input
    # commit-tree 无 -p 参数（orphan 提交）
    commit_tree_call = next(
        call for call in fake_runner.calls if call[:2] == ["git", "commit-tree"]
    )
    assert "-p" not in commit_tree_call
    assert [
        "git",
        "push",
        "--force",
        config.git.remote,
        "commit1:refs/heads/iar-evidence/issue-42",
    ] in fake_runner.calls


def test_upload_evidence_branch_returns_none_without_files(tmp_path: Path) -> None:
    """Empty evidence directories skip the upload."""
    upload = upload_evidence_branch(
        issue=_issue(),
        worktree_path=tmp_path,
        config=AppConfig(),
        process_runner=FakeProcessRunner(),
    )
    assert upload is None


def test_build_evidence_comment_embeds_images_and_quotes_text(
    tmp_path: Path,
) -> None:
    """Images embed via blob raw links; text files are quoted inline."""
    worktree_path, _evidence_dir = _evidence_worktree(tmp_path)
    config = AppConfig()
    comment = build_evidence_comment(
        upload=EvidenceUpload(
            branch="iar-evidence/issue-42",
            commit_sha="commit1",
            file_names=("rv-1-shot.png", "rv-2-cli.txt"),
        ),
        worktree_path=worktree_path,
        config=config,
        pr_url="https://github.com/example/repo/pull/7",
        head_sha="abc1234",
    )

    assert (
        "![rv-1-shot.png](https://github.com/example/repo/blob/"
        "iar-evidence/issue-42/rv-1-shot.png?raw=true)" in comment
    )
    assert "$ demo run" in comment
    evidence_marker = parse_latest_evidence_marker([comment])
    assert evidence_marker is not None
    assert evidence_marker.head_sha == "abc1234"
    assert evidence_marker.branch == "iar-evidence/issue-42"
    assert evidence_marker.count == 2


def test_parse_pr_number() -> None:
    """PR numbers parse from canonical GitHub URLs."""
    assert parse_pr_number("https://github.com/example/repo/pull/123") == 123
    assert parse_pr_number("not a url") is None


def test_publish_validation_evidence_posts_pr_comment(tmp_path: Path) -> None:
    """The composite helper uploads evidence and comments on the PR."""
    worktree_path, evidence_dir = _evidence_worktree(tmp_path)
    config = AppConfig()
    responses = {
        (
            "git",
            "hash-object",
            "-w",
            "--",
            str(evidence_dir / "rv-1-shot.png"),
        ): CommandResult(("git",), 0, "blob1\n", ""),
        (
            "git",
            "hash-object",
            "-w",
            "--",
            str(evidence_dir / "rv-2-cli.txt"),
        ): CommandResult(("git",), 0, "blob2\n", ""),
        ("git", "mktree"): CommandResult(("git", "mktree"), 0, "tree1\n", ""),
        (
            "git",
            "commit-tree",
            "tree1",
            "-m",
            "Realistic Validation evidence for issue #42",
        ): CommandResult(("git",), 0, "commit1\n", ""),
    }
    fake_runner = FakeProcessRunner(responses=responses)
    fake_client = FakeGitHubClient()

    upload = publish_validation_evidence(
        issue=_issue(),
        worktree_path=worktree_path,
        config=config,
        github_client=fake_client,
        process_runner=fake_runner,
        pr_url="https://github.com/example/repo/pull/7",
        head_sha="abc1234",
    )

    assert upload is not None
    pr_comment_calls = [
        call for call in fake_client.calls if call["method"] == "comment_pr"
    ]
    assert len(pr_comment_calls) == 1
    assert pr_comment_calls[0]["pr_number"] == 7
    assert "iar:validation-evidence" in pr_comment_calls[0]["body"]


def test_publish_validation_evidence_skips_when_not_required(
    tmp_path: Path,
) -> None:
    """Issues without a validation checklist skip evidence publication."""
    fake_client = FakeGitHubClient()
    upload = publish_validation_evidence(
        issue=_issue(body="plain"),
        worktree_path=tmp_path,
        config=AppConfig(),
        github_client=fake_client,
        process_runner=FakeProcessRunner(),
        pr_url="https://github.com/example/repo/pull/7",
        head_sha="abc1234",
    )
    assert upload is None
    assert all(call["method"] != "comment_pr" for call in fake_client.calls)


# ---------------------------------------------------------------------------
# publish_changes 集成（清单注入与泄漏守卫）
# ---------------------------------------------------------------------------


def test_publish_changes_appends_checklist_block(tmp_path: Path) -> None:
    """PR bodies gain the marker-wrapped human sign-off checklist."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                ("git",), 0, "task/42\n", ""
            ),
            ("git", "remote"): CommandResult(("git",), 0, "origin\n", ""),
        }
    )
    fake_client = FakeGitHubClient()

    publish_changes(
        _issue(),
        tmp_path,
        AppConfig(),
        fake_client,
        fake_runner,
    )

    draft_pr_call = next(
        call for call in fake_client.calls if call["method"] == "create_draft_pr"
    )
    assert "iar:realistic-validation version=1 total=2" in draft_pr_call["body"]
    assert "iar:realistic-validation-end" in draft_pr_call["body"]


def test_publish_changes_skips_checklist_for_waived_issue(tmp_path: Path) -> None:
    """Waived issues publish without the sign-off checklist."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                ("git",), 0, "task/42\n", ""
            ),
            ("git", "remote"): CommandResult(("git",), 0, "origin\n", ""),
        }
    )
    fake_client = FakeGitHubClient()
    waived_body = _ISSUE_BODY_WITH_VALIDATION + format_validation_waiver_marker("ok")

    publish_changes(
        _issue(body=waived_body),
        tmp_path,
        AppConfig(),
        fake_client,
        fake_runner,
    )

    draft_pr_call = next(
        call for call in fake_client.calls if call["method"] == "create_draft_pr"
    )
    assert "iar:realistic-validation" not in draft_pr_call["body"]


# ---------------------------------------------------------------------------
# daemon 软门禁
# ---------------------------------------------------------------------------


class _GateGitHubClient(FakeGitHubClient):
    """Fake client with review issues and PR contexts for gate tests."""

    def __init__(self, review_issues: list[IssueSummary]) -> None:
        super().__init__()
        self._review_issues = review_issues

    def list_review_candidate_issues(self, labels, limit):  # noqa: ANN001, D102
        super().list_review_candidate_issues(labels, limit)
        return list(self._review_issues)


def _gate_setup(
    *,
    pr_body: str,
    pr_head: str = "abc1111",
    evidence_head: str | None = "abc1111",
    issue_labels: tuple[str, ...] = ("agent/review",),
) -> tuple[_GateGitHubClient, IssueSummary]:
    review_issue = IssueSummary(
        number=42,
        title="Demo",
        url="https://github.com/example/repo/issues/42",
        body=_ISSUE_BODY_WITH_VALIDATION,
        labels=issue_labels,
    )
    gate_client = _GateGitHubClient([review_issue])
    lifecycle_comment = "\n".join(
        [
            format_event_marker(
                phase="draft_pr_created",
                cycle=1,
                head_sha=pr_head,
                pr_branch="task/42",
            ),
            "Draft PR created.",
        ]
    )
    gate_client._issue_comments[42] = [lifecycle_comment]
    gate_client._pr_contexts["task/42"] = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/7",
        branch="task/42",
        head_sha=pr_head,
        base_sha="basesha",
        number=7,
        body=pr_body,
    )
    if evidence_head is not None:
        gate_client._pr_comments[7] = [
            f"<!-- iar:validation-evidence version=1 head={evidence_head} "
            "branch=iar-evidence/issue-42 count=2 -->"
        ]
    return gate_client, review_issue


def _checklist_pr_body(*, ticked: bool) -> str:
    block = build_validation_checklist_block(["- [ ] item A", "- [ ] item B"])
    if ticked:
        block = block.replace("- [ ] item", "- [x] item")
    return f"Closes #42\n\n{block}\n"


def test_gate_keeps_pending_while_unchecked(tmp_path: Path) -> None:
    """Unchecked checklists converge labels to validation/pending."""
    gate_client, _review_issue = _gate_setup(pr_body=_checklist_pr_body(ticked=False))

    process_validation_gate(
        repo_path=tmp_path,
        config=AppConfig(),
        github_client=gate_client,
        process_runner=FakeProcessRunner(),
    )

    label_calls = [
        call for call in gate_client.calls if call["method"] == "edit_issue_labels"
    ]
    assert label_calls == [
        {
            "method": "edit_issue_labels",
            "issue_number": 42,
            "add": ["validation/pending"],
            "remove": ["validation/passed"],
        }
    ]


def test_gate_passes_and_audits_once(tmp_path: Path) -> None:
    """Fully ticked checklists earn validation/passed plus one audit comment."""
    gate_client, _review_issue = _gate_setup(pr_body=_checklist_pr_body(ticked=True))

    process_validation_gate(
        repo_path=tmp_path,
        config=AppConfig(),
        github_client=gate_client,
        process_runner=FakeProcessRunner(),
    )

    label_calls = [
        call for call in gate_client.calls if call["method"] == "edit_issue_labels"
    ]
    assert label_calls[0]["add"] == ["validation/passed"]
    audit_comments = [
        call
        for call in gate_client.calls
        if call["method"] == "comment_issue" and "validation_passed" in call["body"]
    ]
    assert len(audit_comments) == 1

    # 第二轮：audit comment 已存在（含相同 head），不应重复发评
    process_validation_gate(
        repo_path=tmp_path,
        config=AppConfig(),
        github_client=gate_client,
        process_runner=FakeProcessRunner(),
    )
    audit_comments_after_second_pass = [
        call
        for call in gate_client.calls
        if call["method"] == "comment_issue" and "validation_passed" in call["body"]
    ]
    assert len(audit_comments_after_second_pass) == 1


def test_gate_resets_stale_sign_off(tmp_path: Path) -> None:
    """New commits after sign-off untick the checklist and notify."""
    gate_client, _review_issue = _gate_setup(
        pr_body=_checklist_pr_body(ticked=True),
        pr_head="def2222",
        evidence_head="abc1111",
    )

    process_validation_gate(
        repo_path=tmp_path,
        config=AppConfig(),
        github_client=gate_client,
        process_runner=FakeProcessRunner(),
    )

    body_updates = [
        call
        for call in gate_client.calls
        if call["method"] == "update_pull_request_body"
    ]
    assert len(body_updates) == 1
    reset_state = parse_validation_checklist_state(body_updates[0]["body"])
    assert reset_state is not None
    assert reset_state.unchecked_count == 2
    reset_comments = [
        call
        for call in gate_client.calls
        if call["method"] == "comment_pr" and "validation_reset" in call["body"]
    ]
    assert len(reset_comments) == 1
    label_calls = [
        call for call in gate_client.calls if call["method"] == "edit_issue_labels"
    ]
    assert label_calls[0]["add"] == ["validation/pending"]


def test_gate_skips_issue_without_checklist_block(tmp_path: Path) -> None:
    """PRs without the marker block are left untouched."""
    gate_client, _review_issue = _gate_setup(pr_body="Closes #42\n")

    process_validation_gate(
        repo_path=tmp_path,
        config=AppConfig(),
        github_client=gate_client,
        process_runner=FakeProcessRunner(),
    )

    assert all(call["method"] != "edit_issue_labels" for call in gate_client.calls)


def test_cleanup_deletes_branches_for_closed_issues(tmp_path: Path) -> None:
    """Evidence branches of closed issues are deleted remotely."""
    config = AppConfig()
    fake_client = FakeGitHubClient()
    fake_client._issue_states[41] = "CLOSED"
    fake_client._issue_states[42] = "OPEN"
    ls_remote_command = (
        "git",
        "ls-remote",
        "--heads",
        config.git.remote,
        "refs/heads/iar-evidence/*",
    )
    fake_runner = FakeProcessRunner(
        responses={
            ls_remote_command: CommandResult(
                ls_remote_command,
                0,
                "sha1\trefs/heads/iar-evidence/issue-41\n"
                "sha2\trefs/heads/iar-evidence/issue-42\n",
                "",
            )
        }
    )

    cleanup_closed_issue_evidence_branches(
        repo_path=tmp_path,
        config=config,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert [
        "git",
        "push",
        config.git.remote,
        "--delete",
        "iar-evidence/issue-41",
    ] in fake_runner.calls
    assert [
        "git",
        "push",
        config.git.remote,
        "--delete",
        "iar-evidence/issue-42",
    ] not in fake_runner.calls


def test_evidence_branch_name_uses_prefix() -> None:
    """Branch names follow <prefix>issue-<N>."""
    config = AppConfig(validation=ValidationConfig(branch_prefix="proof/"))
    assert evidence_branch_name(7, config) == "proof/issue-7"


# ---------------------------------------------------------------------------
# commit 循环证据门禁
# ---------------------------------------------------------------------------


def test_run_agent_until_committed_fails_without_evidence(tmp_path: Path) -> None:
    """Missing evidence exhausts recovery and surfaces the gate failure."""
    from backend.core.shared.models.agent_runner import RunnerConfig
    from backend.core.use_cases.run_agent_once import (
        MaxRetriesExceededError,
        run_agent_until_committed,
    )

    config = AppConfig(
        runner=RunnerConfig(
            max_recovery_attempts=1,
            recovery_retry_delay_seconds=0,
            verification_commands=(),
        )
    )
    fake_runner = FakeProcessRunner()

    with pytest.raises(MaxRetriesExceededError) as exc_info:
        run_agent_until_committed(
            selected_agent="claude",
            issue=_issue(),
            worktree_path=tmp_path,
            config=config,
            process_runner=fake_runner,
            before_sha="abc1111",
            expected_branch="task/42",
        )

    assert any(
        "Realistic Validation evidence" in attempt.detail
        for attempt in exc_info.value.attempt_results
    )


# ---------------------------------------------------------------------------
# iar issue create 物化
# ---------------------------------------------------------------------------


def test_create_issue_materializes_validation_checklist(tmp_path: Path) -> None:
    """PRDs with a validation section produce issue bodies with the checklist."""
    prd_path = tmp_path / "tasks" / "pending" / "20260101-000000-prd-demo.md"
    prd_path.parent.mkdir(parents=True)
    prd_path.write_text(_PRD_WITH_VALIDATION, encoding="utf-8")
    fake_client = FakeGitHubClient()

    create_issue_from_prd(
        request=IssueFromPrdRequest(
            repo_path=tmp_path,
            prd_path=prd_path,
            issue_type="feature",
        ),
        github_client=fake_client,
    )

    create_call = next(
        call for call in fake_client.calls if call["method"] == "create_issue"
    )
    assert "## Realistic Validation" in create_call["body"]
    assert "The executing agent MUST run each item" in create_call["body"]
    assert create_call["body"].count("- [ ] **行为") >= 2
    assert "iar:validation-waived" not in create_call["body"]


def test_create_issue_materializes_waiver_marker(tmp_path: Path) -> None:
    """PRDs with an explicit waiver produce the hidden marker, no checklist."""
    prd_path = tmp_path / "tasks" / "pending" / "20260101-000000-prd-docs.md"
    prd_path.parent.mkdir(parents=True)
    prd_path.write_text(_PRD_WITH_WAIVER, encoding="utf-8")
    fake_client = FakeGitHubClient()

    create_issue_from_prd(
        request=IssueFromPrdRequest(
            repo_path=tmp_path,
            prd_path=prd_path,
            issue_type="docs",
        ),
        github_client=fake_client,
    )

    create_call = next(
        call for call in fake_client.calls if call["method"] == "create_issue"
    )
    assert "iar:validation-waived" in create_call["body"]
    assert "## Realistic Validation" in create_call["body"]
    body_validation_state = parse_validation_checklist_state(create_call["body"])
    assert body_validation_state is None
