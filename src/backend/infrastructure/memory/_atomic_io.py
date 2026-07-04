"""记忆文件系统写入的公共原子工具。

统一封装 ``tmp 文件 + os.replace`` 路径，让 ``infrastructure/memory/``
下的所有 store 都走同一套原子落盘实现。设计要点：

- 临时文件与目标文件**同目录**，确保 ``os.replace`` 在 POSIX 下是
  ``rename(2)`` 原子操作（跨文件系统 rename 会退化为 copy + delete，
  失去原子语义）。
- 写入完成后 ``fsync`` 临时文件再 ``os.replace``，减少掉电半写的可能性。
- 失败时主动清理临时文件，避免堆积。

行为契约：当多个进程或线程并发调用本工具写入同一目标时，结果
等于"其中一次成功落盘的全部内容"，即 ``last-write-wins``。该语义与
PRD §6 D-04 一致（共享目录并发写仅要求不产生半写损坏文件）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(target_path: str | Path, content: str) -> Path:
    """Atomically replace *target_path* with *content* (UTF-8 text).

    Args:
        target_path: 目标文件路径；其父目录会被 ``mkdir -p`` 创建。
        content: 待写入的文本内容。

    Returns:
        落盘后的目标文件绝对路径（与 ``target_path.resolve()`` 一致）。

    Raises:
        OSError: 任何文件系统写入失败；临时文件在异常路径上被清理。
    """
    path = Path(target_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


__all__ = ["atomic_write_text"]
