"""``.gitignore`` sync for ``iar init``.

``iar init`` 在写完 ``.iar.toml`` 之后,会确保 IAR 运行时的中间产物
(``.iar/``、``.agent-runner/``、``.iar-worktrees/``) 出现在
``.gitignore`` 中。新增的条目用 ``# >>> iar (managed by `iar init`) >>>``
/ ``# <<< iar <<<`` 块标记包裹,保证:

1. 幂等 — 重跑 ``iar init`` 不会产生重复条目或与已有配置冲突。
2. 局部 — 块外的现有 ``.gitignore`` 内容(项目自定义规则)不会被修改。
3. 可外部声明 — 如果用户已经在块外写了 ``.agent-runner/`` 之类,
   块内会跳过该条目,避免冗余。

``.git/info/exclude`` 是仓库内每个 clone 独立的本地文件,无法从工具
统一写入,因此这里只检测并提示历史漂移,不在 init 阶段强改。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

GITIGNORE_BLOCK_HEADER = "# >>> iar (managed by `iar init`) >>>"
"""``iar init`` 管理的 ``.gitignore`` 块的起始标记。"""

GITIGNORE_BLOCK_FOOTER = "# <<< iar <<<"
"""``iar init`` 管理的 ``.gitignore`` 块的结束标记。"""

# 段注释 -> 该段包含的 ignore 模式。结构化便于幂等地构建块,
# 也让用户能一眼看出每条规则的作用域。
IAR_GITIGNORE_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("# Realistic Validation evidence (worktree-local)", (".iar/",)),
    ("# Agent runner state (worktree-local)", (".agent-runner/",)),
    ("# IAR-managed git worktrees (host-local)", (".iar-worktrees/",)),
)
"""``iar init`` 需要写入 ``.gitignore`` 的所有条目,按段分组。"""

# 出现在 ``.git/info/exclude`` 中、且应被 ``.gitignore`` 块取代的
# 历史条目。检测到时给用户打印提示,提示手动清理。
_LEGACY_INFO_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "/.iar/evidence/",
    "/.iar-worktrees/",
)


@dataclass(frozen=True)
class GitignoreSyncOptions:
    """``ensure_gitignore_entries`` 的入参。

    Attributes:
        repo_root_path: 仓库根目录。函数会在其下读/写 ``.gitignore``。
        dry_run: 为真时不写文件,只生成 :class:`GitignoreSyncResult`
            用于打印计划。
        skip: 为真时完全跳过(``--no-update-gitignore``),仍会检测
            ``.git/info/exclude`` 的历史漂移并填入 ``info_exclude_hint``。
    """

    repo_root_path: Path
    dry_run: bool = False
    skip: bool = False


@dataclass(frozen=True)
class GitignoreSyncResult:
    """``ensure_gitignore_entries`` 的执行结果。

    Attributes:
        gitignore_path: 仓库根下的 ``.gitignore`` 绝对路径(无论是否写入)。
        block_inserted: 新建了 ``.gitignore`` 或新插入了 iar 块。
        block_updated: iar 块已存在但内容需要更新(例如部分条目已
            在块外声明,块需要同步收缩)。
        entries_added: 实际写入块的模式列表(顺序与
            :data:`IAR_GITIGNORE_SECTIONS` 一致)。
        entries_skipped_external: 因已在 iar 块外的 ``.gitignore`` 中
            声明而被跳过的模式列表。
        info_exclude_hint: 检测到 ``.git/info/exclude`` 含历史 iar 条目,
            应提示用户清理。
        dry_run: 透传 :class:`GitignoreSyncOptions` 的 ``dry_run``。
        skipped: 用户显式 opt-out(``--no-update-gitignore``)。
    """

    gitignore_path: Path
    block_inserted: bool
    block_updated: bool
    entries_added: tuple[str, ...]
    entries_skipped_external: tuple[str, ...]
    info_exclude_hint: bool
    dry_run: bool
    skipped: bool


def _read_gitignore_text(gitignore_path: Path) -> str:
    """读取 ``.gitignore`` 文本;文件不存在返回空串。"""
    if not gitignore_path.is_file():
        return ""
    return gitignore_path.read_text(encoding="utf-8")


def _parse_gitignore_blocks(text: str) -> tuple[str, str | None, str]:
    """把 ``.gitignore`` 切成 ``(prelude, iar_block 或 None, postlude)``。

    仅识别 iar 自己管理的块;块外内容原样保留。若 header 存在但 footer
    缺失,视为损坏的块并返回 ``(text, None, "")``,避免误删用户内容。
    """
    if not text:
        return "", None, ""
    lines = text.splitlines(keepends=True)
    header_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.rstrip("\r\n") == GITIGNORE_BLOCK_HEADER:
            header_idx = idx
            break
    if header_idx is None:
        return text, None, ""
    footer_idx: int | None = None
    for idx in range(header_idx + 1, len(lines)):
        if lines[idx].rstrip("\r\n") == GITIGNORE_BLOCK_FOOTER:
            footer_idx = idx
            break
    if footer_idx is None:
        # 块不闭合(被人为编辑破坏) — 拒绝触碰,避免数据丢失。
        return text, None, ""
    prelude = "".join(lines[:header_idx])
    block_text = "".join(lines[header_idx : footer_idx + 1])
    postlude = "".join(lines[footer_idx + 1 :])
    return prelude, block_text, postlude


def _block_patterns(block_text: str | None) -> set[str]:
    """从 iar 块中提取非空、非注释的模式集合。"""
    if not block_text:
        return set()
    patterns: set[str] = set()
    for line in block_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.add(stripped)
    return patterns


def _patterns_outside_block(text: str) -> set[str]:
    """返回 ``.gitignore`` 中位于 iar 块外的非空、非注释模式集合。"""
    if not text:
        return set()
    _, block_text, _ = _parse_gitignore_blocks(text)
    block_lines: set[str] = set()
    if block_text:
        for line in block_text.splitlines():
            stripped = line.strip()
            if stripped:
                block_lines.add(stripped)
    result: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in block_lines:
            continue
        result.add(stripped)
    return result


def _build_block_text(
    external_patterns: set[str],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """构造 iar 块文本。

    Args:
        external_patterns: 块外已存在的模式集合。块内不再重复出现。

    Returns:
        ``(block_text, entries_added, entries_skipped_external)``。
        当所有模式都已在块外时,返回的 block_text 只含 header 与 footer
        之间的空体,调用方据此决定是否完全跳过块的写入。
    """
    added: list[str] = []
    skipped: list[str] = []
    body_lines: list[str] = []
    for comment, patterns in IAR_GITIGNORE_SECTIONS:
        kept: list[str] = []
        for pattern in patterns:
            if pattern in external_patterns:
                skipped.append(pattern)
                continue
            added.append(pattern)
            kept.append(pattern)
        if not kept:
            continue
        if body_lines:
            body_lines.append("")
        body_lines.append(comment)
        body_lines.extend(kept)
    block_text = (
        f"{GITIGNORE_BLOCK_HEADER}\n"
        + "\n".join(body_lines)
        + f"\n{GITIGNORE_BLOCK_FOOTER}\n"
    )
    return block_text, tuple(added), tuple(skipped)


def _info_exclude_has_iar_entries(repo_root_path: Path) -> bool:
    """检测 ``.git/info/exclude`` 是否含历史 iar 条目。"""
    info_exclude = repo_root_path / ".git" / "info" / "exclude"
    if not info_exclude.is_file():
        return False
    try:
        text = info_exclude.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in _LEGACY_INFO_EXCLUDE_PATTERNS:
            return True
        # 块外 .gitignore 已声明的模式(用户搬过来的副本)也算漂移。
        for _, patterns in IAR_GITIGNORE_SECTIONS:
            if stripped in patterns:
                return True
    return False


def _replace_iar_block(existing_text: str, new_block_text: str) -> str:
    """用 ``new_block_text`` 替换 ``existing_text`` 中的 iar 块。

    块外(prelude / postlude)逐字符保留,包括空行与缩进。
    """
    prelude, _, postlude = _parse_gitignore_blocks(existing_text)
    return prelude + new_block_text + postlude


def ensure_gitignore_entries(
    options: GitignoreSyncOptions,
) -> GitignoreSyncResult:
    """确保 ``.gitignore`` 含 IAR 运行时中间产物的 ignore 条目。

    行为契约:
    - 块外已有等价条目 → 块内不重复,只在结果里报告 ``skipped_external``。
    - 块外为空 → 直接插入新块。
    - 块已存在且模式集合一致 → 什么都不做(以"模式集合"而非"文本"
      比较,避免段间空行差异触发误更新)。
    - 块已存在但需要同步 → 原地更新块。
    - 块损坏(header 在但 footer 缺失) → 拒绝修改,等用户手工修复。
    - ``.git/info/exclude`` 含历史 iar 条目 → 返回 ``info_exclude_hint=True``,
      由调用方负责打印提示。

    Args:
        options: 控制仓库根、dry-run、是否整体跳过。

    Returns:
        :class:`GitignoreSyncResult`,描述本次同步对 ``.gitignore`` 的所有
        副作用,以及 ``.git/info/exclude`` 的检测结果。
    """
    gitignore_path = options.repo_root_path / ".gitignore"
    info_hint = _info_exclude_has_iar_entries(options.repo_root_path)

    if options.skip:
        return GitignoreSyncResult(
            gitignore_path=gitignore_path,
            block_inserted=False,
            block_updated=False,
            entries_added=(),
            entries_skipped_external=(),
            info_exclude_hint=info_hint,
            dry_run=options.dry_run,
            skipped=True,
        )

    existing_text = _read_gitignore_text(gitignore_path)
    _, existing_block, _ = _parse_gitignore_blocks(existing_text)
    if (
        existing_text
        and existing_block is None
        and (
            GITIGNORE_BLOCK_HEADER in existing_text
            and GITIGNORE_BLOCK_FOOTER not in existing_text
        )
    ):
        # 块被破坏,不动文件,让用户自己处理。
        return GitignoreSyncResult(
            gitignore_path=gitignore_path,
            block_inserted=False,
            block_updated=False,
            entries_added=(),
            entries_skipped_external=(),
            info_exclude_hint=info_hint,
            dry_run=options.dry_run,
            skipped=False,
        )

    external_patterns = _patterns_outside_block(existing_text)
    new_block_text, entries_added, entries_skipped_external = _build_block_text(
        external_patterns
    )
    existing_block_patterns = _block_patterns(existing_block)
    new_block_patterns = set(entries_added)

    block_inserted = False
    block_updated = False
    if existing_block is None:
        if not entries_added:
            # 没有新条目要写(全部已在块外)。仍把 skipped_external 透传
            # 给调用方用于提示,但不写入空标记块。
            return GitignoreSyncResult(
                gitignore_path=gitignore_path,
                block_inserted=False,
                block_updated=False,
                entries_added=(),
                entries_skipped_external=entries_skipped_external,
                info_exclude_hint=info_hint,
                dry_run=options.dry_run,
                skipped=False,
            )
        if existing_text and not existing_text.endswith("\n"):
            existing_text = existing_text + "\n"
        if existing_text:
            new_text = existing_text + "\n" + new_block_text
        else:
            new_text = new_block_text
        block_inserted = True
    else:
        if existing_block_patterns == new_block_patterns:
            return GitignoreSyncResult(
                gitignore_path=gitignore_path,
                block_inserted=False,
                block_updated=False,
                entries_added=(),
                entries_skipped_external=entries_skipped_external,
                info_exclude_hint=info_hint,
                dry_run=options.dry_run,
                skipped=False,
            )
        new_text = _replace_iar_block(existing_text, new_block_text)
        block_updated = True

    if not options.dry_run:
        gitignore_path.write_text(new_text, encoding="utf-8")

    return GitignoreSyncResult(
        gitignore_path=gitignore_path,
        block_inserted=block_inserted,
        block_updated=block_updated,
        entries_added=entries_added,
        entries_skipped_external=entries_skipped_external,
        info_exclude_hint=info_hint,
        dry_run=options.dry_run,
        skipped=False,
    )


__all__ = [
    "GITIGNORE_BLOCK_FOOTER",
    "GITIGNORE_BLOCK_HEADER",
    "GitignoreSyncOptions",
    "GitignoreSyncResult",
    "IAR_GITIGNORE_SECTIONS",
    "ensure_gitignore_entries",
]
