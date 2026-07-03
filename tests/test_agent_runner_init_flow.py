"""Tests for ``init_flow.copy_bundled_skills`` and helpers."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

from backend.engines.agent_runner.init_flow import (
    DEFAULT_BUNDLED_SKILL_NAMES,
    BundledSkillCopyOptions,
    SKILL_PACKAGE_NAME,
    UnknownBundledSkillError,
    compute_target_skill_directory_hash,
    copy_bundled_skills,
    format_skill_copy_plan,
    format_skill_copy_summary,
    list_bundled_skill_names,
    plan_skill_copy,
    resolve_target_skills_root,
)


# ---------------------------------------------------------------------------
# Package layout
# ---------------------------------------------------------------------------


def test_skill_package_exposes_prd_and_code_reviewer() -> None:
    """The bundled skills package must expose prd + code-reviewer directories."""
    bundled_names = list_bundled_skill_names()
    assert "prd" in bundled_names
    assert "code-reviewer" in bundled_names


def test_default_bundled_skill_names_cover_required_skills() -> None:
    """The default skill list must include prd + code-reviewer."""
    assert "prd" in DEFAULT_BUNDLED_SKILL_NAMES
    assert "code-reviewer" in DEFAULT_BUNDLED_SKILL_NAMES


def test_resolve_target_skills_root_is_repo_dot_claude_skills(tmp_path: Path) -> None:
    """``resolve_target_skills_root`` must return ``<repo>/.claude/skills``."""
    target_root = resolve_target_skills_root(tmp_path)
    assert target_root == (tmp_path / ".claude" / "skills").resolve()


# ---------------------------------------------------------------------------
# Plan + hash helpers
# ---------------------------------------------------------------------------


def test_plan_skill_copy_lists_all_files() -> None:
    """Plans must include every file shipped inside the skill tree."""
    plans, _ = plan_skill_copy(repo_root_path=Path("/tmp"), skill_name="prd")
    expected_files = {
        relative_path
        for relative_path in sorted(
            str(p.relative_to(files(SKILL_PACKAGE_NAME) / "prd")).replace("\\", "/")
            for p in (files(SKILL_PACKAGE_NAME) / "prd").rglob("*")
            if p.is_file()
        )
    }
    actual_files = {plan.relative_path for plan in plans}
    assert actual_files == expected_files
    assert actual_files  # sanity: at least one file
    assert "SKILL.md" in actual_files


def test_compute_target_skill_directory_hash_missing_returns_none(
    tmp_path: Path,
) -> None:
    """A non-existent target directory must yield ``None``."""
    assert compute_target_skill_directory_hash(tmp_path / "missing") is None


def test_plan_skill_copy_unknown_skill_raises() -> None:
    """An unknown skill name must raise ``UnknownBundledSkillError``."""
    with pytest.raises(UnknownBundledSkillError):
        plan_skill_copy(repo_root_path=Path("/tmp"), skill_name="does-not-exist")


# ---------------------------------------------------------------------------
# copy_bundled_skills behaviour
# ---------------------------------------------------------------------------


def test_copy_bundled_skills_writes_new_files(tmp_path: Path) -> None:
    """First run on a fresh repo must write every bundled file."""
    result = copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path))

    assert not result.skipped
    assert set(result.copied_skills) == {"prd", "code-reviewer"}
    assert not result.skipped_identical_skills
    assert not result.overwritten_skills
    assert not result.diverged_skills

    for skill_name in ("prd", "code-reviewer"):
        target_root = tmp_path / ".claude" / "skills" / skill_name
        assert target_root.is_dir()
        bundled_root = files(SKILL_PACKAGE_NAME) / skill_name
        for source_path in bundled_root.rglob("*"):
            if not source_path.is_file():
                continue
            relative = source_path.relative_to(bundled_root).as_posix()
            target_path = target_root / relative
            assert target_path.is_file()
            assert target_path.read_bytes() == source_path.read_bytes()


def test_copy_bundled_skills_skips_identical_second_run(tmp_path: Path) -> None:
    """A second run on the same repo must report all skills identical."""
    copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path))
    second = copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path))
    assert not second.copied_skills
    assert set(second.skipped_identical_skills) == {"prd", "code-reviewer"}
    assert not second.overwritten_skills
    assert not second.diverged_skills


def test_copy_bundled_skills_diverged_without_force_keeps_local(tmp_path: Path) -> None:
    """Divergence without ``--force`` must keep the local copy and warn."""
    copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path))
    prd_target = tmp_path / ".claude" / "skills" / "prd" / "SKILL.md"
    original_bytes = prd_target.read_bytes()
    prd_target.write_text("local customisation", encoding="utf-8")

    result = copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path, force=False))

    assert not result.copied_skills
    assert "prd" in result.diverged_skills
    assert "code-reviewer" in result.skipped_identical_skills
    # Local file is preserved
    assert prd_target.read_text(encoding="utf-8") == "local customisation"
    # And the bundled content is still available for a forced retry.
    assert prd_target.read_bytes() != original_bytes


def test_copy_bundled_skills_force_overwrites_diverged(tmp_path: Path) -> None:
    """``--force`` must overwrite divergent files."""
    copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path))
    prd_target = tmp_path / ".claude" / "skills" / "prd" / "SKILL.md"
    prd_target.write_text("local customisation", encoding="utf-8")

    result = copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path, force=True))

    assert "prd" in result.overwritten_skills
    assert "prd" not in result.diverged_skills
    bundled_root = files(SKILL_PACKAGE_NAME) / "prd"
    bundled_bytes = (bundled_root / "SKILL.md").read_bytes()
    assert prd_target.read_bytes() == bundled_bytes


def test_copy_bundled_skills_skip_skips_step(tmp_path: Path) -> None:
    """``skip=True`` must skip the step entirely (no files written)."""
    result = copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path, skip=True))
    assert result.skipped
    assert not result.copied_skills
    assert not (tmp_path / ".claude" / "skills").exists()


def test_copy_bundled_skills_dry_run_does_not_write(tmp_path: Path) -> None:
    """``dry_run=True`` must return plans without touching the filesystem."""
    result = copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path, dry_run=True))

    assert result.dry_run
    assert set(result.copied_skills) == {"prd", "code-reviewer"}
    assert not (tmp_path / ".claude" / "skills").exists()


def test_copy_bundled_skills_removes_stale_files_on_force(tmp_path: Path) -> None:
    """Forced overwrite must purge files the bundled copy no longer ships."""
    copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path))
    stale_path = tmp_path / ".claude" / "skills" / "prd" / "stale.txt"
    stale_path.write_text("stale", encoding="utf-8")

    copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path, force=True))

    assert not stale_path.exists()


def test_copy_bundled_skills_raises_for_unknown_skill_name(tmp_path: Path) -> None:
    """An unknown skill name must raise ``UnknownBundledSkillError``."""
    with pytest.raises(UnknownBundledSkillError):
        copy_bundled_skills(
            BundledSkillCopyOptions(
                repo_root_path=tmp_path,
                skill_names=("prd", "does-not-exist"),
            )
        )


def test_target_hash_matches_bundled_hash_after_copy(tmp_path: Path) -> None:
    """The SHA256 of the copied tree must match the bundled aggregated hash."""
    copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path))

    for skill_name in ("prd", "code-reviewer"):
        target_root = tmp_path / ".claude" / "skills" / skill_name
        actual_hash = compute_target_skill_directory_hash(target_root)
        _, bundled_hash = plan_skill_copy(repo_root_path=tmp_path, skill_name=skill_name)
        assert actual_hash == bundled_hash, skill_name


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def test_format_skill_copy_plan_lists_bundled_names(tmp_path: Path) -> None:
    """The plan line must mention every bundled skill."""
    line = format_skill_copy_plan(tmp_path)
    assert "prd" in line
    assert "code-reviewer" in line
    assert str(tmp_path / ".claude" / "skills") in line


def test_format_skill_copy_summary_reports_copied_and_skipped(tmp_path: Path) -> None:
    """The summary must list each bucket that was touched."""
    first = copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path))
    second = copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path))

    first_lines = format_skill_copy_summary(first)
    second_lines = format_skill_copy_summary(second)

    assert any("prd" in line and "Copied" in line for line in first_lines)
    assert any("code-reviewer" in line and "Copied" in line for line in first_lines)
    assert any("up to date" in line for line in second_lines)


def test_format_skill_copy_summary_skipped_returns_empty(tmp_path: Path) -> None:
    """``skipped=True`` results must produce no summary lines."""
    result = copy_bundled_skills(BundledSkillCopyOptions(repo_root_path=tmp_path, skip=True))
    assert format_skill_copy_summary(result) == []
