"""SQLAlchemy database setup for infrastructure persistence."""

from typing import Any, Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.infrastructure.config.settings import config
from backend.infrastructure.logging.logger import logger

Base = declarative_base()

DATABASE_URL = config.resolved_database_url
if DATABASE_URL.startswith("mysql://"):
    DATABASE_URL = DATABASE_URL.replace("mysql://", "mysql+pymysql://")


def create_database_engine(**kwargs: Any) -> Any:
    """Create a SQLAlchemy engine.

    Args:
        **kwargs: Extra keyword arguments forwarded to ``create_engine``.

    Returns:
        Any: SQLAlchemy engine instance.
    """
    default_kwargs = {
        "poolclass": StaticPool,
        "echo": False,
    }
    default_kwargs.update(kwargs)
    return create_engine(DATABASE_URL, **default_kwargs)


engine = create_database_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def create_tables(base: Any = None) -> None:
    """Create all tables for the provided declarative base."""
    target_base = base or Base
    target_base.metadata.create_all(bind=engine)
    logger.info("数据库表创建成功！")


def get_db() -> Generator[Session, None, None]:
    """Yield a database session for dependency injection."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_database(base: Any = None) -> None:
    """Initialize database tables."""
    create_tables(base)


__all__ = [
    "Base",
    "SessionLocal",
    "create_database_engine",
    "create_tables",
    "engine",
    "get_db",
    "init_database",
]
