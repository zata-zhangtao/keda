"""Database backup utilities."""

import gzip
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse


def backup_database(db_url: str, output_path: Path) -> None:
    """Backup database to a gzipped SQL file.

    Args:
        db_url: Database connection URL.
        output_path: Path to write the gzipped backup file.

    Raises:
        ValueError: If the database scheme is not supported.
        FileNotFoundError: If the SQLite database file does not exist.
        subprocess.CalledProcessError: If the dump command fails.
    """
    db_url_lower: str = db_url.lower()
    if "sqlite" in db_url_lower:
        _backup_sqlite(db_url, output_path)
    elif "postgres" in db_url_lower:
        _backup_postgres(db_url, output_path)
    else:
        raise ValueError(f"Unsupported database scheme in URL: {db_url}")


def _parse_sqlite_path(db_url: str) -> Path:
    """Extract the filesystem path from a SQLite URL.

    Supports ``sqlite:///absolute/path`` and ``sqlite://relative/path``.
    """
    parsed = urlparse(db_url)
    if parsed.path:
        path_str = parsed.path
        if parsed.netloc:
            path_str = "/" + parsed.netloc + path_str
        return Path(path_str)
    return Path(db_url.replace("sqlite://", ""))


def _backup_sqlite(db_url: str, output_path: Path) -> None:
    """Backup SQLite database via ``sqlite3 .dump``."""
    db_path = _parse_sqlite_path(db_url)
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database file not found: {db_path}")

    result = subprocess.run(
        ["sqlite3", str(db_path), ".dump"],
        capture_output=True,
        text=True,
        check=True,
    )
    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        f.write(result.stdout)


def _backup_postgres(db_url: str, output_path: Path) -> None:
    """Backup PostgreSQL database via ``pg_dump``."""
    parsed = urlparse(db_url)
    host: str = parsed.hostname or "localhost"
    port: int = parsed.port or 5432
    database: str = parsed.path.lstrip("/") if parsed.path else "postgres"
    user: str = parsed.username or "postgres"
    password: str = parsed.password or ""

    env = os.environ.copy()
    if password:
        env["PGPASSWORD"] = password
    env["PGCLIENTENCODING"] = "UTF8"

    cmd = [
        "pg_dump",
        "-h",
        host,
        "-p",
        str(port),
        "-U",
        user,
        "-d",
        database,
        "--encoding=UTF8",
        "-F",
        "p",
    ]

    with gzip.open(output_path, "wb") as f:
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE)
        if proc.stdout is None:
            raise RuntimeError("pg_dump stdout pipe was not created")
        with proc.stdout:
            shutil.copyfileobj(proc.stdout, f)
        return_code = proc.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, cmd)
