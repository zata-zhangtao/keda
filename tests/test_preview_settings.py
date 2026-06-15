"""Tests for PreviewSettings configuration loading."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from backend.infrastructure.config.settings import PreviewSettings


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


def test_preview_settings_defaults():
    """Defaults must match the documented placeholder values."""
    settings = PreviewSettings()

    assert settings.enabled is False
    assert settings.base_domain == "preview.example.com"
    assert settings.project_slug == "keda"
    assert settings.app_dir_root == "/opt/preview"
    assert settings.registry_host == "ghcr.io"
    assert settings.registry_namespace == "zata-zhangtao"
    assert settings.traefik_network == "traefik"
    assert settings.url_scheme == "https"
    assert settings.subdomain_template == "pr-{pr_number}.{base_domain}"
    assert settings.compose_template == "{project_slug}-pr-{pr_number}"


def test_preview_settings_env_prefix_overrides(clean_preview_env):
    """PREVIEW_* environment variables must override defaults."""
    with patch.dict(
        os.environ,
        {
            "PREVIEW_ENABLED": "true",
            "PREVIEW_BASE_DOMAIN": "dev.example.com",
            "PREVIEW_PROJECT_SLUG": "demo",
        },
    ):
        settings = PreviewSettings()

    assert settings.enabled is True
    assert settings.base_domain == "dev.example.com"
    assert settings.project_slug == "demo"


def test_preview_settings_enabled_string_parsing(clean_preview_env):
    """Enabled must be parsed from common truthy string forms."""
    with patch.dict(os.environ, {"PREVIEW_ENABLED": "1"}):
        settings = PreviewSettings()
    assert settings.enabled is True

    with patch.dict(os.environ, {"PREVIEW_ENABLED": "false"}):
        settings = PreviewSettings()
    assert settings.enabled is False


def test_app_settings_aggregates_preview():
    """AppSettings must expose a preview attribute of type PreviewSettings."""
    from backend.infrastructure.config.settings import AppSettings

    settings = AppSettings()
    assert isinstance(settings.preview, PreviewSettings)
