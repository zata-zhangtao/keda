"""Regression tests for the duplication-check shared git helpers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# duplication_check_utils 是 hooks/shared 下的独立脚本模块，import 前需将该目录
# 放到 sys.path，与其它 hook 测试保持一致的加载方式。
_HOOKS_SHARED_PATH = Path(__file__).resolve().parents[1] / "hooks" / "shared"
if str(_HOOKS_SHARED_PATH) not in sys.path:
    sys.path.insert(0, str(_HOOKS_SHARED_PATH))

import duplication_check_utils as duplication_utils  # noqa: E402


def _run_git(repo_path: Path, *git_args: str) -> None:
    """在给定仓库根目录执行一条 git 命令（失败即抛错）。"""
    subprocess.run(
        ["git", *git_args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture
def committed_repo(tmp_path: Path) -> Path:
    """创建一个含单次提交（20 行样本文件）的临时 git 仓库并返回其根目录。"""
    _run_git(tmp_path, "init", "-q")
    _run_git(tmp_path, "config", "user.email", "test@example.com")
    _run_git(tmp_path, "config", "user.name", "Test Runner")
    sample_file = tmp_path / "src" / "sample.py"
    sample_file.parent.mkdir(parents=True, exist_ok=True)
    sample_file.write_text(
        "\n".join(f"line_{line_number}" for line_number in range(1, 21)) + "\n",
        encoding="utf-8",
    )
    _run_git(tmp_path, "add", "-A")
    _run_git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_parse_new_side_line_ranges_reads_added_hunks_and_skips_deletions() -> None:
    """新侧行区间应正确解析，纯删除 hunk（新侧行数为 0）应被跳过。"""
    diff_text = "\n".join(
        [
            "diff --git a/x b/x",
            "@@ -1 +1 @@",
            "@@ -10,0 +11,3 @@",
            "@@ -20,2 +23,0 @@",
        ]
    )
    assert duplication_utils._parse_new_side_line_ranges(diff_text) == [(1, 1), (11, 13)]


def test_changed_line_ranges_returns_modified_new_side(committed_repo: Path) -> None:
    """修改工作树某一行后，应返回该行对应的新侧区间。"""
    sample_file = committed_repo / "src" / "sample.py"
    sample_lines = sample_file.read_text(encoding="utf-8").splitlines()
    sample_lines[14] = "line_15_modified"
    sample_file.write_text("\n".join(sample_lines) + "\n", encoding="utf-8")

    changed_ranges = duplication_utils.changed_line_ranges(committed_repo, Path("src/sample.py"))

    assert changed_ranges == [(15, 15)]


def test_changed_line_ranges_empty_when_unmodified(committed_repo: Path) -> None:
    """未改动的已跟踪文件应返回空区间（调用方据此按整文件处理）。"""
    changed_ranges = duplication_utils.changed_line_ranges(committed_repo, Path("src/sample.py"))

    assert changed_ranges == []


def test_changed_line_ranges_falls_back_to_staged_diff(committed_repo: Path) -> None:
    """工作树已与 HEAD 一致但索引仍有暂存改动时，应回退到 --cached 差异。"""
    sample_file = committed_repo / "src" / "sample.py"
    original_text = sample_file.read_text(encoding="utf-8")
    sample_lines = original_text.splitlines()
    sample_lines[2] = "line_3_modified"
    sample_file.write_text("\n".join(sample_lines) + "\n", encoding="utf-8")
    _run_git(committed_repo, "add", "src/sample.py")
    # 把工作树恢复到 HEAD：此时 git diff HEAD 为空，只有 --cached 能看到改动。
    sample_file.write_text(original_text, encoding="utf-8")

    changed_ranges = duplication_utils.changed_line_ranges(committed_repo, Path("src/sample.py"))

    assert changed_ranges == [(3, 3)]
