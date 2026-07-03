"""Backup & Restore CLI.

Usage:
    cd scripts/backup_service
    uv run python main.py                    # Interactive menu
    uv run python main.py backup             # Start backup scheduler
    uv run python main.py restore            # Interactive restore
    uv run python main.py restore --date ... # Non-interactive restore
    uv run python main.py list               # List available backups
"""

import hashlib
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from scripts.backup_service.archiver import archive_directory
from scripts.backup_service.config import BackupConfig
from scripts.backup_service.db import backup_database
from scripts.backup_service.restore import RestoreClient
from scripts.backup_service.restore import main as restore_main
from scripts.backup_service.s3_client import S3Client


def _should_run(now: datetime, backup_time: str) -> bool:
    """Return whether *now* matches the scheduled backup time (HH:MM)."""
    expected_hour, expected_minute = map(int, backup_time.split(":"))
    return now.hour == expected_hour and now.minute == expected_minute


def _is_full_backup_day(now: datetime, full_day: int) -> bool:
    """Return whether today is the configured full-backup day.

    Weekday mapping follows Python's ``datetime.weekday()``:
    Monday = 0, ..., Sunday = 6.
    """
    return now.weekday() == full_day


def _compute_sha256(file_path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    hash_sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_sha256.update(chunk)
    return hash_sha256.hexdigest()


def _run_backup(config: BackupConfig, s3: S3Client) -> None:
    """Execute a single backup cycle: DB, logs, resources, upload, cleanup."""
    now = datetime.now(timezone.utc)
    timestamp: str = now.strftime("%Y-%m-%d_%H%M%S")
    backup_type: str = "full" if _is_full_backup_day(now, config.full_backup_day) else "incremental"

    work_dir = Path(config.work_dir) / timestamp
    work_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "timestamp": timestamp,
        "type": backup_type,
        "files": [],
    }

    # 1. Database backup
    db_output = work_dir / "database.sql.gz"
    try:
        backup_database(config.database_url, db_output)
        db_key = f"{config.s3_prefix}/{timestamp}/{backup_type}/database.sql.gz"
        s3.upload_file(db_output, db_key)
        manifest["files"].append(
            {
                "name": "database.sql.gz",
                "size": db_output.stat().st_size,
                "s3_key": db_key,
                "sha256": _compute_sha256(db_output),
            }
        )
        print(f"[OK] Database backup uploaded: {db_key}")
    except Exception as exc:
        manifest["database_error"] = str(exc)
        print(f"[ERROR] Database backup failed: {exc}", file=sys.stderr)
        traceback.print_exc()

    # 2. Logs backup
    logs_dir = Path(config.logs_dir)
    if logs_dir.exists() and any(logs_dir.iterdir()):
        logs_output = work_dir / "logs.tar.gz"
        snapshot = Path(config.work_dir) / ".snapshot.logs"
        try:
            archive_directory(logs_dir, logs_output, snapshot, backup_type == "full")
            logs_key = f"{config.s3_prefix}/{timestamp}/{backup_type}/logs.tar.gz"
            s3.upload_file(logs_output, logs_key)
            manifest["files"].append(
                {
                    "name": "logs.tar.gz",
                    "size": logs_output.stat().st_size,
                    "s3_key": logs_key,
                    "sha256": _compute_sha256(logs_output),
                }
            )
            print(f"[OK] Logs backup uploaded: {logs_key}")
        except Exception as exc:
            manifest["logs_error"] = str(exc)
            print(f"[ERROR] Logs backup failed: {exc}", file=sys.stderr)
            traceback.print_exc()

    # 3. Resources backup
    resources_dir = Path(config.resources_dir)
    if resources_dir.exists() and any(resources_dir.iterdir()):
        resources_output = work_dir / "resources.tar.gz"
        snapshot = Path(config.work_dir) / ".snapshot.resources"
        try:
            archive_directory(
                resources_dir,
                resources_output,
                snapshot,
                backup_type == "full",
            )
            resources_key = f"{config.s3_prefix}/{timestamp}/{backup_type}/resources.tar.gz"
            s3.upload_file(resources_output, resources_key)
            manifest["files"].append(
                {
                    "name": "resources.tar.gz",
                    "size": resources_output.stat().st_size,
                    "s3_key": resources_key,
                    "sha256": _compute_sha256(resources_output),
                }
            )
            print(f"[OK] Resources backup uploaded: {resources_key}")
        except Exception as exc:
            manifest["resources_error"] = str(exc)
            print(f"[ERROR] Resources backup failed: {exc}", file=sys.stderr)
            traceback.print_exc()

    # 4. Upload manifest
    manifest_key = f"{config.s3_prefix}/{timestamp}/{backup_type}/manifest.json"
    s3.upload_json(manifest, manifest_key)
    print(f"[OK] Manifest uploaded: {manifest_key}")

    # 5. Cleanup local work directory
    shutil.rmtree(work_dir)

    # 6. Cleanup old S3 backups
    try:
        deleted = s3.cleanup_old_backups(config.s3_prefix, config.retention_days)
        if deleted:
            print(f"[OK] Cleaned up {deleted} old backup objects from S3")
    except Exception as exc:
        print(f"[WARN] Failed to cleanup old backups: {exc}", file=sys.stderr)

    print(f"[DONE] Backup completed at {timestamp} ({backup_type})")


