"""Alembic environment configuration."""

import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context

from backend.infrastructure.config.settings import config as app_config
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

# Resolve DATABASE_URL: env var > .env/.env.local > project config defaults
DATABASE_URL: str = os.getenv("DATABASE_URL") or app_config.resolved_database_url


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
