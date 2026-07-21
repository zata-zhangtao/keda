#!/usr/bin/env python3
"""为新复制出的项目或 Worktree 设置独立的数据库。

读取目标目录的 ``.env.local``，根据标识派生唯一的数据库名，更新
``DATABASE_URL``，并尝试在 PostgreSQL 或 MySQL 中创建该数据库。
"""

from __future__ import annotations

import re
import sys
from hashlib import sha256
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


def derive_database_name(database_identifier: str) -> str:
    """根据标识派生 PostgreSQL/MySQL 安全的数据库名。

    Args:
        database_identifier: 用于派生数据库名的项目或 Worktree 标识。

    Returns:
        小写、下划线连接、不超过 63 字节的数据库名。
    """
    db_name = re.sub(r"[^a-z0-9]+", "_", database_identifier.lower()).strip("_")
    db_name = re.sub(r"_+", "_", db_name)
    if not db_name:
        db_name = "app"
    if db_name[0].isdigit():
        db_name = f"app_{db_name}"
    if len(db_name) <= 63:
        return db_name

    # PostgreSQL 标识符默认限制 63 字节，低于 MySQL 的 64 字节限制。
    # 保留哈希尾缀，避免长分支名截断后冲突。
    identifier_digest = sha256(database_identifier.encode("utf-8")).hexdigest()[:8]
    return f"{db_name[:54]}_{identifier_digest}"


def update_database_url(
    env_local_path: Path,
    database_identifier: str,
) -> tuple[Optional[str], Optional[str]]:
    """更新 ``.env.local`` 中的 ``DATABASE_URL`` 为项目专用数据库。

    Args:
        env_local_path: 目标项目 ``.env.local`` 路径。
        database_identifier: 用于派生数据库名的项目或 Worktree 标识。

    Returns:
        (新的 DATABASE_URL, 新的数据库名)；若无需更新则返回 ``(None, None)``。
    """
    if not env_local_path.exists():
        return None, None

    text = env_local_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    new_url: Optional[str] = None
    updated = False

    for index, line in enumerate(lines):
        if not line.startswith("DATABASE_URL="):
            continue
        _, _, value = line.partition("=")
        value = value.strip()
        if not value:
            continue
        parsed = urlparse(value)
        if not parsed.scheme.startswith(("postgresql", "mysql")):
            # 只改写 PostgreSQL/MySQL URL，SQLite 等保持不变。
            continue
        db_name = derive_database_name(database_identifier)
        new_url = parsed._replace(path=f"/{db_name}").geturl()
        lines[index] = f"DATABASE_URL={new_url}"
        updated = True
        break

    if updated:
        env_local_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return (new_url, derive_database_name(database_identifier)) if updated else (None, None)


def create_postgres_database(database_url: str) -> bool:
    """尝试根据 ``DATABASE_URL`` 创建 PostgreSQL 数据库。

    Args:
        database_url: 已更新为项目专用数据库的 URL。

    Returns:
        数据库已创建或已存在返回 ``True``，否则返回 ``False``。
    """
    parsed = urlparse(database_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    user = parsed.username
    password = parsed.password
    db_name = parsed.path.lstrip("/") if parsed.path else ""

    if not db_name:
        return False

    try:
        import psycopg2
    except ImportError:
        print("psycopg2 不可用，无法自动创建数据库。")
        return False

    connection = None
    try:
        connection = psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname="postgres",
        )
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s;",
                (db_name,),
            )
            if cursor.fetchone():
                print(f"数据库 '{db_name}' 已存在。")
                return True
            cursor.execute(f"CREATE DATABASE {db_name};")
        print(f"已创建数据库: {db_name}")
        return True
    except psycopg2.Error as exc:
        print(f"无法创建数据库 '{db_name}': {exc}")
        return False
    finally:
        if connection is not None:
            connection.close()


def create_mysql_database(database_url: str) -> bool:
    """尝试根据 ``DATABASE_URL`` 创建 MySQL 数据库。

    Args:
        database_url: 已更新为项目专用数据库的 MySQL URL。

    Returns:
        数据库已创建或已存在返回 ``True``，否则返回 ``False``。
    """
    parsed = urlparse(database_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 3306
    user = parsed.username
    password = parsed.password
    db_name = parsed.path.lstrip("/") if parsed.path else ""

    if not db_name:
        return False

    try:
        import pymysql
    except ImportError:
        print("PyMySQL 不可用，无法自动创建数据库。")
        return False

    connection = None
    try:
        connection = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database="mysql",
            autocommit=True,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s;",
                (db_name,),
            )
            if cursor.fetchone():
                print(f"数据库 '{db_name}' 已存在。")
                return True
            cursor.execute(f"CREATE DATABASE `{db_name}`;")
        print(f"已创建数据库: {db_name}")
        return True
    except pymysql.MySQLError as exc:
        print(f"无法创建数据库 '{db_name}': {exc}")
        return False
    finally:
        if connection is not None:
            connection.close()


def main() -> int:
    """脚本入口。"""
    script_arguments = sys.argv[1:]
    strict_mode = "--strict" in script_arguments
    positional_arguments = [
        argument_value for argument_value in script_arguments if argument_value != "--strict"
    ]
    if len(positional_arguments) != 2:
        print(
            "Usage: python setup_copied_database.py <database_identifier> <project_root> [--strict]"
        )
        return 1

    database_identifier, project_root_argument = positional_arguments
    project_root = Path(project_root_argument)

    env_local_path = project_root / ".env.local"
    new_url, db_name = update_database_url(env_local_path, database_identifier)

    if new_url is None or db_name is None:
        print(".env.local 中未找到 PostgreSQL/MySQL 的 DATABASE_URL，跳过数据库设置。")
        return 1 if strict_mode else 0

    print(f"已将 DATABASE_URL 改写为项目专用数据库: {db_name}")

    parsed_database_url = urlparse(new_url)
    if parsed_database_url.scheme.startswith("postgresql"):
        database_ready = create_postgres_database(new_url)
    elif parsed_database_url.scheme.startswith("mysql"):
        database_ready = create_mysql_database(new_url)
    else:
        database_ready = False

    if database_ready:
        print("数据库已就绪。")
    else:
        print("请手动创建数据库，然后运行 `uv run alembic upgrade head`。")
        return 1 if strict_mode else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
