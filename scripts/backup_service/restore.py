"""Restore service for backups from S3.

Supports single-point restore and incremental-chain restore for files.
Database is always restored from the target date's dump directly.

Standalone usage (local machine):
    uv run python scripts/backup_service/restore.py --date 2026-05-14_020000 \
        --restore-db --restore-logs --restore-resources --yes

Docker usage (no local psql needed, uses backup container):
    docker compose --profile backup run --rm backup \
        python -m scripts.backup_service.restore
"""

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from scripts.backup_service.db import _parse_sqlite_path
from scripts.backup_service.s3_client import S3Client


class RestoreClient(S3Client):
    """S3 client for browsing and downloading backups.

    Extends :class:`S3Client` with restore-specific helpers.
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        prefix: str,
    ) -> None:
        super().__init__(endpoint, access_key, secret_key, bucket)
        self.prefix = prefix

    def list_backup_dates(self) -> list[str]:
        """Return sorted list of backup dates (YYYY-MM-DD_HHMMSS)."""
        dates: set[str] = set()
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix + "/"):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                parts = key.split("/")
                if len(parts) >= 2:
                    date_str = parts[1]
                    if "_" in date_str and date_str.replace("_", "").replace("-", "").isdigit():
                        dates.add(date_str)
        return sorted(dates)

    def get_backup_manifest(self, date_str: str) -> dict | None:
        """Fetch manifest.json for a backup date (tries full then incremental)."""
        for backup_type in ("full", "incremental"):
            key = f"{self.prefix}/{date_str}/{backup_type}/manifest.json"
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=key)
                return json.loads(response["Body"].read().decode("utf-8"))
            except self.client.exceptions.NoSuchKey:
                continue
        return None

    def download_backup_file(self, s3_key: str, output_path: Path) -> None:
        """Download a single file from S3."""
        self.client.download_file(self.bucket, s3_key, str(output_path))


def _find_full_backup_before(
    client: RestoreClient, dates: list[str], target_date: str
) -> str | None:
    """Locate the most recent full backup on or before *target_date*."""
    if target_date not in dates:
        return None
    target_idx = dates.index(target_date)
    for i in range(target_idx, -1, -1):
        manifest = client.get_backup_manifest(dates[i])
        if manifest and manifest.get("type") == "full":
            return dates[i]
    return None


def _parse_postgres_url(db_url: str) -> dict[str, str | int]:
    """Parse a PostgreSQL connection URL into components.

    Args:
        db_url: PostgreSQL connection URL.

    Returns:
        Mapping with keys ``host``, ``port``, ``database``, ``user``, ``password``.
    """
    parsed = urlparse(db_url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/") if parsed.path else "postgres",
        "user": parsed.username or "postgres",
        "password": parsed.password or "",
    }


def _build_psql_env(password: str) -> dict[str, str]:
    """Build environment dict with PGPASSWORD if needed."""
    env = os.environ.copy()
    if password:
        env["PGPASSWORD"] = password
    return env


def restore_database(
    db_url: str,
    sql_gz_path: Path,
    sanitize_invalid_utf8: bool = False,
) -> None:
    """Restore database from a gzipped SQL dump.

    Args:
        db_url: Target database URL.
        sql_gz_path: Path to the ``database.sql.gz`` file.
        sanitize_invalid_utf8: Replace invalid UTF-8 bytes with the Unicode
            replacement character before piping the dump to PostgreSQL.

    Raises:
        ValueError: If the database scheme is not supported.
        subprocess.CalledProcessError: If the restore command fails.
    """
    db_url_lower: str = db_url.lower()
    if "sqlite" in db_url_lower:
        _restore_sqlite(db_url, sql_gz_path)
    elif "postgres" in db_url_lower:
        _restore_postgres(
            db_url,
            sql_gz_path,
            sanitize_invalid_utf8=sanitize_invalid_utf8,
        )
    else:
        raise ValueError(f"Unsupported database scheme: {db_url}")


_GZIP_MAGIC = b"\x1f\x8b"
_GZIP_DEFLATE_MAGIC = b"\x1f\x8b\x08"
_LEGACY_GZIP_FILENAME_MARKERS = (b"database.sql", b"database.sql.gz")
_INCOMPATIBLE_SET_PREFIXES = (b"SET transaction_timeout",)
_POSTGRES_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sanitize_invalid_utf8_line(line: bytes) -> tuple[bytes, bool]:
    """Replace invalid UTF-8 bytes in one dump line."""
    try:
        line.decode("utf-8")
        return line, False
    except UnicodeDecodeError:
        return line.decode("utf-8", errors="replace").encode("utf-8"), True


def _find_legacy_gzip_tail_index(line: bytes) -> int:
    """Return the index where malformed legacy gzip tail bytes begin."""
    gzip_tail_index = line.find(_GZIP_DEFLATE_MAGIC)
    if gzip_tail_index >= 0:
        return gzip_tail_index

    for marker in _LEGACY_GZIP_FILENAME_MARKERS:
        marker_index = line.find(marker)
        if marker_index > 0:
            prefix = line[:marker_index]
            if not prefix.decode("utf-8", errors="ignore").strip():
                return 0
            try:
                prefix.decode("utf-8")
            except UnicodeDecodeError:
                return 0

    return -1


def _pipe_gzip_to_command(
    sql_gz_path: Path,
    cmd: list[str],
    env: dict[str, str] | None = None,
    sanitize_invalid_utf8: bool = False,
) -> None:
    """Stream a SQL dump into a subprocess via stdin.

    Supports both gzipped (``.sql.gz``) and plain SQL files by sniffing the
    gzip magic header ``0x1f 0x8b``. Automatically skips ``SET`` statements
    that reference configuration parameters which may not exist on the target
    PostgreSQL instance (e.g. ``transaction_timeout``).
    """
    with open(sql_gz_path, "rb") as f_check:
        magic = f_check.read(2)

    is_gzipped_dump = magic == _GZIP_MAGIC
    if is_gzipped_dump:
        f_in = gzip.open(sql_gz_path, "rb")
    else:
        f_in = open(sql_gz_path, "rb")

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, env=env)
    invalid_utf8_replacements = 0
    embedded_gzip_tail_skipped = False
    try:
        for line in f_in:
            stop_after_line = False
            if not is_gzipped_dump:
                gzip_tail_index = _find_legacy_gzip_tail_index(line)
                if gzip_tail_index >= 0:
                    line = line[:gzip_tail_index]
                    embedded_gzip_tail_skipped = True
                    stop_after_line = True

            stripped = line.strip()
            if line and any(stripped.startswith(prefix) for prefix in _INCOMPATIBLE_SET_PREFIXES):
                if stop_after_line:
                    break
                continue
            if sanitize_invalid_utf8:
                line, replaced = _sanitize_invalid_utf8_line(line)
                if replaced:
                    invalid_utf8_replacements += 1
            if line:
                proc.stdin.write(line)  # type: ignore[union-attr]
            if stop_after_line:
                break
    finally:
        f_in.close()
        proc.stdin.close()  # type: ignore[union-attr]
        proc.wait()
        if embedded_gzip_tail_skipped:
            print(
                "[WARN] Ignored embedded gzip bytes at the end of a plain SQL "
                "dump. This indicates a malformed legacy backup."
            )
        if invalid_utf8_replacements:
            print(
                f"[WARN] Replaced invalid UTF-8 bytes in "
                f"{invalid_utf8_replacements} dump line(s)."
            )
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, proc.args)


def _restore_sqlite(db_url: str, sql_gz_path: Path) -> None:
    """Restore SQLite database by piping SQL into ``sqlite3``."""
    db_path = _parse_sqlite_path(db_url)
    print(f"Restoring SQLite database to {db_path}...")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _pipe_gzip_to_command(sql_gz_path, ["sqlite3", str(db_path)])
    print("SQLite restore complete.")


def _restore_postgres(
    db_url: str,
    sql_gz_path: Path,
    sanitize_invalid_utf8: bool = False,
) -> None:
    """Restore PostgreSQL database by piping SQL into ``psql``.

    Uses ``-v ON_ERROR_STOP=1`` so that any SQL error causes an immediate
    non-zero exit. ``--single-transaction`` keeps failed restores from leaving
    partially applied database state.
    """
    parsed = _parse_postgres_url(db_url)
    host: str = parsed["host"]
    port: int = parsed["port"]
    database: str = parsed["database"]
    user: str = parsed["user"]
    password: str = parsed["password"]

    env = _build_psql_env(password)

    print(f"Restoring PostgreSQL database {database} on {host}:{port}...")

    try:
        if shutil.which("createdb"):
            subprocess.run(
                ["createdb", "-h", host, "-p", str(port), "-U", user, database],
                env=env,
                capture_output=True,
                check=True,
            )
            print(f"Created database {database}.")
        else:
            subprocess.run(
                [
                    "psql",
                    "-b",
                    "-h",
                    host,
                    "-p",
                    str(port),
                    "-U",
                    user,
                    "-d",
                    "postgres",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    f"CREATE DATABASE {database}",
                ],
                env=env,
                capture_output=True,
                check=True,
            )
            print(f"Created database {database} via psql.")
    except subprocess.CalledProcessError:
        pass

    _pipe_gzip_to_command(
        sql_gz_path,
        [
            "psql",
            "-b",
            "-v",
            "ON_ERROR_STOP=1",
            "--single-transaction",
            "-h",
            host,
            "-p",
            str(port),
            "-U",
            user,
            "-d",
            database,
        ],
        env=env,
        sanitize_invalid_utf8=sanitize_invalid_utf8,
    )
    print("PostgreSQL restore complete.")


def restore_archive(archive_path: Path, target_dir: str, use_incremental: bool = False) -> None:
    """Extract a ``tar.gz`` archive to *target_dir*.

    Args:
        archive_path: Path to the ``.tar.gz`` archive.
        target_dir: Directory to extract into.
        use_incremental: If ``True``, pass ``-G`` so GNU tar honours incremental
            deletion metadata.
    """
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = ["tar"]
    if use_incremental:
        cmd.append("-G")
    cmd.extend(["-xzf", str(archive_path), "-C", str(target)])

    print(f"Extracting {archive_path.name} to {target}...")
    subprocess.run(cmd, check=True)
    print("Extraction complete.")


def _find_s3_key(manifest: dict, filename: str) -> str | None:
    """Look up the S3 key of *filename* inside a manifest."""
    for file_entry in manifest.get("files", []):
        if file_entry.get("name") == filename:
            return file_entry.get("s3_key")
    return None


def _restore_chain(
    client: RestoreClient,
    chain: list[str],
    logs_dir: str,
    resources_dir: str,
    tmp: Path,
) -> None:
    """Apply a backup chain (full + incrementals) for file restores."""
    for idx, date_str in enumerate(chain):
        manifest = client.get_backup_manifest(date_str)
        if not manifest:
            print(f"[WARN] No manifest for {date_str}, skipping.")
            continue

        is_first = idx == 0
        use_g = not is_first

        logs_key = _find_s3_key(manifest, "logs.tar.gz")
        if logs_key:
            logs_path = tmp / f"{date_str}_logs.tar.gz"
            client.download_backup_file(logs_key, logs_path)
            restore_archive(logs_path, logs_dir, use_incremental=use_g)

        res_key = _find_s3_key(manifest, "resources.tar.gz")
        if res_key:
            res_path = tmp / f"{date_str}_resources.tar.gz"
            client.download_backup_file(res_key, res_path)
            restore_archive(res_path, resources_dir, use_incremental=use_g)


def _clean_target_schema(db_url: str) -> None:
    """Drop and recreate the ``public`` schema in the target database.

    Args:
        db_url: Target PostgreSQL database URL.

    Raises:
        subprocess.CalledProcessError: If the SQL command fails.
    """
    parsed = _parse_postgres_url(db_url)
    env = _build_psql_env(parsed["password"])
    cmd = [
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        parsed["host"],
        "-p",
        str(parsed["port"]),
        "-U",
        parsed["user"],
        "-d",
        parsed["database"],
        "-c",
        "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
    ]
    print(f"Cleaning public schema in {parsed['database']}...")
    subprocess.run(cmd, env=env, check=True, capture_output=True)
    print("Public schema recreated.")


def _drop_and_recreate_target_db(db_url: str) -> None:
    """Drop and recreate the target PostgreSQL database.

    Connects via the standard PostgreSQL client tools ``dropdb`` and ``createdb``.

    Args:
        db_url: Target PostgreSQL database URL.

    Raises:
        subprocess.CalledProcessError: If ``dropdb`` or ``createdb`` fails.
    """
    parsed = _parse_postgres_url(db_url)
    env = _build_psql_env(parsed["password"])
    host = parsed["host"]
    port = parsed["port"]
    database = parsed["database"]
    user = parsed["user"]

    print(f"Dropping database {database}...")
    subprocess.run(
        ["dropdb", "--if-exists", "-h", host, "-p", str(port), "-U", user, database],
        env=env,
        check=True,
        capture_output=True,
    )
    print(f"Creating database {database}...")
    subprocess.run(
        ["createdb", "-h", host, "-p", str(port), "-U", user, database],
        env=env,
        check=True,
        capture_output=True,
    )
    print("Database recreated.")


def _quote_postgres_identifier_path(identifier_path: str) -> str | None:
    """Safely quote a PostgreSQL table identifier path.

    Args:
        identifier_path: Table identifier, optionally schema-qualified.

    Returns:
        Quoted identifier path, or ``None`` when the input is invalid.
    """
    parts = identifier_path.split(".")
    if not parts or len(parts) > 2:
        return None
    if any(not _POSTGRES_IDENTIFIER_PATTERN.fullmatch(part) for part in parts):
        return None
    return ".".join(f'"{part}"' for part in parts)


def _get_table_row_count(db_url: str, table_name: str) -> int | None:
    """Return the row count of *table_name* or ``None`` if it does not exist.

    Args:
        db_url: Database connection URL.
        table_name: Table name to query.

    Returns:
        Row count, or ``None`` if the table does not exist or query fails.
    """
    quoted_table_name = _quote_postgres_identifier_path(table_name)
    if quoted_table_name is None:
        return None

    parsed = _parse_postgres_url(db_url)
    env = _build_psql_env(parsed["password"])
    cmd = [
        "psql",
        "-h",
        parsed["host"],
        "-p",
        str(parsed["port"]),
        "-U",
        parsed["user"],
        "-d",
        parsed["database"],
        "-t",
        "-A",
        "-c",
        f"SELECT COUNT(*) FROM {quoted_table_name}",
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _print_target_summary(
    db_url: str,
    date_str: str,
    backup_type: str,
    restore_items: list[str],
    clean_target_schema: bool,
    drop_target_db: bool,
    manifest_has_db: bool,
    sanitize_invalid_utf8: bool = False,
) -> None:
    """Print a masked summary of the restore target and mode."""
    parsed = _parse_postgres_url(db_url)
    print("\nTarget database:")
    print(f"  host: {parsed['host']}")
    print(f"  port: {parsed['port']}")
    print(f"  database: {parsed['database']}")
    print(f"  user: {parsed['user']}")
    print("Restore mode:")
    print(f"  database: {'yes' if 'database' in restore_items else 'no'}")
    print(f"  clean target schema: {'yes' if clean_target_schema else 'no'}")
    print(f"  drop target database: {'yes' if drop_target_db else 'no'}")
    print(f"  sanitize invalid utf-8: {'yes' if sanitize_invalid_utf8 else 'no'}")
    print("Backup:")
    print(f"  date: {date_str}")
    print(f"  type: {backup_type}")
    print(f"  database.sql.gz: {'present' if manifest_has_db else 'missing'}")


def _verify_and_summarize_tables(db_url: str, required_tables: list[str]) -> tuple[bool, list[str]]:
    """Print row-count summaries and verify required tables.

    Args:
        db_url: Target database URL.
        required_tables: Tables that must exist with >0 rows.

    Returns:
        Tuple of (verification_passed, warning_messages).
    """
    warnings: list[str] = []

    if not required_tables:
        return True, []

    print("\nPost-restore table summary:")
    for table in required_tables:
        count = _get_table_row_count(db_url, table)
        if count is None:
            msg = f"  {table}: not found"
            print(msg)
            warnings.append(f"Required table {table} does not exist.")
        else:
            print(f"  {table}: {count} rows")
            if count == 0:
                warnings.append(f"Required table {table} has 0 rows.")

    if warnings:
        for warning in warnings:
            print(f"[WARN] {warning}")
        return False, warnings
    return True, []


def _interactive_select_backup(client: RestoreClient, dates: list[str]) -> str:
    """Present an interactive numbered menu to choose a backup date."""
    print("\nAvailable backups:")
    for idx, d in enumerate(dates, start=1):
        manifest = client.get_backup_manifest(d)
        btype = manifest.get("type", "unknown") if manifest else "unknown"
        print(f"  [{idx}] {d} ({btype})")

    while True:
        raw = input(f"\nSelect backup to restore [1-{len(dates)}]: ").strip()
        if raw.isdigit():
            choice = int(raw)
            if 1 <= choice <= len(dates):
                return dates[choice - 1]
        print("Invalid selection. Please enter a number from the list.")


def _interactive_select_components() -> tuple[bool, bool, bool]:
    """Ask the user which components to restore."""
    print("\nComponents to restore:")
    print("  [1] Database")
    print("  [2] Logs")
    print("  [3] Resources")
    print("  [4] All of the above")

    while True:
        raw = input("Enter numbers separated by commas [1-4]: ").strip()
        if raw == "4":
            return True, True, True
        try:
            choices = {int(x.strip()) for x in raw.split(",")}
            if choices.issubset({1, 2, 3}):
                return 1 in choices, 2 in choices, 3 in choices
        except ValueError:
            pass
        print("Invalid selection.")


def _interactive_confirm(target_date: str, restore_items: list[str]) -> bool:
    """Ask the user to confirm before restoring."""
    print(f"\nThis will restore: {', '.join(restore_items)} from {target_date}")
    confirm = input("Continue? [y/N]: ").strip().lower()
    return confirm == "y"


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for restore CLI."""
    parser = argparse.ArgumentParser(
        description="Restore backups from S3. "
        "When no --date is given, runs in interactive mode.",
        add_help=False,
    )
    parser.add_argument(
        "--s3-endpoint",
        default=os.getenv("S3_ENDPOINT", ""),
        help="S3 endpoint URL (default: $S3_ENDPOINT)",
    )
    parser.add_argument(
        "--s3-access-key",
        default=os.getenv("S3_ACCESS_KEY", ""),
        help="S3 access key (default: $S3_ACCESS_KEY)",
    )
    parser.add_argument(
        "--s3-secret-key",
        default=os.getenv("S3_SECRET_KEY", ""),
        help="S3 secret key (default: $S3_SECRET_KEY)",
    )
    parser.add_argument(
        "--s3-bucket",
        default=os.getenv("S3_BUCKET", "app-backups"),
        help="S3 bucket (default: $S3_BUCKET or app-backups)",
    )
    parser.add_argument(
        "--s3-prefix",
        default=os.getenv("S3_PREFIX", "app-backups"),
        help="S3 key prefix (default: $S3_PREFIX or app-backups)",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", ""),
        help="Database URL (default: $DATABASE_URL)",
    )
    parser.add_argument(
        "--logs-dir",
        default=os.getenv("LOGS_DIR", "/app/backend/logs"),
        help="Logs directory (default: $LOGS_DIR or /app/backend/logs)",
    )
    parser.add_argument(
        "--resources-dir",
        default=os.getenv("RESOURCES_DIR", "/app/backend/data"),
        help="Resources directory (default: $RESOURCES_DIR or /app/backend/data)",
    )
    parser.add_argument(
        "--date",
        help="Target backup date to restore (YYYY-MM-DD_HHMMSS). "
        "If omitted, runs interactively.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available backups and exit (non-interactive)",
    )
    parser.add_argument("--restore-db", action="store_true", help="Restore database")
    parser.add_argument("--restore-logs", action="store_true", help="Restore logs")
    parser.add_argument("--restore-resources", action="store_true", help="Restore resources")
    parser.add_argument(
        "--chain",
        action="store_true",
        help="For file restores, apply the full + incremental chain "
        "up to --date instead of only the target backup",
    )
    parser.add_argument(
        "--clean-target-schema",
        action="store_true",
        help="Drop and recreate the public schema before restoring the database",
    )
    parser.add_argument(
        "--drop-target-db",
        action="store_true",
        help="Drop and recreate the target database before restoring (destructive)",
    )
    parser.add_argument(
        "--verify-table",
        action="append",
        default=[],
        metavar="TABLE",
        help="Require this table to exist with >0 rows after restore "
        "(can be given multiple times)",
    )
    parser.add_argument(
        "--sanitize-invalid-utf8",
        action="store_true",
        help="Replace invalid UTF-8 bytes in the database dump before restore "
        "(destructive data repair for dirty dumps)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt (non-interactive)",
    )
    parser.add_argument("-h", "--help", action="help", help="Show this help message and exit")
    return parser


