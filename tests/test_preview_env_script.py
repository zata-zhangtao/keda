"""Smoke tests for the preview_env CLI script."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "preview_env.py"

PREVIEW_TOML_TEMPLATE = """\
[preview]
enabled = true
base_domain = "{base_domain}"
project_slug = "{project_slug}"
app_dir_root = "{app_dir_root}"
registry_host = "{registry_host}"
registry_namespace = "{registry_namespace}"
traefik_network = "{traefik_network}"
url_scheme = "{url_scheme}"
subdomain_template = "{subdomain_template}"
compose_template = "{compose_template}"
"""


def _write_preview_config(tmp_path: Path, **overrides: object) -> Path:
    """Materialize a deterministic [preview] config.toml in tmp_path."""
    defaults: dict[str, object] = {
        "base_domain": "preview.example.com",
        "project_slug": "keda",
        "app_dir_root": "/opt/preview",
        "registry_host": "ghcr.io",
        "registry_namespace": "zata-zhangtao",
        "traefik_network": "traefik",
        "url_scheme": "https",
        "subdomain_template": "pr-{pr_number}.{base_domain}",
        "compose_template": "{project_slug}-pr-{pr_number}",
    }
    defaults.update(overrides)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        PREVIEW_TOML_TEMPLATE.format(**defaults),
        encoding="utf-8",
    )
    return config_path


def _expected_env_lines(pr_number: int, sha: str, **overrides: object) -> list[str]:
    """Compute the canonical KEY=VALUE lines the script should print."""
    defaults: dict[str, object] = {
        "base_domain": overrides.get("base_domain", "preview.example.com"),
        "project_slug": overrides.get("project_slug", "keda"),
        "app_dir_root": overrides.get("app_dir_root", "/opt/preview"),
        "registry_host": overrides.get("registry_host", "ghcr.io"),
        "registry_namespace": overrides.get("registry_namespace", "zata-zhangtao"),
        "traefik_network": overrides.get("traefik_network", "traefik"),
        "url_scheme": overrides.get("url_scheme", "https"),
        "subdomain_template": overrides.get("subdomain_template", "pr-{pr_number}.{base_domain}"),
        "compose_template": overrides.get("compose_template", "{project_slug}-pr-{pr_number}"),
    }
    short_sha = sha[:8] if len(sha) > 8 else sha
    subdomain = str(defaults["subdomain_template"]).format(
        pr_number=pr_number,
        base_domain=defaults["base_domain"],
    )
    compose_project_name = str(defaults["compose_template"]).format(
        project_slug=defaults["project_slug"],
        pr_number=pr_number,
    )
    backend_image = (
        f"{defaults['registry_host']}/{defaults['registry_namespace']}/"
        f"{defaults['project_slug']}-backend:{short_sha}"
    )
    frontend_image = (
        f"{defaults['registry_host']}/{defaults['registry_namespace']}/"
        f"{defaults['project_slug']}-frontend:{short_sha}"
    )
    return [
        f"PREVIEW_DOMAIN={subdomain}",
        f"COMPOSE_PROJECT_NAME={compose_project_name}",
        f"APP_DIR={defaults['app_dir_root']}/{compose_project_name}",
        f"BACKEND_IMAGE={backend_image}",
        f"FRONTEND_IMAGE={frontend_image}",
        f"REGISTRY_HOST={defaults['registry_host']}",
        f"REGISTRY_NAMESPACE={defaults['registry_namespace']}",
        f"TRAEFIK_NETWORK={defaults['traefik_network']}",
        f"TRAEFIK_ROUTER_NAME={compose_project_name}",
        f"TRAEFIK_SERVICE_NAME={compose_project_name}",
        f"PREVIEW_URL_SCHEME={defaults['url_scheme']}",
    ]


@pytest.fixture
def clean_preview_env():
    """Remove PREVIEW_* environment variables before each test."""
    prefix = "PREVIEW_"
    original = {key: value for key, value in os.environ.items() if key.startswith(prefix)}
    for key in list(os.environ.keys()):
        if key.startswith(prefix):
            del os.environ[key]
    yield
    for key in list(os.environ.keys()):
        if key.startswith(prefix):
            del os.environ[key]
    os.environ.update(original)


def _run_script(
    args: list[str],
    cwd: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run preview_env.py inside ``cwd`` so [preview] is read from there."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        check=True,
    )


def test_preview_env_script_outputs_required_keys(clean_preview_env, tmp_path):
    """Script stdout must contain every preview key derived from [preview]."""
    _write_preview_config(tmp_path)

    result = _run_script(["--pr", "123", "--sha", "deadbeefcafe"], cwd=tmp_path)
    stdout = result.stdout

    expected = _expected_env_lines(pr_number=123, sha="deadbeefcafe")
    for line in expected:
        assert line in stdout, f"Missing expected output: {line}"

    # Script must print every line declared in render_preview_env.
    assert len(stdout.strip().splitlines()) == len(expected)


def test_preview_env_script_env_override(clean_preview_env, tmp_path):
    """Script must honor PREVIEW_* environment overrides over [preview]."""
    _write_preview_config(
        tmp_path,
        base_domain="preview.example.com",
        project_slug="keda",
    )

    result = _run_script(
        ["--pr", "123", "--sha", "deadbeefcafe"],
        cwd=tmp_path,
        extra_env={
            "PREVIEW_BASE_DOMAIN": "dev.example.com",
            "PREVIEW_PROJECT_SLUG": "demo",
        },
    )

    assert "PREVIEW_DOMAIN=pr-123.dev.example.com" in result.stdout
    assert "COMPOSE_PROJECT_NAME=demo-pr-123" in result.stdout


def test_preview_env_script_appends_github_env(clean_preview_env, tmp_path):
    """When GITHUB_ENV is set, the script must append values to that file."""
    _write_preview_config(tmp_path)

    github_env = tmp_path / "github_env"
    github_env.write_text("EXISTING=value\n", encoding="utf-8")

    _run_script(
        ["--pr", "123", "--sha", "deadbeefcafe"],
        cwd=tmp_path,
        extra_env={"GITHUB_ENV": str(github_env)},
    )

    content = github_env.read_text(encoding="utf-8")
    assert "EXISTING=value" in content

    expected = _expected_env_lines(pr_number=123, sha="deadbeefcafe")
    for line in expected:
        assert line in content, f"Missing expected line: {line}"


def test_preview_env_script_requires_pr_and_sha(clean_preview_env, tmp_path):
    """Script must fail when required arguments are missing."""
    _write_preview_config(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        _run_script(["--pr", "123"], cwd=tmp_path)


def test_preview_env_script_reads_alternate_config(clean_preview_env, tmp_path):
    """The script must reflect any field overridden in [preview]."""
    _write_preview_config(
        tmp_path,
        base_domain="staging.yewmoon.fun",
        project_slug="alt-project",
        app_dir_root="/srv/preview",
        registry_namespace="alt-owner",
    )

    result = _run_script(["--pr", "7", "--sha", "abcdef1234567890"], cwd=tmp_path)

    expected = _expected_env_lines(
        pr_number=7,
        sha="abcdef1234567890",
        base_domain="staging.yewmoon.fun",
        project_slug="alt-project",
        app_dir_root="/srv/preview",
        registry_namespace="alt-owner",
    )
    for line in expected:
        assert line in result.stdout, f"Missing expected output: {line}"
