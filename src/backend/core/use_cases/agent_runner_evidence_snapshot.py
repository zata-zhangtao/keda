"""把 builder 的最终树证据保护起来,不被 independent verifier 的复跑覆盖。

verifier 的 prompt 要求它"自己跑真实入口、并逐个跑 negative control",而它能跑的
就是 builder 写进 evidence manifest 的那些 capture 脚本——那些脚本会把输出写回同一
批 ``rv-*.txt``。于是 verifier 一跑,builder 已经过门禁的证据就被它自己的复跑结果
覆盖;verifier 被超时杀掉时更糟:证据目录停在它最后一个 negative control 的破坏态
(比如故意改红的测试输出),而证据门禁早就通过了,这份自相矛盾的证据会照原样发布给
人审。

因此在 verifier 启动前给证据目录做快照,verifier 结束后(拿到 verdict 或抛异常都
一样)把被改写、被删除的文件恢复回 builder 版本。verifier 自己的产物(如
``verifier-response.txt``)由 ``keep_filenames`` 排除在恢复之外。

保护是"尽力而为"的旁路能力:快照或恢复自身出错只记警告,绝不改变门禁结论。
"""

from __future__ import annotations

import filecmp
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.models.agent_runner import AppConfig
from backend.core.use_cases.agent_runner_validation import evidence_dir_path

_logger = logging.getLogger(__name__)

_SNAPSHOT_DIR_PREFIX = "iar-evidence-snapshot-"


@dataclass(frozen=True)
class EvidenceSnapshot:
    """证据目录在 verifier 启动前的一份副本。

    ``snapshot_dir`` 落在系统临时目录而不是 worktree 内,免得快照自己变成证据
    或进入 diff。
    """

    evidence_dir: Path
    snapshot_dir: Path


def snapshot_evidence_dir(worktree_path: Path, config: AppConfig) -> EvidenceSnapshot | None:
    """给证据目录拍一份临时快照。

    Returns:
        ``EvidenceSnapshot``;证据目录不存在或拷贝失败时返回 ``None``(调用方
        据此跳过恢复,门禁行为不变)。
    """
    evidence_dir = evidence_dir_path(worktree_path, config)
    if not evidence_dir.is_dir():
        return None
    try:
        snapshot_dir = Path(tempfile.mkdtemp(prefix=_SNAPSHOT_DIR_PREFIX))
        shutil.copytree(evidence_dir, snapshot_dir, dirs_exist_ok=True)
    except OSError as snapshot_error:
        _logger.warning(
            "Could not snapshot evidence dir %s before the verifier ran: %s",
            evidence_dir,
            snapshot_error,
        )
        return None
    return EvidenceSnapshot(evidence_dir=evidence_dir, snapshot_dir=snapshot_dir)


def restore_evidence_snapshot(
    snapshot: EvidenceSnapshot | None,
    *,
    keep_filenames: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """把被 verifier 改写或删除的证据文件恢复回快照版本。

    只恢复"快照里有、现在内容不同或已丢失"的文件。verifier 新建的文件保持原样
    ——删掉别人刚写出来的文件比留下它风险更大,而且新文件本身就是"verifier 动过
    证据目录"的可见线索,恢复列表会一并记进日志。

    Args:
        snapshot: :func:`snapshot_evidence_dir` 的返回值;``None`` 时直接返回空。
        keep_filenames: 不恢复的文件名(按文件名匹配,不含目录)。verifier 自己的
            响应日志属于这一类:它在 verifier 之后才写,恢复会把它盖回旧内容。

    Returns:
        实际恢复的文件相对路径,按字典序排列。
    """
    if snapshot is None:
        return ()
    try:
        restored_relative_paths = _restore_changed_files(snapshot, keep_filenames)
    except OSError as restore_error:
        _logger.warning(
            "Could not restore builder evidence into %s after the verifier ran: %s",
            snapshot.evidence_dir,
            restore_error,
        )
        return ()
    finally:
        shutil.rmtree(snapshot.snapshot_dir, ignore_errors=True)
    if restored_relative_paths:
        _logger.warning(
            "Independent verifier rewrote %d builder evidence file(s) in %s; "
            "restored the builder version(s): %s",
            len(restored_relative_paths),
            snapshot.evidence_dir,
            ", ".join(restored_relative_paths),
        )
    return restored_relative_paths


def _restore_changed_files(
    snapshot: EvidenceSnapshot,
    keep_filenames: tuple[str, ...],
) -> tuple[str, ...]:
    """逐个文件比对快照与现状,把有差异的拷回去并返回恢复清单。"""
    restored_relative_paths: list[str] = []
    for snapshot_file_path in sorted(snapshot.snapshot_dir.rglob("*")):
        if not snapshot_file_path.is_file():
            continue
        relative_path = snapshot_file_path.relative_to(snapshot.snapshot_dir)
        if relative_path.name in keep_filenames:
            continue
        current_file_path = snapshot.evidence_dir / relative_path
        if current_file_path.is_file() and filecmp.cmp(
            snapshot_file_path, current_file_path, shallow=False
        ):
            continue
        current_file_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot_file_path, current_file_path)
        restored_relative_paths.append(relative_path.as_posix())
    return tuple(restored_relative_paths)
