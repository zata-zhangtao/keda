"""Smoke tests for the preview_env CLI script."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "preview_env.py"


@pytest.fixture
def clean_preview_env():
    """Remove PREVIEW_* environment variables before each test."""
    prefix = "PREVIEW_"
    original = {
        key: value for key, value in os.environ.items() if key.startswith(prefix)
    }
    for key in list(os.environ.keys()):
        if key.startswith(prefix):
            del os.environ[key]
    yield
    for key in list(os.environ.keys()):
        if key.startswith(prefix):
            del os.environ[key]
    os.environ.update(original)


def _run_script(args: list[str], extra_env: dict[str, str] | None = None) -> str:
    """Run preview_env.py and return stdout text."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
    )
    return result.stdout


def test_preview_env_script_outputs_required_keys(clean_preview_env):
    """Script stdout must contain all required preview keys."""
    stdout = _run_script(["--pr", "123", "--sha", "deadbeefcafe"])

    required_keys = [
        "PREVIEW_DOMAIN=pr-123.preview.example.com",
        "COMPOSE_PROJECT_NAME=keda-pr-123",
        "BACKEND_IMAGE=ghcr.io/zata-zhangtao/keda-backend:deadbee",
        "FRONTEND_IMAGE=ghcr.io/zata-zhangtao/keda-frontend:deadbee",
    ]
    for required in required_keys:
        assert required in stdout, f"Missing expected output: {required}"


def test_preview_env_script_env_override(clean_preview_env):
    """Script must honor PREVIEW_* environment overrides."""
    stdout = _run_script(
        ["--pr", "123", "--sha", "deadbeefcafe"],
        extra_env={
            "PREVIEW_BASE_DOMAIN": "dev.example.com",
            "PREVIEW_PROJECT_SLUG": "demo",
        },
    )

    assert "PREVIEW_DOMAIN=pr-123.dev.example.com" in stdout
    assert "COMPOSE_PROJECT_NAME=demo-pr-123" in stdout


def test_preview_env_script_appends_github_env(clean_preview_env, tmp_path):
    """When GITHUB_ENV is set, the script must append values to that file."""
    github_env = tmp_path / "github_env"
    github_env.write_text("EXISTING=value\n", encoding="utf-8")

    _run_script(
        ["--pr", "123", "--sha", "deadbeefcafe"],
        extra_env={"GITHUB_ENV": str(github_env)},
    )

    content = github_env.read_text(encoding="utf-8")
    assert "EXISTING=value" in content
    assert "PREVIEW_DOMAIN=pr-123.preview.example.com" in content
    assert "COMPOSE_PROJECT_NAME=keda-pr-123" in content


def test_preview_env_script_requires_pr_and_sha(clean_preview_env):
    """Script must fail when required arguments are missing."""
    with pytest.raises(subprocess.CalledProcessError):
        _run_script(["--pr", "123"])
