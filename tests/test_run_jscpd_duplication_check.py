"""Regression tests for the incremental jscpd duplication hook logic.

只覆盖“重复是否落在改动行”的判定与显示格式化，不实际调用 jscpd 二进制，
因此不依赖 node 环境即可稳定运行。
"""

from __future__ import annotations

import sys
from pathlib import Path

# run_jscpd_duplication_check 依赖同目录的 duplication_check_utils，import 前需
# 将 hooks/shared 放到 sys.path。
_HOOKS_SHARED_PATH = Path(__file__).resolve().parents[1] / "hooks" / "shared"
if str(_HOOKS_SHARED_PATH) not in sys.path:
    sys.path.insert(0, str(_HOOKS_SHARED_PATH))

import run_jscpd_duplication_check as jscpd_hook  # noqa: E402

_SETTINGS_PATH = Path("frontend-public/app/(app)/app/settings/page.tsx")
_AGENTS_NEW_PATH = Path("frontend-public/app/(app)/app/agents/new/page.tsx")


def _scaffold_duplicate() -> dict[str, object]:
    """构造一条与实测一致的 jscpd 重复记录（secondFile 带反向 end）。"""
    return {
        "lines": 12,
        "tokens": 0,
        "firstFile": {"name": _SETTINGS_PATH.as_posix(), "start": 37, "end": 48},
        "secondFile": {"name": _AGENTS_NEW_PATH.as_posix(), "start": 29, "end": 13},
    }


def test_file_entry_line_span_uses_start_plus_lines() -> None:
    """应以可靠的 start + 顶层匹配行数推算区间，忽略反向的 end。"""
    span = jscpd_hook._file_entry_line_span({"name": "a.tsx", "start": 37, "end": 13}, 12)
    assert span == (37, 48)


def test_file_entry_line_span_falls_back_to_start_loc() -> None:
    """缺少 start 字段时应回退到 startLoc.line。"""
    span = jscpd_hook._file_entry_line_span({"name": "a.tsx", "startLoc": {"line": 5}}, 3)
    assert span == (5, 7)


def test_file_entry_line_span_returns_none_without_usable_start() -> None:
    """既无 start 也无 startLoc.line 时返回 None。"""
    assert jscpd_hook._file_entry_line_span({"name": "a.tsx"}, 12) is None


def test_ranges_intersect_detects_overlap_and_gap() -> None:
    """区间相交判定：重叠为真，完全错开为假。"""
    assert jscpd_hook._ranges_intersect((37, 48), [(40, 41)]) is True
    assert jscpd_hook._ranges_intersect((37, 48), [(7, 7), (109, 123)]) is False


def test_duplicate_ignored_when_change_is_outside_span() -> None:
    """改动没碰到重复区间（历史骨架重复）时不应判失败。"""
    candidate_changed_ranges = {_SETTINGS_PATH: [(7, 7), (109, 123)]}
    assert (
        jscpd_hook._duplicate_touches_changed_lines(_scaffold_duplicate(), candidate_changed_ranges)
        is False
    )


def test_duplicate_flagged_when_change_is_inside_span() -> None:
    """改动落在重复区间内（真正触达重复）时必须判失败。"""
    candidate_changed_ranges = {_SETTINGS_PATH: [(40, 42)]}
    assert (
        jscpd_hook._duplicate_touches_changed_lines(_scaffold_duplicate(), candidate_changed_ranges)
        is True
    )


def test_duplicate_flagged_for_new_file_without_ranges() -> None:
    """全新文件（空改动区间）整文件视为改动，重复仍应判失败。"""
    new_helper_path = Path("frontend-public/lib/new_helper.ts")
    existing_path = Path("frontend-public/lib/existing.ts")
    duplicate_entry = {
        "lines": 8,
        "firstFile": {"name": new_helper_path.as_posix(), "start": 1, "end": 8},
        "secondFile": {"name": existing_path.as_posix(), "start": 3, "end": 10},
    }
    assert (
        jscpd_hook._duplicate_touches_changed_lines(duplicate_entry, {new_helper_path: []}) is True
    )


def test_duplicate_ignored_when_no_candidate_participates() -> None:
    """重复的两侧都不是候选文件时不应判失败。"""
    duplicate_entry = {
        "lines": 8,
        "firstFile": {"name": "a.ts", "start": 1, "end": 8},
        "secondFile": {"name": "b.ts", "start": 3, "end": 10},
    }
    assert (
        jscpd_hook._duplicate_touches_changed_lines(duplicate_entry, {Path("c.ts"): [(1, 5)]})
        is False
    )


def test_format_duplicate_message_uses_reliable_span() -> None:
    """输出应显示修正后的区间（29-40），而非 jscpd 反向 end 的 29-13。"""
    message = jscpd_hook._format_duplicate_message(_scaffold_duplicate())
    assert f"{_SETTINGS_PATH.as_posix()}:37-48" in message
    assert f"{_AGENTS_NEW_PATH.as_posix()}:29-40" in message
    assert "12 lines" in message
