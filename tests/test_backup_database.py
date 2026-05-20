"""Unit tests for backup database utilities."""

from __future__ import annotations

import gzip
import io
import subprocess
from pathlib import Path
from unittest.mock import patch

from scripts.backup_service.db import _backup_postgres


class _FakeDumpProcess:
    def __init__(self, stdout_payload: bytes, return_code: int = 0) -> None:
        self.stdout = io.BytesIO(stdout_payload)
        self.return_code = return_code

    def wait(self) -> int:
        return self.return_code


@patch("scripts.backup_service.db.subprocess.Popen")
def test_backup_postgres_streams_pg_dump_through_gzip(
    mock_popen,
    tmp_path: Path,
) -> None:
    dump_payload = b"CREATE TABLE sample(id integer);\n"
    mock_popen.return_value = _FakeDumpProcess(dump_payload)
    output_path = tmp_path / "database.sql.gz"

    _backup_postgres(
        "postgresql://user:secret@localhost:5432/app",
        output_path,
    )

    assert output_path.read_bytes().startswith(b"\x1f\x8b")
    with gzip.open(output_path, "rb") as f:
        assert f.read() == dump_payload

    cmd = mock_popen.call_args[0][0]
    env = mock_popen.call_args[1]["env"]
    assert "--encoding=UTF8" in cmd
    assert env["PGPASSWORD"] == "secret"
    assert env["PGCLIENTENCODING"] == "UTF8"
    assert mock_popen.call_args[1]["stdout"] == subprocess.PIPE


@patch("scripts.backup_service.db.subprocess.Popen")
def test_backup_postgres_raises_when_pg_dump_fails(
    mock_popen,
    tmp_path: Path,
) -> None:
    mock_popen.return_value = _FakeDumpProcess(b"partial dump\n", return_code=2)

    try:
        _backup_postgres(
            "postgresql://user:secret@localhost:5432/app",
            tmp_path / "database.sql.gz",
        )
    except subprocess.CalledProcessError as exc:
        assert exc.returncode == 2
    else:
        raise AssertionError("Expected CalledProcessError")