def _validate_config(args: argparse.Namespace) -> list[str]:
    """Return a list of missing required configuration keys."""
    missing: list[str] = []
    if not args.s3_endpoint:
        missing.append("S3_ENDPOINT (--s3-endpoint)")
    if not args.s3_access_key:
        missing.append("S3_ACCESS_KEY (--s3-access-key)")
    if not args.s3_secret_key:
        missing.append("S3_SECRET_KEY (--s3-secret-key)")
    if not args.database_url:
        missing.append("DATABASE_URL (--database-url)")
    return missing


def run_restore(args: argparse.Namespace) -> int:
    """Execute restore workflow from parsed arguments."""
    if args.clean_target_schema and args.drop_target_db:
        print("Error: --clean-target-schema and --drop-target-db are mutually exclusive.")
        return 1

    missing = _validate_config(args)
    if missing:
        print(f"Missing required configuration: {', '.join(missing)}")
        print("\nSet environment variables or pass as CLI arguments.")
        return 1

    client = RestoreClient(
        args.s3_endpoint,
        args.s3_access_key,
        args.s3_secret_key,
        args.s3_bucket,
        args.s3_prefix,
    )

    dates = client.list_backup_dates()

    if args.list:
        print("Available backups:")
        for d in dates:
            manifest = client.get_backup_manifest(d)
            btype = manifest.get("type", "unknown") if manifest else "unknown"
            print(f"  {d} ({btype})")
        return 0

    if not args.date:
        if not dates:
            print("No backups found in S3.")
            return 1

        target_date = _interactive_select_backup(client, dates)
        restore_db, restore_logs, restore_resources = _interactive_select_components()

        manifest = client.get_backup_manifest(target_date)
        backup_type = manifest.get("type", "unknown") if manifest else "unknown"
        use_chain = False
        if backup_type == "incremental" and (restore_logs or restore_resources):
            chain_input = (
                input("\nUse chain restore (apply full + all incrementals)? [y/N]: ")
                .strip()
                .lower()
            )
            use_chain = chain_input == "y"

        restore_items: list[str] = []
        if restore_db:
            restore_items.append("database")
        if restore_logs:
            restore_items.append("logs")
        if restore_resources:
            restore_items.append("resources")

        db_key = _find_s3_key(manifest, "database.sql.gz") if manifest else None
        _print_target_summary(
            args.database_url,
            target_date,
            backup_type,
            restore_items,
            args.clean_target_schema,
            args.drop_target_db,
            manifest_has_db=db_key is not None,
            sanitize_invalid_utf8=args.sanitize_invalid_utf8,
        )

        if not _interactive_confirm(target_date, restore_items):
            print("Aborted.")
            return 0

        return _execute_restore(
            client,
            dates,
            target_date,
            restore_db,
            restore_logs,
            restore_resources,
            use_chain,
            args.database_url,
            args.logs_dir,
            args.resources_dir,
            clean_target_schema=args.clean_target_schema,
            drop_target_db=args.drop_target_db,
            verify_tables=args.verify_table,
            sanitize_invalid_utf8=args.sanitize_invalid_utf8,
            yes=False,
        )

    if args.date not in dates:
        print(f"Backup {args.date} not found.")
        return 1

    restore_items: list[str] = []
    if args.restore_db:
        restore_items.append("database")
    if args.restore_logs:
        restore_items.append("logs")
    if args.restore_resources:
        restore_items.append("resources")

    if not restore_items:
        print(
            "No restore targets specified. Use --restore-db, --restore-logs, --restore-resources."
        )
        return 1

    manifest = client.get_backup_manifest(args.date)
    backup_type = manifest.get("type", "unknown") if manifest else "unknown"
    db_key = _find_s3_key(manifest, "database.sql.gz") if manifest else None

    _print_target_summary(
        args.database_url,
        args.date,
        backup_type,
        restore_items,
        args.clean_target_schema,
        args.drop_target_db,
        manifest_has_db=db_key is not None,
        sanitize_invalid_utf8=args.sanitize_invalid_utf8,
    )

    if args.drop_target_db:
        print("\nWARNING: --drop-target-db will permanently delete the target database.")

    print(f"\nThis will restore: {', '.join(restore_items)} from {args.date}")
    if not args.yes:
        confirm = input("Continue? [y/N]: ")
        if confirm.lower() != "y":
            print("Aborted.")
            return 0

    return _execute_restore(
        client,
        dates,
        args.date,
        args.restore_db,
        args.restore_logs,
        args.restore_resources,
        args.chain,
        args.database_url,
        args.logs_dir,
        args.resources_dir,
        clean_target_schema=args.clean_target_schema,
        drop_target_db=args.drop_target_db,
        verify_tables=args.verify_table,
        sanitize_invalid_utf8=args.sanitize_invalid_utf8,
        yes=args.yes,
    )


