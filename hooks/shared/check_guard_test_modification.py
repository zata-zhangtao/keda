#!/usr/bin/env python3
"""守卫测试修改保护 hook。

当 ``tests/guards/`` 下的文件被提交时，要求提交者通过 ``GUARD_UPDATE_ACK=1``
显式确认这是有意的规则更新，而非为了让失败的守卫测试通过。

pre-commit local hook 配置::

    - id: check-guard-test-modification
      name: Check guard test modification
      entry: uv run python hooks/shared/check_guard_test_modification.py
      language: system
      pass_filenames: false
      always_run: true
"""

from __future__ import annotations

import os
import subprocess
import sys


def _get_staged_guard_files() -> list[str]:
    """返回暂存区内发生变更的守卫测试文件。

    Returns:
        相对于仓库根目录的守卫测试文件路径。

    Raises:
        RuntimeError: 无法读取 Git 暂存区时抛出。
    """
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "--",
            "tests/guards",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        error_message = result.stderr.strip() or "未知 Git 错误"
        raise RuntimeError(error_message)
    return [file_path for file_path in result.stdout.splitlines() if file_path]


def main() -> int:
    """检查守卫测试修改是否经过显式确认。

    Returns:
        0 表示允许提交（无守卫测试改动，或已通过 ``GUARD_UPDATE_ACK`` 确认）；
        1 表示拒绝提交。
    """
    try:
        guarded_files = _get_staged_guard_files()
    except RuntimeError as error:
        print(f"⛔ 无法检查守卫测试暂存状态：{error}", file=sys.stderr)
        return 1

    if not guarded_files:
        return 0

    if os.environ.get("GUARD_UPDATE_ACK", "").strip():
        print(
            "GUARD_UPDATE_ACK 已设置：确认本次修改 tests/guards/ 是有意的规则更新。\n"
            "请确保已同步更新对应的约定文档与 tests/guards/README.md。"
        )
        return 0

    print("⛔ 检测到对守卫测试目录 tests/guards/ 的修改：\n", file=sys.stderr)
    for file_path in guarded_files:
        print(f"  - {file_path}", file=sys.stderr)
    print(
        "\n守卫测试守护仓库约定，失败时应修复触发它的源代码或配置，而不是修改"
        "测试本身让失败消失。\n"
        "如果你确实要更新守卫规则本身（约定变更），请先同步对应约定文档，"
        "然后设置环境变量再提交：\n"
        "  GUARD_UPDATE_ACK=1 git commit ...\n"
        "AI 代理默认不应设置该变量；如因任务需要修改守卫测试，请先与人类确认。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
