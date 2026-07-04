"""Tests for the memory stable anchoring fix.

Covers three concerns introduced by
``tasks/pending/P1-BUG-20260704-153640-agent-runner-memory-stable-anchoring.md``:

1. The factory layer absolutises relative memory directories against the
   target repo root and leaves ``~`` / absolute paths untouched.
2. ``build_default_memory_services`` records a warning when the config
   still contains relative paths (the legitimate warning path).
3. The three memory stores perform atomic writes via
   ``infrastructure/memory/_atomic_io.atomic_write_text`` so concurrent
   saves never produce half-written files, and ``enabled=False`` skips
   every read/write.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.core.agent.memory._composition import (
    _log_relative_anchor_warning,
    build_default_memory_services,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    AttemptResult,
    FailureType,
    IssueSummary,
    MemoryConfig,
)
from backend.engines.agent_runner.factory import (
    _anchor_memory_config,
)
from backend.infrastructure.memory import atomic_write_text
from backend.infrastructure.memory.skill_draft_store import (
    SkillDraftStore,
    SkillDraftUpdate,
    _parse_skill,
)
from backend.infrastructure.memory.short_term_store import (
    ShortTermMemoryContext,
    ShortTermMemoryStore,
)


# ---------------------------------------------------------------------------
# factory anchoring (FR-1, FR-2)
# ---------------------------------------------------------------------------


def test_anchor_memory_config_resolves_relative_against_repo_root(tmp_path: Path) -> None:
    """相对路径解析到 ``repo_path`` 下，绝对路径原样保留。"""
    repo_root = tmp_path / "repo"
    memory = MemoryConfig(
        enabled=True,
        base_dir=".iar/memory",
        skill_drafts_dir=".iar/skills/drafts",
        promoted_skills_dirs=(".iar/skills",),
    )

    anchored = _anchor_memory_config(memory, repo_root)

    assert anchored.base_dir == str(repo_root / ".iar/memory")
    assert anchored.skill_drafts_dir == str(repo_root / ".iar/skills" / "drafts")
    assert anchored.promoted_skills_dirs == (str(repo_root / ".iar/skills"),)
    # 原始对象是 frozen dataclass，必须保持原样以避免调用方共享同一内存。
    assert memory.base_dir == ".iar/memory"


def test_anchor_memory_config_keeps_absolute_paths_verbatim(tmp_path: Path) -> None:
    """绝对路径（含 ``~`` 展开后仍为绝对者）原样使用。"""
    repo_root = tmp_path / "repo"
    absolute_base = str(tmp_path / "elsewhere" / "memory")
    absolute_drafts = str(tmp_path / "elsewhere" / "drafts")
    absolute_promoted = str(tmp_path / "elsewhere" / "skills")

    memory = MemoryConfig(
        enabled=True,
        base_dir=absolute_base,
        skill_drafts_dir=absolute_drafts,
        promoted_skills_dirs=(absolute_promoted,),
    )

    anchored = _anchor_memory_config(memory, repo_root)
    assert anchored.base_dir == absolute_base
    assert anchored.skill_drafts_dir == absolute_drafts
    assert anchored.promoted_skills_dirs == (absolute_promoted,)


def test_anchor_memory_config_expands_tilde() -> None:
    """``~`` 被 :meth:`pathlib.Path.expanduser` 展开。"""
    repo_root = Path("/does-not-matter")
    memory = MemoryConfig(
        enabled=True,
        base_dir="~/memories/base",
        skill_drafts_dir="~/.local/skills/drafts",
        promoted_skills_dirs=("~/.local/skills",),
    )

    anchored = _anchor_memory_config(memory, repo_root)
    for resolved in (
        anchored.base_dir,
        anchored.skill_drafts_dir,
        anchored.promoted_skills_dirs[0],
    ):
        assert "~" not in resolved


# ---------------------------------------------------------------------------
# composition layer warning (FR-4)
# ---------------------------------------------------------------------------


def test_log_relative_anchor_warning_emits_when_relative(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """相对目录触发 warning，绝对目录静默。"""
    memory = MemoryConfig(
        enabled=True,
        base_dir=".iar/memory",
        skill_drafts_dir=".iar/skills/drafts",
        promoted_skills_dirs=(".iar/skills",),
    )
    with caplog.at_level(
        logging.WARNING,
        logger="backend.core.agent.memory._composition",
    ):
        _log_relative_anchor_warning(memory)
    assert any(
        "Memory config still has relative paths" in record.message for record in caplog.records
    )


def test_log_relative_anchor_warning_quiet_on_absolute(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """绝对目录不应触发 warning。"""
    memory = MemoryConfig(
        enabled=True,
        base_dir="/tmp/whatever/memory",
        skill_drafts_dir="/tmp/whatever/drafts",
        promoted_skills_dirs=("/tmp/whatever/skills",),
    )
    with caplog.at_level(
        logging.WARNING,
        logger="backend.core.agent.memory._composition",
    ):
        _log_relative_anchor_warning(memory)
    assert not any(
        "Memory config still has relative paths" in record.message for record in caplog.records
    )


def test_build_default_memory_services_disabled_returns_none_sentinels(
    tmp_path: Path,
) -> None:
    """``enabled=False`` 全部跳过，stores 字段都是 ``None``。"""
    memory = MemoryConfig(
        enabled=False,
        base_dir=str(tmp_path / "memory"),
        skill_drafts_dir=str(tmp_path / "drafts"),
        promoted_skills_dirs=(str(tmp_path / "skills"),),
    )
    config = AppConfig(memory=memory)
    services = build_default_memory_services(tmp_path, config.memory)
    assert services.is_disabled()
    assert services.short_term is None
    assert services.long_term is None
    assert services.skill is None


def test_disabled_writes_nothing(tmp_path: Path) -> None:
    """``enabled=False`` 时即使写入路径有，磁盘上也不应出现任何文件。

    Drives the full short-term save entry point with ``enabled=False``
    and asserts nothing was written to the worktree.
    """
    from backend.core.agent.memory.short_term_memory import (
        save_short_term_memory,
    )

    store = ShortTermMemoryStore(tmp_path / "never-touched")
    config = AppConfig(
        memory=MemoryConfig(
            enabled=False,
            base_dir=str(tmp_path / "never-touched"),
            skill_drafts_dir=str(tmp_path / "never-touched" / "drafts"),
            promoted_skills_dirs=(str(tmp_path / "never-touched" / "skills"),),
        ),
    )
    issue = IssueSummary(
        number=42,
        title="disabled",
        url="https://example/42",
        body="disabled body",
        labels=("memory", "disabled"),
    )
    attempt = AttemptResult(
        attempt_number=1,
        failure_type=FailureType.SUCCESS,
        detail="detail",
        recovered=True,
    )
    result = save_short_term_memory(
        repo_id="rv-disabled",
        issue=issue,
        attempt_result=attempt,
        worktree_path=tmp_path,
        memory_config=config.memory,
        store=store,
    )
    assert result is None
    assert not (tmp_path / "never-touched").exists()


# ---------------------------------------------------------------------------
# atomic write helper (FR-5)
# ---------------------------------------------------------------------------


def test_atomic_write_text_replaces_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_no_tmp_leak_on_failure(tmp_path: Path) -> None:
    """写入失败时不应留下临时文件。"""
    target = tmp_path / "out.txt"

    class _BoomError(RuntimeError):
        pass

    real_replace = os.replace

    def _exploding_replace(*_args: object, **_kwargs: object) -> None:
        raise _BoomError("boom")

    os.replace = _exploding_replace  # type: ignore[assignment]
    try:
        with pytest.raises(_BoomError):
            atomic_write_text(target, "anything")
    finally:
        os.replace = real_replace  # type: ignore[assignment]
    leftovers = list(target.parent.glob(f".{target.name}.*.tmp"))
    assert leftovers == [], f"临时文件残留：{leftovers}"


# ---------------------------------------------------------------------------
# concurrent atomic writes (rv-6)
# ---------------------------------------------------------------------------


def _make_short_term_store(tmp_path: Path) -> ShortTermMemoryStore:
    return ShortTermMemoryStore(tmp_path)


def _build_context(issue_number: int, attempt: int) -> ShortTermMemoryContext:
    return ShortTermMemoryContext(
        repo_id="rv-concurrency",
        issue_number=issue_number,
        issue_title=f"Issue #{issue_number}",
        issue_url=f"https://example/{issue_number}",
        summary=f"attempt {attempt}",
        final_solution=f"solution {attempt}",
    )


def test_concurrent_short_term_store_atomic(tmp_path: Path) -> None:
    """并发写同一 issue 的短期记忆后，目标文件仍是完整可解析 JSON。

    暴露 last-write-wins 行为但不出现半写损坏文件。
    """
    store = _make_short_term_store(tmp_path)
    barrier = threading.Barrier(parties=16)

    def _worker(index: int) -> None:
        barrier.wait()
        ctx = _build_context(issue_number=123, attempt=index)
        store.save("rv-concurrency", 123, ctx)

    with ThreadPoolExecutor(max_workers=16) as pool:
        for index in range(16):
            pool.submit(_worker, index)

    output = tmp_path / "short_term" / "rv-concurrency" / "123" / "context.json"
    assert output.is_file()
    parsed = json.loads(output.read_text(encoding="utf-8"))
    assert parsed["issue_number"] == 123
    assert parsed["final_solution"].startswith("solution ")


def test_concurrent_short_term_store_non_atomic_corrupts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """负向控制：把 ``atomic_write_text`` 替换为分两段非原子直写后，
    并发 save 同一 issue 产生半写破损文件。

    本用例断言**破损发生**——即 PRD §9 rv-6 要求的"去掉原子替换后
    同用例变红"反向记录。它通过证明"非原子直写在并发下会坏"反向
    佐证 ``test_concurrent_short_term_store_atomic`` 的成功是真正
    归功于 ``atomic_write_text`` 的 ``tmp + os.replace`` 语义，而非
    非原子写碰巧也完整。

    设计要点：两个线程写**不同长度**的内容、各自从中点拆分两段写、
    中间用 ``Barrier`` 强制交错。不同长度保证两线程第二段写入偏移
    不同，使最终文件必然是两线程内容的混合体——任何混合都无法解析
    为合法 JSON 或与任一完整 worker 的内容一致。
    """
    from backend.infrastructure.memory import short_term_store as st_module

    barrier = threading.Barrier(parties=2, timeout=10)

    def _split_non_atomic_write(target_path: str | Path, content: str) -> Path:
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        midpoint = len(content) // 2
        # 第一段：truncate + 写前半 + 关闭（模拟非原子直写被抢占后落盘）
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content[:midpoint])
            handle.flush()
        # barrier 强制两线程都已写入前半段后再各自续写后半段
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        # 第二段：在已存在文件上 seek 到中点续写后半段
        with open(path, "r+", encoding="utf-8") as handle:
            handle.seek(midpoint)
            handle.write(content[midpoint:])
            handle.flush()
        return path

    monkeypatch.setattr(st_module, "atomic_write_text", _split_non_atomic_write)

    store = _make_short_term_store(tmp_path)
    issue = 777
    valid_solutions: set[str] = set()

    def _worker(solution: str) -> None:
        ctx = ShortTermMemoryContext(
            repo_id="rv-concurrency",
            issue_number=issue,
            issue_title=f"Issue #{issue}",
            issue_url=f"https://example/{issue}",
            summary=f"attempt with {solution}",
            final_solution=solution,
        )
        valid_solutions.add(solution)
        store.save("rv-concurrency", issue, ctx)

    # 刻意使用长度差异显著的两个 solution，确保两线程 JSON 长度不同、
    # 中点不同、第二段写入偏移不同——这是确定性产生混合破损的关键。
    short_solution = "ok"
    long_solution = "apply-tmp-plus-os-replace-" * 20
    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(_worker, short_solution)
        pool.submit(_worker, long_solution)

    output = tmp_path / "short_term" / "rv-concurrency" / str(issue) / "context.json"
    raw = output.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
        final = str(parsed.get("final_solution", ""))
    except json.JSONDecodeError:
        final = "<unparseable>"

    assert final not in valid_solutions, (
        f"负向控制未观测到破损：非原子分写并发后目标文件仍为完整合法内容 "
        f"(final_solution={final!r})，预期应为两线程内容的混合破损。"
    )


def test_concurrent_skill_draft_store_atomic(tmp_path: Path) -> None:
    """并发 save 同名 skill 草稿后，目标文件仍是完整 markdown。"""
    store = SkillDraftStore(tmp_path / "drafts")
    barrier = threading.Barrier(parties=8)

    def _worker(index: int) -> None:
        barrier.wait()
        store.save_draft(
            SkillDraftUpdate(
                name="alpha",
                description=f"draft {index}",
                tags=("rv-6",),
                body="body content",
                usage_count=index,
                success_count=index,
            ),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        for index in range(8):
            pool.submit(_worker, index)

    output = tmp_path / "drafts" / "alpha.md"
    assert output.is_file()
    text = output.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "description:" in text


def test_concurrent_skill_draft_store_non_atomic_corrupts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """负向控制：把 ``atomic_write_text`` 替换为分两段非原子直写后，
    并发 save 同名 skill 草稿产生半写破损 markdown。

    机制同 ``test_concurrent_short_term_store_non_atomic_corrupts``：
    两线程写不同长度内容、中点拆分、``Barrier`` 交错，保证最终文件
    是两线程内容的混合体。断言正向结果（文件能被 ``_parse_skill``
    解析为某个完整 worker 的 skill——description 与 body 均精确匹配）
    不再成立——即"去掉原子替换后同用例变红"。

    刻意让两线程的 ``description`` **和** ``body`` 长度差异都显著，
    使 frontmatter 与 body 段的中点偏移都不同，确保任何交错都产生
    无法解析或内容混合的破损文件。
    """
    from backend.infrastructure.memory import skill_draft_store as sd_module

    barrier = threading.Barrier(parties=2, timeout=10)

    def _split_non_atomic_write(target_path: str | Path, content: str) -> Path:
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        midpoint = len(content) // 2
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content[:midpoint])
            handle.flush()
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        with open(path, "r+", encoding="utf-8") as handle:
            handle.seek(midpoint)
            handle.write(content[midpoint:])
            handle.flush()
        return path

    monkeypatch.setattr(sd_module, "atomic_write_text", _split_non_atomic_write)

    store = SkillDraftStore(tmp_path / "drafts")
    valid_descriptions: set[str] = set()
    valid_bodies: set[str] = set()

    def _worker(description: str, body: str) -> None:
        valid_descriptions.add(description)
        valid_bodies.add(body)
        store.save_draft(
            SkillDraftUpdate(
                name="alpha",
                description=description,
                tags=("rv-6",),
                body=body,
                usage_count=1,
                success_count=1,
            ),
        )

    # 两线程的 description 与 body 长度都显著不同，确保 frontmatter 与
    # body 的中点偏移各异——这是确定性产生混合破损的关键。
    short_desc = "short"
    long_desc = "long-description-" * 30
    short_body = "short body"
    long_body = "long body content " * 50
    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(_worker, short_desc, short_body)
        pool.submit(_worker, long_desc, long_body)

    output = tmp_path / "drafts" / "alpha.md"
    parsed = _parse_skill(output)
    if parsed is None:
        corrupted = True
    else:
        corrupted = parsed.description not in valid_descriptions or parsed.body not in valid_bodies
    assert corrupted, (
        "负向控制未观测到破损：非原子分写并发后目标 markdown 仍可解析为某个完整 "
        "worker 的 skill（description 与 body 均精确匹配），预期应为两线程内容的混合破损。"
    )
