"""Tests for PRD delivery readiness gating.

Covers ``resolve_prd_archive_path`` and ``ensure_prd_delivery_ready``:
acceptance checklist completeness, Change Log format enforcement and the
pending -> archive ``git mv`` transition."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    IssueSummary,
)
from backend.core.use_cases.run_agent_once import (
    PrdDeliveryError,
    ensure_prd_delivery_ready,
    resolve_prd_archive_path,
)
from tests.conftest import FakeProcessRunner
from tests.support.agent_runner import (
    create_commit,
    init_git_repo,
)


def test_resolve_prd_archive_path_converts_pending() -> None:
    """Pending PRD paths should map to the archive directory."""
    assert resolve_prd_archive_path("tasks/pending/example.md") == "tasks/archive/example.md"


def test_resolve_prd_archive_path_returns_none_for_non_pending() -> None:
    """Non-pending paths should not resolve to an archive path."""
    assert resolve_prd_archive_path("tasks/archive/example.md") is None
    assert resolve_prd_archive_path("docs/example.md") is None


def test_ensure_prd_delivery_ready_skips_when_no_prd_path(tmp_path: Path) -> None:
    """Gate should be a no-op when the Issue has no canonical PRD path."""
    issue = IssueSummary(number=1, title="T", url="U", body="No PRD.", labels=())
    fake_runner = FakeProcessRunner()
    ensure_prd_delivery_ready(issue, tmp_path, fake_runner)
    assert fake_runner.calls == []


def test_ensure_prd_delivery_ready_raises_when_pending_incomplete(
    tmp_path: Path,
) -> None:
    """Pending PRD with unchecked items should raise PrdDeliveryError."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prd_path = tmp_path / "tasks" / "pending" / "example.md"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "- [x] done",
                "- [ ] undone",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()
    with pytest.raises(PrdDeliveryError, match="unchecked items"):
        ensure_prd_delivery_ready(issue, tmp_path, fake_runner)