def _execute_restore(
    client: RestoreClient,
    dates: list[str],
    date_str: str,
    restore_db: bool,
    restore_logs: bool,
    restore_resources: bool,
    chain: bool,
    database_url: str,
    logs_dir: str,
    resources_dir: str,
    clean_target_schema: bool = False,
    drop_target_db: bool = False,
    verify_tables: list[str] | None = None,
    sanitize_invalid_utf8: bool = False,
    yes: bool = False,
) -> int:
    """Execute the actual restore download-and-extract workflow."""
    manifest = client.get_backup_manifest(date_str)
    if not manifest:
        print(f"Could not find manifest for {date_str}")
        return 1

    backup_type = manifest.get("type", "unknown")
    print(f"\nTarget backup: {date_str} ({backup_type})")

    restore_items: list[str] = []
    if restore_db:
        restore_items.append("database")
    if restore_logs:
        restore_items.append("logs")
    if restore_resources:
        restore_items.append("resources")

    if not restore_items:
        print("No restore targets selected.")
        return 1

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        if restore_db:
            db_key = _find_s3_key(manifest, "database.sql.gz")
            if not db_key:
                print("[ERROR] Database backup not found in manifest.")
                return 1

            db_path = tmp / "database.sql.gz"
            client.download_backup_file(db_key, db_path)

            if clean_target_schema:
                try:
                    _clean_target_schema(database_url)
                except subprocess.CalledProcessError as exc:
                    print(f"[ERROR] Failed to clean target schema: {exc}")
                    return 1

            if drop_target_db:
                try:
                    _drop_and_recreate_target_db(database_url)
                except subprocess.CalledProcessError as exc:
                    print(f"[ERROR] Failed to drop and recreate target database: {exc}")
                    return 1

            try:
                restore_database(
                    database_url,
                    db_path,
                    sanitize_invalid_utf8=sanitize_invalid_utf8,
                )
            except subprocess.CalledProcessError as exc:
                print(f"[ERROR] Database restore failed: {exc}")
                return 1

        if restore_logs or restore_resources:
            if chain:
                full_date = _find_full_backup_before(client, dates, date_str)
                if not full_date:
                    print("[ERROR] No full backup found before target date. Cannot chain restore.")
                    return 1
                full_idx = dates.index(full_date)
                target_idx = dates.index(date_str)
                chain_dates = dates[full_idx : target_idx + 1]
                print(f"Chain restore: {' -> '.join(chain_dates)}")
                _restore_chain(client, chain_dates, logs_dir, resources_dir, tmp)
            else:
                if restore_logs:
                    logs_key = _find_s3_key(manifest, "logs.tar.gz")
                    if logs_key:
                        logs_path = tmp / "logs.tar.gz"
                        client.download_backup_file(logs_key, logs_path)
                        restore_archive(
                            logs_path,
                            logs_dir,
                            use_incremental=(backup_type == "incremental"),
                        )
                    else:
                        print("[WARN] Logs backup not found in manifest.")

                if restore_resources:
                    res_key = _find_s3_key(manifest, "resources.tar.gz")
                    if res_key:
                        res_path = tmp / "resources.tar.gz"
                        client.download_backup_file(res_key, res_path)
                        restore_archive(
                            res_path,
                            resources_dir,
                            use_incremental=(backup_type == "incremental"),
                        )
                    else:
                        print("[WARN] Resources backup not found in manifest.")

    if restore_db:
        if verify_tables is None:
            verify_tables = []
        passed, _ = _verify_and_summarize_tables(database_url, verify_tables)
        if not passed:
            return 1

    print("Restore completed successfully.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Standalone CLI entry point."""
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    return run_restore(args)


if __name__ == "__main__":
    sys.exit(main())
