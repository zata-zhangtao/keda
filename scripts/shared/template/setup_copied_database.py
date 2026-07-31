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


def create_postgres_database(
    database_url: str,
    locale: str = "",
    encoding: str = "",
) -> bool:
    """尝试根据 ``DATABASE_URL`` 创建 PostgreSQL 数据库。

    不显式给定 ``LOCALE`` / ``ENCODING`` 时，从 ``template0`` 复制（避免被
    集群 ``template1`` 自定义污染），并继承 cluster 默认 collation——这是
    最广泛兼容的默认行为。

    显式给定 ``locale`` 或 ``encoding`` 时，会拼到 ``CREATE DATABASE``
    语句中。注意 PostgreSQL 要求 ``ENCODING`` 与 ``LC_COLLATE`` / ``LC_CTYPE``
    都与 OS locale 匹配，若集群未安装该 locale 会报错。

    Args:
        database_url: 已更新为项目专用数据库的 URL。
        locale: 可选 PostgreSQL ``LOCALE``，例如 ``C.UTF-8``。
        encoding: 可选 PostgreSQL ``ENCODING``，例如 ``UTF8``。

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

    create_database_sql = compose_postgres_create_database_sql(db_name, locale, encoding)

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
            cursor.execute(create_database_sql)
        print(
            f"已创建数据库: {db_name}"
            + (f" (locale={locale})" if locale else "")
            + (f" (encoding={encoding})" if encoding else "")
        )
        return True
    except psycopg2.Error as exc:
        print(f"无法创建数据库 '{db_name}': {exc}")
        return False
    finally:
        if connection is not None:
            connection.close()


def compose_postgres_create_database_sql(
    db_name: str,
    locale: str = "",
    encoding: str = "",
) -> str:
    """拼装 PostgreSQL ``CREATE DATABASE`` 语句。

    始终从 ``template0`` 复制，避免被集群 ``template1`` 自定义污染。
    显式传入 ``locale`` / ``encoding`` 时追加对应子句。

    单独抽出来便于单元测试在没有真实数据库时验证 SQL 形式。
    """
    db_options = []
    if encoding:
        db_options.append(f"ENCODING = '{encoding}'")
    if locale:
        db_options.append(f"LOCALE = '{locale}'")
    options_clause = f" WITH { ' '.join(db_options)}" if db_options else ""
    return f"CREATE DATABASE {db_name}{options_clause} TEMPLATE template0;"


# MySQL 默认字符集 / 排序规则。
# 选 ``utf8mb4_unicode_ci`` 与 MySQL 5.7 默认以及大多数 ORM 默认一致，
# 跨 MySQL 5.7 / 8.0 / MariaDB 全部可用。
# MySQL 8 服务器默认 ``utf8mb4_0900_ai_ci`` 与此不同，会与某些既有依赖
# ``utf8mb4_unicode_ci`` 的迁移链路触发 ``Illegal mix of collations``，
# 所以上游默认走更保守的 ``utf8mb4_unicode_ci``；下游项目如有其他选择，
# 在 ``.env.local`` 用 ``DATABASE_CHARSET`` / ``DATABASE_COLLATION`` 覆盖。
DEFAULT_MYSQL_CHARSET = "utf8mb4"
DEFAULT_MYSQL_COLLATION = "utf8mb4_unicode_ci"


def create_mysql_database(
    database_url: str,
    charset: str = DEFAULT_MYSQL_CHARSET,
    collation: str = DEFAULT_MYSQL_COLLATION,
) -> bool:
    """尝试根据 ``DATABASE_URL`` 创建 MySQL 数据库。

    创建时显式带上 ``CHARACTER SET`` / ``COLLATE``，避免落到 MySQL 8
    服务器默认 ``utf8mb4_0900_ai_ci``。下游项目可通过 ``charset`` /
    ``collation`` 参数或 ``.env.local`` 的 ``DATABASE_CHARSET`` /
    ``DATABASE_COLLATION`` 覆盖。

    Args:
        database_url: 已更新为项目专用数据库的 MySQL URL。
        charset: 新建数据库的字符集，默认与上游通用保守选择一致。
        collation: 新建数据库的排序规则，默认与上游通用保守选择一致。

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

    create_database_sql = compose_mysql_create_database_sql(db_name, charset, collation)

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
            cursor.execute(create_database_sql)
        print(f"已创建数据库: {db_name} (charset={charset}, collation={collation})")
        return True
    except pymysql.MySQLError as exc:
        print(f"无法创建数据库 '{db_name}': {exc}")
        return False
    finally:
        if connection is not None:
            connection.close()


def compose_mysql_create_database_sql(
    db_name: str,
    charset: str = DEFAULT_MYSQL_CHARSET,
    collation: str = DEFAULT_MYSQL_COLLATION,
) -> str:
    """拼装 MySQL ``CREATE DATABASE`` 语句。

    始终显式带 ``CHARACTER SET`` / ``COLLATE``，避免落到 MySQL 8
    服务器默认 ``utf8mb4_0900_ai_ci``。单独抽出来便于单元测试。
    """
    return f"CREATE DATABASE `{db_name}` " f"CHARACTER SET {charset} COLLATE {collation};"


# .env.local 中可被下游项目覆盖的数据库建库选项；优先级高于模块默认。
# 设置后会被 ``main()`` 读取并传给 ``create_postgres_database`` /
# ``create_mysql_database``。不设置则用模块默认值。
DATABASE_OPTION_KEYS: tuple[str, ...] = (
    "DATABASE_CHARSET",
    "DATABASE_COLLATION",
    "DATABASE_LOCALE",
    "DATABASE_ENCODING",
)


def read_database_options(env_local_path: Path) -> dict[str, str]:
    """从 ``.env.local`` 读取 ``DATABASE_*`` 建库选项。

    解析规则：

    - 形式 ``KEY=VALUE`` 的行；忽略 ``#`` 开头的注释；
    - 命中 ``DATABASE_OPTION_KEYS`` 之一的键即纳入结果；
    - ``VALUE`` 去掉首尾空白，保留空串（与"未设置"等价；
      ``main()`` 会用 ``or <default>`` 兜底）。

    Args:
        env_local_path: 目标项目 ``.env.local`` 路径。

    Returns:
        ``{key: value}`` 形式的选项字典。仅包含出现在文件中的键，
        缺失的键不在结果里（让调用方用 ``dict.get`` 配默认值）。
    """
    options: dict[str, str] = {}
    if not env_local_path.exists():
        return options

    for raw_line in env_local_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator != "=":
            continue
        key = key.strip()
        if key not in DATABASE_OPTION_KEYS:
            continue
        options[key] = value.strip()
    return options


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

    # 下游项目可在 .env.local 里覆盖四个建库选项；未设置则用模块默认。
    database_options = read_database_options(env_local_path)
    mysql_charset = database_options.get("DATABASE_CHARSET") or DEFAULT_MYSQL_CHARSET
    mysql_collation = database_options.get("DATABASE_COLLATION") or DEFAULT_MYSQL_COLLATION
    postgres_locale = database_options.get("DATABASE_LOCALE", "")
    postgres_encoding = database_options.get("DATABASE_ENCODING", "")

    parsed_database_url = urlparse(new_url)
    if parsed_database_url.scheme.startswith("postgresql"):
        database_ready = create_postgres_database(
            new_url, locale=postgres_locale, encoding=postgres_encoding
        )
    elif parsed_database_url.scheme.startswith("mysql"):
        database_ready = create_mysql_database(
            new_url, charset=mysql_charset, collation=mysql_collation
        )
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