def run_backup_scheduler() -> int:
    """Run the internal backup scheduler loop."""
    config = BackupConfig.from_env()
    config.validate()

    s3 = S3Client(
        endpoint=config.s3_endpoint,
        access_key=config.s3_access_key,
        secret_key=config.s3_secret_key,
        bucket=config.s3_bucket,
    )

    print(
        f"Backup service started. Schedule: {config.backup_time} UTC, "
        f"full backup on weekday {config.full_backup_day}"
    )

    last_run_minute: str | None = None

    while True:
        now = datetime.now(timezone.utc)
        current_minute = now.strftime("%Y-%m-%d_%H:%M")

        if _should_run(now, config.backup_time) and current_minute != last_run_minute:
            try:
                _run_backup(config, s3)
                last_run_minute = current_minute
            except Exception as exc:
                print(f"[FATAL] Backup cycle failed: {exc}", file=sys.stderr)
                last_run_minute = current_minute

        time.sleep(10)

    return 0


def _select_option(options: list[tuple[str, str]], title: str) -> str:
    """Display a numbered menu and return the selected key."""
    print(f"\n{title}")
    print("=" * len(title))
    for idx, (_, label) in enumerate(options, start=1):
        print(f"  [{idx}] {label}")

    while True:
        raw = input(f"\nSelect [1-{len(options)}]: ").strip()
        if raw.isdigit():
            choice = int(raw)
            if 1 <= choice <= len(options):
                return options[choice - 1][0]
        print("Invalid selection.")


def _run_backup_now() -> int:
    """Run a single backup cycle immediately."""
    try:
        config = BackupConfig.from_env()
        config.validate()
        s3 = S3Client(
            endpoint=config.s3_endpoint,
            access_key=config.s3_access_key,
            secret_key=config.s3_secret_key,
            bucket=config.s3_bucket,
        )
        print("Running backup now...\n")
        _run_backup(config, s3)
    except Exception as exc:
        print(f"[ERROR] Backup failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _list_backups() -> int:
    """List available backups from S3."""
    try:
        config = BackupConfig.from_env()
        config.validate()
        client = RestoreClient(
            config.s3_endpoint,
            config.s3_access_key,
            config.s3_secret_key,
            config.s3_bucket,
            config.s3_prefix,
        )
        dates = client.list_backup_dates()
        if not dates:
            print("No backups found.")
            return 0

        print("\nAvailable backups:")
        for idx, d in enumerate(dates, start=1):
            manifest = client.get_backup_manifest(d)
            btype = manifest.get("type", "unknown") if manifest else "unknown"
            print(f"  [{idx}] {d} ({btype})")
    except Exception as exc:
        print(f"[ERROR] Failed to list backups: {exc}", file=sys.stderr)
        return 1
    return 0


def _run_menu() -> int:
    """Display the main menu and dispatch."""
    options = [
        ("backup_now", "Run backup now"),
        ("scheduler", "Start backup scheduler"),
        ("restore", "Restore from backup"),
        ("list", "List available backups"),
        ("exit", "Exit"),
    ]

    while True:
        key = _select_option(options, "Backup Service")

        if key == "exit":
            print("Goodbye.")
            return 0
        if key == "backup_now":
            _run_backup_now()
        elif key == "scheduler":
            print("Starting backup scheduler (Ctrl+C to stop)...\n")
            return run_backup_scheduler()
        elif key == "restore":
            return restore_main([])
        elif key == "list":
            _list_backups()

        print()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Without arguments: display interactive menu.
    With arguments: dispatch directly to the requested command.
    """
    load_dotenv()

    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        return _run_menu()

    command = argv[0]
    remaining = argv[1:]

    if command == "backup":
        return run_backup_scheduler()
    if command == "backup-now":
        return _run_backup_now()
    if command == "restore":
        return restore_main(remaining)
    if command == "list":
        return _list_backups()

    print(f"Unknown command: {command}")
    print("\nCommands: backup, backup-now, restore, list")
    print("Run without arguments for interactive menu.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
