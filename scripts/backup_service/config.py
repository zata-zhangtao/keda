"""Backup service configuration."""

import os
from dataclasses import dataclass


@dataclass
class BackupConfig:
    """Backup service configuration loaded from environment variables."""

    database_url: str
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    s3_prefix: str
    backup_time: str
    full_backup_day: int
    logs_dir: str
    resources_dir: str
    work_dir: str
    retention_days: int

    @classmethod
    def from_env(cls) -> "BackupConfig":
        """Load configuration from environment variables."""
        return cls(
            database_url=os.getenv("DATABASE_URL", ""),
            s3_endpoint=os.getenv("S3_ENDPOINT", ""),
            s3_access_key=os.getenv("S3_ACCESS_KEY", ""),
            s3_secret_key=os.getenv("S3_SECRET_KEY", ""),
            s3_bucket=os.getenv("S3_BUCKET", "app-backups"),
            s3_prefix=os.getenv("S3_PREFIX", "app-backups"),
            backup_time=os.getenv("BACKUP_TIME", "18:00"),
            full_backup_day=int(os.getenv("FULL_BACKUP_DAY", "6")),
            logs_dir=os.getenv("LOGS_DIR", "/app/backend/logs"),
            resources_dir=os.getenv("RESOURCES_DIR", "/app/backend/data"),
            work_dir=os.getenv("WORK_DIR", "/tmp/backups"),
            retention_days=int(os.getenv("RETENTION_DAYS", "30")),
        )

    def validate(self) -> None:
        """Validate required configuration values."""
        if not self.database_url:
            raise ValueError("DATABASE_URL is required")
        if not self.s3_endpoint:
            raise ValueError("S3_ENDPOINT is required")
        if not self.s3_access_key:
            raise ValueError("S3_ACCESS_KEY is required")
        if not self.s3_secret_key:
            raise ValueError("S3_SECRET_KEY is required")