def test_ensure_prd_delivery_ready_requires_change_log_for_prd_change(
    tmp_path: Path,
) -> None:
    """本轮修改 PRD 时必须有独立于 Checklist 的结构化 Change Log。"""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prd_path = tmp_path / "tasks" / "pending" / "example.md"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_content = "# PRD\n\n## Acceptance Checklist\n\n- [x] done\n"
    prd_path.write_text(
        baseline_content + "\n新增了实现细节。\n",
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()

    with pytest.raises(PrdDeliveryError, match="without a Change Log section"):
        ensure_prd_delivery_ready(
            issue,
            tmp_path,
            fake_runner,
            prd_baseline_content=baseline_content,
        )


def test_ensure_prd_delivery_ready_requires_new_change_log_entry(
    tmp_path: Path,
) -> None:
    """已有 Change Log 时，每次新的 PRD 修改仍须追加一条记录。"""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    baseline_content = """# PRD

## Change Log

### 2026-07-13 · Earlier change
- 类型：实现细化
- 原文：原实现
- 变更后：新实现
- 原因：补齐边界
- 影响：无用户可见变化
- 审核：已记录

## Acceptance Checklist

- [x] done
"""
    prd_path = tmp_path / "tasks" / "pending" / "example.md"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        baseline_content.replace("# PRD", "# PRD\n\n新的实现说明"),
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()

    with pytest.raises(PrdDeliveryError, match="without appending a Change Log entry"):
        ensure_prd_delivery_ready(
            issue,
            tmp_path,
            fake_runner,
            prd_baseline_content=baseline_content,
        )


def _prd_body_with_table_change_log(baseline_content: str) -> str:
    """构造把 Change Log 写成 Markdown 表格的 PRD（解析器会数成 0 条）。"""
    return (
        baseline_content
        + "\n新的实现说明\n\n## Change Log\n\n"
        + "| # | Type | Before | After | Reason | Impact | Review |\n"
        + "|---|------|--------|-------|--------|--------|--------|\n"
        + "| CL-1 | evidence | 旧 | 新 | 补齐证据 | 无用户可见变化 | 待审 |\n"
    )


def test_ensure_prd_delivery_ready_rejects_table_change_log(tmp_path: Path) -> None:
    """Change Log 写成表格时门禁必须报错并解释表格行不被计入。

    这是历史 recovery 死循环的根因：agent 用表格记录变更、解析器数成 0 条，
    错误信息又不说明原因，agent 每轮只会往表格里再补一行。
    """
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prd_path = tmp_path / "tasks" / "pending" / "example.md"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_content = "# PRD\n\n## Acceptance Checklist\n\n- [x] done\n"
    prd_path.write_text(
        _prd_body_with_table_change_log(baseline_content),
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()

    with pytest.raises(
        PrdDeliveryError,
        match="without a Change Log entry.*table rows are not counted",
    ):
        ensure_prd_delivery_ready(
            issue,
            tmp_path,
            fake_runner,
            prd_baseline_content=baseline_content,
        )


def test_ensure_prd_delivery_ready_accepts_bullet_change_log(tmp_path: Path) -> None:
    """规范的 ``###`` 标题 + bullet 字段格式必须通过 Change Log 门禁。"""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prd_path = tmp_path / "tasks" / "pending" / "example.md"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "tasks" / "archive").mkdir(parents=True, exist_ok=True)
    baseline_content = "# PRD\n\n## Acceptance Checklist\n\n- [x] done\n"
    prd_path.write_text(
        baseline_content
        + "\n## Change Log\n\n"
        + "### 2026-07-24 · 验收更新\n"
        + "- Type: evidence\n"
        + "- Before: 验收项未完成\n"
        + "- After: 验收项已完成\n"
        + "- Reason: 已执行缺失验证\n"
        + "- Impact: 无用户可见变化\n"
        + "- Review: runner 门禁待验证\n",
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()

    # 不抛异常即视为通过；runner 会随后 git add + git mv 归档。
    ensure_prd_delivery_ready(
        issue,
        tmp_path,
        fake_runner,
        prd_baseline_content=baseline_content,
    )


def test_ensure_prd_delivery_ready_validates_change_log_on_archive_path(
    tmp_path: Path,
) -> None:
    """agent 私自把 PRD 移进 archive/ 也不能绕过 Change Log 门禁。"""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    baseline_content = "# PRD\n\n## Acceptance Checklist\n\n- [x] done\n"
    # 模拟 agent 违规 git mv：pending 路径不存在，archive 路径存在且 Change Log 是表格。
    archive_path = tmp_path / "tasks" / "archive" / "example.md"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(
        _prd_body_with_table_change_log(baseline_content),
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()

    with pytest.raises(PrdDeliveryError, match="without a Change Log entry"):
        ensure_prd_delivery_ready(
            issue,
            tmp_path,
            fake_runner,
            prd_baseline_content=baseline_content,
        )


def test_ensure_prd_delivery_ready_git_mv_when_pending_complete(
    tmp_path: Path,
) -> None:
    """Complete pending PRD should be moved to archive by git mv."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prd_path = tmp_path / "tasks" / "pending" / "example.md"
    archive_dir = tmp_path / "tasks" / "archive"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "- [x] done",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()
    ensure_prd_delivery_ready(issue, tmp_path, fake_runner)
    # The on-disk PRD is staged before the move so ``git mv`` cannot abort with
    # "not under version control" when the file is untracked (e.g. left behind
    # by a PRD rewrite that overwrote it without re-staging).
    add_call = ["git", "add", "--", "tasks/pending/example.md"]
    mv_call = [
        "git",
        "mv",
        "tasks/pending/example.md",
        "tasks/archive/example.md",
    ]
    assert add_call in fake_runner.calls
    assert mv_call in fake_runner.calls
    assert fake_runner.calls.index(add_call) < fake_runner.calls.index(mv_call)


def test_ensure_prd_delivery_ready_passes_when_archive_complete(
    tmp_path: Path,
) -> None:
    """Archived PRD with all items checked should pass the gate."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    archive_path = tmp_path / "tasks" / "archive" / "example.md"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(
        "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "- [x] done",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()
    ensure_prd_delivery_ready(issue, tmp_path, fake_runner)
    assert fake_runner.calls == []


def test_ensure_prd_delivery_ready_raises_when_missing_section(
    tmp_path: Path,
) -> None:
    """PRD without Acceptance Checklist section should raise PrdDeliveryError."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prd_path = tmp_path / "tasks" / "pending" / "example.md"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text("# PRD\n", encoding="utf-8")
    fake_runner = FakeProcessRunner()
    with pytest.raises(PrdDeliveryError, match="Acceptance Checklist section missing"):
        ensure_prd_delivery_ready(issue, tmp_path, fake_runner)


def test_ensure_prd_delivery_ready_raises_when_prd_missing(
    tmp_path: Path,
) -> None:
    """Missing canonical PRD should raise PrdDeliveryError."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    fake_runner = FakeProcessRunner()
    with pytest.raises(PrdDeliveryError, match="Canonical PRD not found"):
        ensure_prd_delivery_ready(issue, tmp_path, fake_runner)


def test_ensure_prd_delivery_ready_raises_when_archive_dir_missing(
    tmp_path: Path,
) -> None:
    """Pending PRD ready for archive but missing archive dir should raise PrdDeliveryError."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )
    prd_path = tmp_path / "tasks" / "pending" / "example.md"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "- [x] done",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_runner = FakeProcessRunner()
    with pytest.raises(PrdDeliveryError, match="Archive directory does not exist"):
        ensure_prd_delivery_ready(issue, tmp_path, fake_runner)


def test_ensure_prd_delivery_ready_archives_untracked_prd_real_git(
    tmp_path: Path,
) -> None:
    """A complete PRD present on disk but missing from the index is archived.

    Reproduces the runner failure where a PRD rewrite left the pending file
    untracked with its deletion staged: ``git mv`` alone aborts with "not under
    version control" (exit 128). The archive step must stage the on-disk file
    first so the move succeeds and the rewritten content is preserved.
    """
    from backend.infrastructure.process_runner import SubprocessRunner

    repo = init_git_repo(tmp_path)
    pending_path = tmp_path / "tasks" / "pending" / "example.md"
    archive_dir = tmp_path / "tasks" / "archive"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / ".gitkeep").write_text("", encoding="utf-8")
    prd_body = "\n".join(["# PRD", "", "## Acceptance Checklist", "", "- [x] done", ""])
    pending_path.write_text(prd_body, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        check=True,
        capture_output=True,
    )
    create_commit(repo, "publish prd")

    # Reproduce the broken worktree state: drop the PRD from the index and
    # overwrite it on disk, leaving it untracked with its deletion staged.
    subprocess.run(
        ["git", "-C", str(repo), "rm", "--cached", "tasks/pending/example.md"],
        check=True,
        capture_output=True,
    )
    rewritten_body = prd_body + "<!-- rewritten -->\n"
    pending_path.write_text(rewritten_body, encoding="utf-8")

    issue = IssueSummary(
        number=7,
        title="T",
        url="U",
        body="PRD path: `tasks/pending/example.md`",
        labels=(),
    )

    ensure_prd_delivery_ready(issue, tmp_path, SubprocessRunner())

    archived_path = archive_dir / "example.md"
    assert not pending_path.exists()
    assert archived_path.exists()
    assert archived_path.read_text(encoding="utf-8") == rewritten_body
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "tasks/archive/example.md"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert tracked == "tasks/archive/example.md"
