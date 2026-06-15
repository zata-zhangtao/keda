"""Tests for preview deployment environment derivation."""

from __future__ import annotations

import pytest

from backend.core.use_cases.preview_deployment import render_preview_env


class _FakePreviewSettings:
    """Minimal stand-in for PreviewSettings that satisfies the protocol."""

    enabled = True
    base_domain = "preview.example.com"
    project_slug = "keda"
    app_dir_root = "/opt/preview"
    registry_host = "ghcr.io"
    registry_namespace = "zata-zhangtao"
    traefik_network = "traefik"
    url_scheme = "https"
    subdomain_template = "pr-{pr_number}.{base_domain}"
    compose_template = "{project_slug}-pr-{pr_number}"


def test_render_preview_env_returns_expected_keys():
    """All expected environment keys must be present."""
    preview = _FakePreviewSettings()
    env_vars = render_preview_env(preview, pr_number=123, commit_sha="deadbeefcafe")

    expected_keys = {
        "PREVIEW_DOMAIN",
        "COMPOSE_PROJECT_NAME",
        "APP_DIR",
        "BACKEND_IMAGE",
        "FRONTEND_IMAGE",
        "REGISTRY_HOST",
        "REGISTRY_NAMESPACE",
        "TRAEFIK_NETWORK",
        "TRAEFIK_ROUTER_NAME",
        "TRAEFIK_SERVICE_NAME",
        "PREVIEW_URL_SCHEME",
    }
    assert set(env_vars.keys()) == expected_keys


def test_render_preview_env_derives_domain_and_project_name():
    """Domain and project name must be derived from templates and PR number."""
    preview = _FakePreviewSettings()
    env_vars = render_preview_env(preview, pr_number=42, commit_sha="abcd1234")

    assert env_vars["PREVIEW_DOMAIN"] == "pr-42.preview.example.com"
    assert env_vars["COMPOSE_PROJECT_NAME"] == "keda-pr-42"
    assert env_vars["APP_DIR"] == "/opt/preview/keda-pr-42"


def test_render_preview_env_uses_short_sha_for_images():
    """Image tags must use the first 8 characters of the commit SHA."""
    preview = _FakePreviewSettings()
    env_vars = render_preview_env(preview, pr_number=7, commit_sha="deadbeefcafebabe")

    assert env_vars["BACKEND_IMAGE"] == "ghcr.io/zata-zhangtao/keda-backend:deadbeef"
    assert env_vars["FRONTEND_IMAGE"] == "ghcr.io/zata-zhangtao/keda-frontend:deadbeef"


def test_render_preview_env_preserves_short_sha():
    """Short SHA input must not be truncated further."""
    preview = _FakePreviewSettings()
    env_vars = render_preview_env(preview, pr_number=1, commit_sha="abc1234")

    assert env_vars["BACKEND_IMAGE"].endswith(":abc1234")


def test_render_preview_env_router_service_names_match_project():
    """Traefik router/service names must equal the compose project name."""
    preview = _FakePreviewSettings()
    env_vars = render_preview_env(preview, pr_number=99, commit_sha="sha99sha")

    assert env_vars["TRAEFIK_ROUTER_NAME"] == env_vars["COMPOSE_PROJECT_NAME"]
    assert env_vars["TRAEFIK_SERVICE_NAME"] == env_vars["COMPOSE_PROJECT_NAME"]


def test_render_preview_env_url_scheme():
    """URL scheme must be forwarded from settings."""
    preview = _FakePreviewSettings()
    preview.url_scheme = "http"
    env_vars = render_preview_env(preview, pr_number=5, commit_sha="sha5")

    assert env_vars["PREVIEW_URL_SCHEME"] == "http"


@pytest.mark.parametrize("pr_number", [0, 1, 9999])
def test_render_preview_env_handles_various_pr_numbers(pr_number):
    """Derivation must work for common PR number ranges."""
    preview = _FakePreviewSettings()
    env_vars = render_preview_env(preview, pr_number=pr_number, commit_sha="deadbeef")

    assert env_vars["PREVIEW_DOMAIN"] == f"pr-{pr_number}.preview.example.com"
    assert env_vars["COMPOSE_PROJECT_NAME"] == f"keda-pr-{pr_number}"
