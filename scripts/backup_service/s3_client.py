"""S3-compatible storage client for backup upload and retention cleanup."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config


class S3Client:
    """Thin wrapper around boto3 S3 for backup operations."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
    ) -> None:
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(connect_timeout=30, read_timeout=300),
        )

    def upload_file(self, file_path: Path, key: str) -> None:
        """Upload a local file to S3."""
        self.client.upload_file(str(file_path), self.bucket, key)

    def upload_json(self, data: dict, key: str) -> None:
        """Upload a JSON dictionary to S3."""
        body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )

    _BACKUP_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")

    def cleanup_old_backups(self, prefix: str, retention_days: int) -> int:
        """Delete backup objects under *prefix* older than *retention_days*.

        Only objects inside timestamp directories (``YYYY-MM-DD_HHMMSS``) are
        removed so unrelated data under the same prefix is not touched.

        Returns:
            Number of deleted objects.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        deleted = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix + "/"):
            for obj in page.get("Contents", []):
                if obj["LastModified"] >= cutoff:
                    continue
                relative = obj["Key"].removeprefix(prefix + "/")
                first_part = relative.split("/", 1)[0]
                if self._BACKUP_DIR_RE.match(first_part):
                    self.client.delete_object(Bucket=self.bucket, Key=obj["Key"])
                    deleted += 1
        return deleted
