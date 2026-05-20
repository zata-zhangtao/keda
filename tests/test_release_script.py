"""Tests for release archive exclusion rules."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_release_module() -> ModuleType:
    """Load the release script module from its file path."""

    release_script_path = Path(__file__).resolve().parents[1] / "scripts" / "release.py"
    release_module_spec = importlib.util.spec_from_file_location(
        "release_script_module",
        release_script_path,
    )
    assert release_module_spec is not None
    assert release_module_spec.loader is not None

    release_module = importlib.util.module_from_spec(release_module_spec)
    release_module_spec.loader.exec_module(release_module)
    return release_module


def test_should_exclude_sensitive_and_scratch_files() -> None:
    """Exclude real env files, local config, and planning scratch files."""

    release_module = _load_release_module()

    assert release_module._should_exclude(".env") is True
    assert release_module._should_exclude("features/demo/.env") is True
    assert release_module._should_exclude(".claude/settings.local.json") is True
    assert release_module._should_exclude("findings.md") is True
    assert release_module._should_exclude("progress.md") is True
    assert release_module._should_exclude("task_plan.md") is True


def test_should_keep_template_examples() -> None:
    """Keep example env files and normal project files in release archives."""

    release_module = _load_release_module()

    assert release_module._should_exclude(".env.example") is False
    assert release_module._should_exclude("env/.env.example") is False
    assert release_module._should_exclude("docs/guides/deployment.md") is False
