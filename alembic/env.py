"""Alembic environment configuration."""

import os
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import create_engine, pool

from alembic import context

# Import project Base
from backend.infrastructure.persistence.database import Base

# Register all models so they are visible in Base.metadata for autogenerate.
# Uncomment and adjust once you have model files:
# import backend.infrastructure.persistence.models  # noqa: F401

# this is the Alembic Config object
alembic_config = context.config

# Interpret the config file for Python logging.
if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

# Target metadata for autogenerate support
target_metadata = Base.metadata

# Resolve DATABASE_URL: env var > .env > project config defaults
_DATABASE_URL: str | None = os.getenv("DATABASE_URL")
if not _DATABASE_URL:
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.is_file():
        with open(_env_path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line.startswith("DATABASE_URL="):
                    _DATABASE_URL = _line.split("=", 1)[1].strip()
                    break
if not _DATABASE_URL:
    from backend.infrastructure.config.settings import config as app_config

    _DATABASE_URL = app_config.resolved_database_url

DATABASE_URL = _DATABASE_URL


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = create_engine(DATABASE_URL, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
