"""Tests for the worktree-specific PostgreSQL database setup helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_database_setup_module() -> ModuleType:
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "shared"
        / "template"
        / "setup_copied_database.py"
    )
    module_spec = importlib.util.spec_from_file_location("setup_copied_database", script_path)
    assert module_spec is not None
    assert module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_derive_database_name_normalizes_and_bounds_identifier() -> None:
    """Database identifiers must be Postgres-safe and no longer than 63 bytes."""
    database_setup_module = _load_database_setup_module()

    normalized_database_name = database_setup_module.derive_database_name("Keda WT/Feature--Login")
    long_database_name = database_setup_module.derive_database_name("feature-" * 20)

    assert normalized_database_name == "keda_wt_feature_login"
    assert len(long_database_name) == 63
    assert long_database_name.startswith("feature_feature_")


def test_update_database_url_replaces_only_postgres_database_name(tmp_path: Path) -> None:
    """A PostgreSQL URL is rewritten while unrelated environment entries remain unchanged."""
    database_setup_module = _load_database_setup_module()
    env_local_path = tmp_path / ".env.local"
    env_local_path.write_text(
        "APP_ENV=development\n"
        "DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/shared_db\n",
        encoding="utf-8",
    )

    rewritten_database_url, generated_database_name = database_setup_module.update_database_url(
        env_local_path,
        "Keda WT/Feature Login",
    )

    assert generated_database_name == "keda_wt_feature_login"
    assert rewritten_database_url == (
        "postgresql+psycopg2://user:pass@localhost:5432/keda_wt_feature_login"
    )
    assert env_local_path.read_text(encoding="utf-8") == (
        "APP_ENV=development\n"
        "DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/keda_wt_feature_login\n"
    )


def test_update_database_url_leaves_non_postgres_url_untouched(tmp_path: Path) -> None:
    """SQLite projects retain their configured URL and do not claim a database setup."""
    database_setup_module = _load_database_setup_module()
    env_local_path = tmp_path / ".env.local"
    sqlite_database_url = "DATABASE_URL=sqlite:///./keda.db\n"
    env_local_path.write_text(sqlite_database_url, encoding="utf-8")

    rewritten_database_url, generated_database_name = database_setup_module.update_database_url(
        env_local_path,
        "feature-login",
    )

    assert rewritten_database_url is None
    assert generated_database_name is None
    assert env_local_path.read_text(encoding="utf-8") == sqlite_database_url


def test_update_database_url_rewrites_mysql_database_name(tmp_path: Path) -> None:
    """MySQL URLs use the same isolated database identifier convention."""
    database_setup_module = _load_database_setup_module()
    env_local_path = tmp_path / ".env.local"
    env_local_path.write_text(
        "DATABASE_URL=mysql+pymysql://user:pass@localhost:3306/shared_db\n",
        encoding="utf-8",
    )

    rewritten_database_url, generated_database_name = database_setup_module.update_database_url(
        env_local_path,
        "Keda IAR Issue 42",
    )

    assert generated_database_name == "keda_iar_issue_42"
    assert rewritten_database_url == "mysql+pymysql://user:pass@localhost:3306/keda_iar_issue_42"
