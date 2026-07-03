"""Tests for deployment environment templates.

These tests verify bidirectional consistency between environment variable
templates and Docker Compose declarations, without hard-coding service
boundaries. Instead, service env keys are extracted directly from the
compose files so adding a new service or variable only requires updating
the compose/env files, not this test module.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT_PATH: Path = Path(__file__).resolve().parents[1]
DOKPLOY_COMPOSE_PATH: Path = PROJECT_ROOT_PATH / "docker-compose.dokploy.yml"
LOCAL_COMPOSE_PATH: Path = PROJECT_ROOT_PATH / "docker-compose.yml"
DOKPLOY_ENV_TEMPLATE_PATH: Path = PROJECT_ROOT_PATH / ".env.dokploy.example"
ROOT_ENV_TEMPLATE_PATH: Path = PROJECT_ROOT_PATH / ".env.example"

# Variables used in Traefik labels or hardcoded in compose that don't need
# an env template entry.
HARDCODED_ENV_KEYS: set[str] = {
    "HOST",
    "PORT",
    "LOGS_DIR",
    "RESOURCES_DIR",
    "WORK_DIR",
}
SPECIAL_LABEL_KEYS: set[str] = {"DOMAIN"}

# Historical env keys that are no longer referenced in the codebase.
# When a key is removed from the application, add it here so the test
# flags stale documentation in the templates.
STALE_ENV_TEMPLATE_KEYS: set[str] = {
    "AZHEXING_API_KEY",
    "REDBOX_API_KEY",
}


def _parse_active_env_keys(env_file_path: Path) -> set[str]:
    """Return uncommented environment variable names from a dotenv file."""
    if not env_file_path.is_file():
        return set()

    active_env_keys: set[str] = set()
    for raw_env_line in env_file_path.read_text(encoding="utf-8").splitlines():
        stripped_env_line: str = raw_env_line.strip()
        if (
            not stripped_env_line
            or stripped_env_line.startswith("#")
            or "=" not in stripped_env_line
        ):
            continue

        assignment_text: str = stripped_env_line.removeprefix("export ").strip()
        env_key: str = assignment_text.split("=", 1)[0].strip()
        if env_key:
            active_env_keys.add(env_key)

    return active_env_keys


def _parse_all_env_keys(env_file_path: Path) -> set[str]:
    """Return commented and uncommented environment variable names."""
    all_env_keys: set[str] = set()
    for raw_env_line in env_file_path.read_text(encoding="utf-8").splitlines():
        stripped_env_line: str = raw_env_line.strip().lstrip("#").strip()
        if not stripped_env_line or "=" not in stripped_env_line:
            continue

        assignment_text: str = stripped_env_line.removeprefix("export ").strip()
        env_key: str = assignment_text.split("=", 1)[0].strip()
        if env_key:
            all_env_keys.add(env_key)

    return all_env_keys


def _read_env_assignment_value(env_file_path: Path, target_env_key: str) -> str | None:
    """Return an env assignment value without parsing dotenv interpolation."""
    for raw_env_line in env_file_path.read_text(encoding="utf-8").splitlines():
        stripped_env_line: str = raw_env_line.strip().lstrip("#").strip()
        if not stripped_env_line or "=" not in stripped_env_line:
            continue

        env_key, _, env_value = stripped_env_line.partition("=")
        if env_key.strip() == target_env_key:
            return env_value.strip()

    return None


def _parse_all_service_env_keys(compose_file_path: Path) -> dict[str, set[str]]:
    """Return {service_name: env_keys} for every service with an environment block.

    This function scans the compose file YAML structure at the indentation
    level used by Docker Compose services (2-space top-level keys).
    """
    services: dict[str, set[str]] = {}
    current_service: str | None = None
    is_environment_block = False

    for raw_compose_line in compose_file_path.read_text(encoding="utf-8").splitlines():
        if raw_compose_line.startswith("  ") and not raw_compose_line.startswith("    "):
            current_service = raw_compose_line.strip().removesuffix(":")
            is_environment_block = False
            continue

        if current_service is None:
            continue

        if raw_compose_line.startswith("    environment:"):
            is_environment_block = True
            services.setdefault(current_service, set())
            continue

        if not is_environment_block:
            continue

        stripped_compose_line: str = raw_compose_line.strip()
        if stripped_compose_line.startswith("- "):
            env_entry: str = stripped_compose_line.removeprefix("- ").strip()
            env_key: str = env_entry.split("=", 1)[0].strip()
            if env_key:
                services[current_service].add(env_key)
            continue

        if stripped_compose_line:
            is_environment_block = False

    return services


def test_active_dokploy_template_keys_are_used_in_compose() -> None:
    """Every active .env.dokploy variable must appear in at least one compose service."""
    template_active: set[str] = _parse_active_env_keys(DOKPLOY_ENV_TEMPLATE_PATH)
    service_envs: dict[str, set[str]] = _parse_all_service_env_keys(DOKPLOY_COMPOSE_PATH)
    all_compose_env_keys: set[str] = set().union(*service_envs.values())

    unused_keys: list[str] = sorted(template_active - all_compose_env_keys - SPECIAL_LABEL_KEYS)

    assert not unused_keys, (
        "Active .env.dokploy variables must be used by at least one compose service: "
        f"{unused_keys}"
    )


def test_compose_service_env_keys_are_documented_in_dokploy_template() -> None:
    """Every compose service env key must be active in .env.dokploy (or hardcoded)."""
    template_active: set[str] = _parse_active_env_keys(DOKPLOY_ENV_TEMPLATE_PATH)
    service_envs: dict[str, set[str]] = _parse_all_service_env_keys(DOKPLOY_COMPOSE_PATH)

    for service_name, env_keys in service_envs.items():
        undocumented: list[str] = sorted(env_keys - template_active - HARDCODED_ENV_KEYS)
        assert not undocumented, (
            f"Service '{service_name}' env keys must be active in .env.dokploy: " f"{undocumented}"
        )


def test_active_env_example_keys_are_used_in_local_compose() -> None:
    """Every active .env.example variable must appear in local docker-compose.yml."""
    template_active: set[str] = _parse_active_env_keys(ROOT_ENV_TEMPLATE_PATH)
    service_envs: dict[str, set[str]] = _parse_all_service_env_keys(LOCAL_COMPOSE_PATH)
    all_compose_env_keys: set[str] = set().union(*service_envs.values())

    unused_keys: list[str] = sorted(template_active - all_compose_env_keys)

    assert not unused_keys, (
        "Active .env.example variables must be used by local compose: " f"{unused_keys}"
    )


def test_local_compose_service_env_keys_are_documented_in_example() -> None:
    """Every local compose service env key must be active in .env.example (or hardcoded)."""
    template_active: set[str] = _parse_active_env_keys(ROOT_ENV_TEMPLATE_PATH)
    service_envs: dict[str, set[str]] = _parse_all_service_env_keys(LOCAL_COMPOSE_PATH)

    for service_name, env_keys in service_envs.items():
        undocumented: list[str] = sorted(env_keys - template_active - HARDCODED_ENV_KEYS)
        assert not undocumented, (
            f"Local service '{service_name}' env keys must be active in .env.example: "
            f"{undocumented}"
        )


def test_env_templates_do_not_list_stale_keys() -> None:
    """Environment templates should not document no-op keys."""
    template_env_keys: set[str] = _parse_all_env_keys(ROOT_ENV_TEMPLATE_PATH)
    dokploy_template_env_keys: set[str] = _parse_all_env_keys(DOKPLOY_ENV_TEMPLATE_PATH)
    stale_template_env_keys: list[str] = sorted(
        (template_env_keys | dokploy_template_env_keys) & STALE_ENV_TEMPLATE_KEYS
    )

    assert not stale_template_env_keys, (
        "Remove stale environment keys from templates: " f"{stale_template_env_keys}"
    )


def test_admin_password_hash_template_values_escape_dollars() -> None:
    """Bcrypt hash placeholders should avoid Docker Compose interpolation."""
    template_hash_value: str | None = _read_env_assignment_value(
        ROOT_ENV_TEMPLATE_PATH, "ADMIN_PASSWORD_HASH"
    )
    dokploy_template_hash_value: str | None = _read_env_assignment_value(
        DOKPLOY_ENV_TEMPLATE_PATH, "ADMIN_PASSWORD_HASH"
    )

    assert template_hash_value is not None
    assert dokploy_template_hash_value is not None
    assert template_hash_value.startswith("$$2")
    assert dokploy_template_hash_value.startswith("$$2")
