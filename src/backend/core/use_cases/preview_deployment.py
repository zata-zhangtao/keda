"""Preview deployment environment derivation logic.

Pure functions used by CI scripts to derive preview stack names,
domains and image references from non-sensitive project configuration.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PreviewSettings(Protocol):
    """Structural type for preview configuration consumed by this module."""

    enabled: bool
    base_domain: str
    project_slug: str
    app_dir_root: str
    registry_host: str
    registry_namespace: str
    traefik_network: str
    url_scheme: str
    subdomain_template: str
    compose_template: str


def render_preview_env(
    preview: PreviewSettings,
    pr_number: int,
    commit_sha: str,
) -> dict[str, str]:
    """Derive non-sensitive preview environment values from settings.

    Args:
        preview: Project preview configuration.
        pr_number: Pull request number.
        commit_sha: Head commit SHA (shortened for image tags).

    Returns:
        Dictionary of environment key/value pairs consumed by the preview
        Docker Compose stack and the GitHub Actions workflow.
    """
    short_sha = _shorten_sha(commit_sha)
    subdomain = preview.subdomain_template.format(
        pr_number=pr_number,
        base_domain=preview.base_domain,
    )
    compose_project_name = preview.compose_template.format(
        project_slug=preview.project_slug,
        pr_number=pr_number,
    )
    preview_domain = f"{subdomain}"
    app_dir = f"{preview.app_dir_root}/{compose_project_name}"
    backend_image = (
        f"{preview.registry_host}/{preview.registry_namespace}/"
        f"{preview.project_slug}-backend:{short_sha}"
    )
    frontend_image = (
        f"{preview.registry_host}/{preview.registry_namespace}/"
        f"{preview.project_slug}-frontend:{short_sha}"
    )

    return {
        "PREVIEW_DOMAIN": preview_domain,
        "COMPOSE_PROJECT_NAME": compose_project_name,
        "APP_DIR": app_dir,
        "BACKEND_IMAGE": backend_image,
        "FRONTEND_IMAGE": frontend_image,
        "REGISTRY_HOST": preview.registry_host,
        "REGISTRY_NAMESPACE": preview.registry_namespace,
        "TRAEFIK_NETWORK": preview.traefik_network,
        "TRAEFIK_ROUTER_NAME": compose_project_name,
        "TRAEFIK_SERVICE_NAME": compose_project_name,
        "PREVIEW_URL_SCHEME": preview.url_scheme,
    }


def _shorten_sha(commit_sha: str) -> str:
    """Return a stable short SHA string.

    Args:
        commit_sha: Full or short commit SHA.

    Returns:
        First 8 characters of the provided SHA, or the original value if
        shorter than 8 characters.
    """
    return commit_sha[:8] if len(commit_sha) > 8 else commit_sha
