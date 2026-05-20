"""Unit tests for backup restore service."""

from __future__ import annotations

import gzip
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.backup_service.restore import (
    _build_parser,
    _clean_target_schema,
    _drop_and_recreate_target_db,
    _execute_restore,
    _find_s3_key,
    _find_legacy_gzip_tail_index,
    _get_table_row_count,
    _parse_postgres_url,
    _pipe_gzip_to_command,
    _print_target_summary,
    _restore_postgres,
    _verify_and_summarize_tables,
    run_restore,
)


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, line: bytes) -> None:
        self.writes.append(line)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, cmd: list[str], **_kwargs: object) -> None:
        self.args = cmd
        self.stdin = _FakeStdin()
        self.returncode = 0

    def wait(self) -> int:
        return self.returncode


def _patch_popen_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> list[_FakeProcess]:
    fake_processes: list[_FakeProcess] = []

    def fake_popen(cmd: list[str], **kwargs: object) -> _FakeProcess:
        fake_process = _FakeProcess(cmd, **kwargs)
        fake_processes.append(fake_process)
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return fake_processes


class TestParsePostgresUrl:
    def test_parses_standard_url(self) -> None:
        url = "postgresql://user:pass@localhost:5432/dbname"
        result = _parse_postgres_url(url)
        assert result["host"] == "localhost"
        assert result["port"] == 5432
        assert result["database"] == "dbname"
        assert result["user"] == "user"
        assert result["password"] == "pass"

    def test_parses_psycopg2_url(self) -> None:
        url = "postgresql+psycopg2://admin:secret@db.example.com:5432/App_test"
        result = _parse_postgres_url(url)
        assert result["host"] == "db.example.com"
        assert result["port"] == 5432
        assert result["database"] == "App_test"
        assert result["user"] == "admin"
        assert result["password"] == "secret"

    def test_parses_url_without_password(self) -> None:
        url = "postgresql://user@host/db"
        result = _parse_postgres_url(url)
        assert result["password"] == ""
        assert result["host"] == "host"


class TestBuildParser:
    def test_mutually_exclusive_flags_not_enforced_by_parser(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--clean-target-schema", "--drop-target-db"])
        assert args.clean_target_schema is True
        assert args.drop_target_db is True

    def test_verify_table_appends(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--verify-table", "t1", "--verify-table", "t2"])
        assert args.verify_table == ["t1", "t2"]

    def test_sanitize_invalid_utf8_flag(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--sanitize-invalid-utf8"])
        assert args.sanitize_invalid_utf8 is True


class TestRunRestoreValidation:
    def test_mutual_exclusion_returns_nonzero(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "--s3-endpoint",
                "http://s3",
                "--s3-access-key",
                "key",
                "--s3-secret-key",
                "secret",
                "--database-url",
                "postgresql://u:p@h/db",
                "--date",
                "2026-05-15_180000",
                "--restore-db",
                "--clean-target-schema",
                "--drop-target-db",
                "--yes",
            ]
        )
        assert run_restore(args) == 1

    @patch("scripts.backup_service.restore.RestoreClient")
    def test_missing_db_dump_fails(self, mock_client_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.list_backup_dates.return_value = ["2026-05-15_180000"]
        manifest = {"type": "full", "files": []}
        mock_client.get_backup_manifest.return_value = manifest
        mock_client_cls.return_value = mock_client

        parser = _build_parser()
        args = parser.parse_args(
            [
                "--s3-endpoint",
                "http://s3",
                "--s3-access-key",
                "key",
                "--s3-secret-key",
                "secret",
                "--database-url",
                "postgresql://u:p@h/db",
                "--date",
                "2026-05-15_180000",
                "--restore-db",
                "--yes",
            ]
        )
        assert run_restore(args) == 1


class TestRestorePostgresCommand:
    @patch("scripts.backup_service.restore.shutil.which")
    @patch("scripts.backup_service.restore.subprocess.run")
    @patch("scripts.backup_service.restore._pipe_gzip_to_command")
    def test_includes_on_error_stop(
        self,
        mock_pipe: MagicMock,
        mock_run: MagicMock,
        mock_which: MagicMock,
    ) -> None:
        mock_which.return_value = "createdb"
        mock_run.return_value = MagicMock(returncode=0)
        mock_pipe.return_value = None

        db_url = "postgresql://user:pass@localhost:5432/testdb"
        sql_path = Path("/tmp/db.sql.gz")
        _restore_postgres(db_url, sql_path)

        mock_run.assert_any_call(
            ["createdb", "-h", "localhost", "-p", "5432", "-U", "user", "testdb"],
            env=mock_run.call_args_list[0][1]["env"],
            capture_output=True,
            check=True,
        )

        mock_pipe.assert_called_once()
        cmd = mock_pipe.call_args[0][1]
        assert "-b" in cmd
        assert "-v" in cmd
        assert "ON_ERROR_STOP=1" in cmd
        assert "--single-transaction" in cmd


class TestPipeGzipToCommand:
    def test_streams_binary_dump_without_utf8_decoding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sql_path = tmp_path / "database.sql.gz"
        with gzip.open(sql_path, "wb") as f:
            f.write(b"SET transaction_timeout = 0;\n")
            f.write(b"SELECT E'\\x8b';\n")
            f.write(b"COPY sample FROM stdin;\nvalue-\x8b\n\\.\n")

        fake_processes = _patch_popen_capture(monkeypatch)

        _pipe_gzip_to_command(sql_path, ["psql"])

        stdin = fake_processes[0].stdin
        assert b"SET transaction_timeout" not in b"".join(stdin.writes)
        assert b"value-\x8b" in b"".join(stdin.writes)
        assert stdin.closed is True

    def test_sanitizes_invalid_utf8_when_requested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sql_path = tmp_path / "database.sql.gz"
        with gzip.open(sql_path, "wb") as f:
            f.write(b"COPY sample FROM stdin;\nvalue-\x8b\n\\.\n")

        fake_processes = _patch_popen_capture(monkeypatch)

        _pipe_gzip_to_command(
            sql_path,
            ["psql"],
            sanitize_invalid_utf8=True,
        )

        stdin = fake_processes[0].stdin
        written_dump = b"".join(stdin.writes)
        assert b"value-\x8b" not in written_dump
        assert "value-�".encode("utf-8") in written_dump

    def test_ignores_embedded_gzip_tail_in_plain_sql_backup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sql_path = tmp_path / "database.sql.gz"
        sql_path.write_bytes(b"SELECT 1;\n" + b"\x1f\x8b\x08legacy-gzip-tail")

        fake_processes = _patch_popen_capture(monkeypatch)

        _pipe_gzip_to_command(sql_path, ["psql"])

        stdin = fake_processes[0].stdin
        assert b"".join(stdin.writes) == b"SELECT 1;\n"

    def test_ignores_split_embedded_gzip_filename_tail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sql_path = tmp_path / "database.sql.gz"
        sql_path.write_bytes(b"SELECT 1;\n\xffj\x02\xffdatabase.sql\x00tail")

        fake_processes = _patch_popen_capture(monkeypatch)

        _pipe_gzip_to_command(sql_path, ["psql"])

        stdin = fake_processes[0].stdin
        assert b"".join(stdin.writes) == b"SELECT 1;\n"


class TestFindLegacyGzipTailIndex:
    def test_detects_gzip_magic(self) -> None:
        assert _find_legacy_gzip_tail_index(b"SELECT 1;\x1f\x8b\x08tail") == 9

    def test_detects_split_filename_tail(self) -> None:
        assert _find_legacy_gzip_tail_index(b"\xffj\x02\xffdatabase.sql\x00") == 0

    def test_ignores_normal_sql_containing_filename(self) -> None:
        assert _find_legacy_gzip_tail_index(b"SELECT 'database.sql';\n") == -1


class TestCleanTargetSchema:
    @patch("scripts.backup_service.restore.subprocess.run")
    def test_executes_drop_and_create_schema(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        _clean_target_schema("postgresql://u:p@h:5432/db")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" in cmd
        assert "ON_ERROR_STOP=1" in cmd


class TestDropAndRecreateTargetDb:
    @patch("scripts.backup_service.restore.subprocess.run")
    def test_executes_dropdb_and_createdb(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        _drop_and_recreate_target_db("postgresql://u:p@h:5432/db")
        assert mock_run.call_count == 2
        drop_call = mock_run.call_args_list[0][0][0]
        create_call = mock_run.call_args_list[1][0][0]
        assert "dropdb" in drop_call[0]
        assert "--if-exists" in drop_call
        assert "createdb" in create_call[0]


class TestGetTableRowCount:
    @patch("scripts.backup_service.restore.subprocess.run")
    def test_returns_count_on_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="  42  \n")
        result = _get_table_row_count("postgresql://u:p@h:5432/db", "my_table")
        assert result == 42
        cmd = mock_run.call_args[0][0]
        assert 'SELECT COUNT(*) FROM "my_table"' in cmd

    @patch("scripts.backup_service.restore.subprocess.run")
    def test_returns_none_on_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="ERROR")
        result = _get_table_row_count("postgresql://u:p@h:5432/db", "missing")
        assert result is None

    @patch("scripts.backup_service.restore.subprocess.run")
    def test_rejects_unsafe_table_name(self, mock_run: MagicMock) -> None:
        result = _get_table_row_count(
            "postgresql://u:p@h:5432/db",
            "safe_table; DROP SCHEMA public CASCADE; --",
        )
        assert result is None
        mock_run.assert_not_called()


class TestVerifyAndSummarizeTables:
    @patch("scripts.backup_service.restore._get_table_row_count")
    def test_passes_when_required_tables_have_rows(
        self, mock_get_count: MagicMock
    ) -> None:
        mock_get_count.side_effect = lambda _url, table: {
            "required_t": 10,
        }.get(table, 0)

        passed, warnings = _verify_and_summarize_tables(
            "postgresql://u:p@h:5432/db", ["required_t"]
        )
        assert passed is True
        assert not warnings

    @patch("scripts.backup_service.restore._get_table_row_count")
    def test_passes_when_no_required_tables(self, mock_get_count: MagicMock) -> None:
        passed, warnings = _verify_and_summarize_tables(
            "postgresql://u:p@h:5432/db", []
        )
        assert passed is True
        assert not warnings

    @patch("scripts.backup_service.restore._get_table_row_count")
    def test_fails_when_required_table_is_zero(self, mock_get_count: MagicMock) -> None:
        mock_get_count.side_effect = lambda _url, table: {
            "required_t": 0,
        }.get(table, 0)

        passed, warnings = _verify_and_summarize_tables(
            "postgresql://u:p@h:5432/db", ["required_t"]
        )
        assert passed is False
        assert any("required_t" in w for w in warnings)

    @patch("scripts.backup_service.restore._get_table_row_count")
    def test_fails_when_required_table_missing(self, mock_get_count: MagicMock) -> None:
        mock_get_count.side_effect = lambda _url, table: None

        passed, warnings = _verify_and_summarize_tables(
            "postgresql://u:p@h:5432/db", ["required_t"]
        )
        assert passed is False
        assert any("required_t" in w for w in warnings)


class TestPrintTargetSummary:
    def test_does_not_expose_password(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_target_summary(
            "postgresql://user:secret@host:5432/db",
            "2026-05-15_180000",
            "full",
            ["database"],
            False,
            False,
            True,
        )
        captured = capsys.readouterr().out
        assert "host" in captured
        assert "secret" not in captured
        assert "database: db" in captured


class TestFindS3Key:
    def test_finds_existing_key(self) -> None:
        manifest = {"files": [{"name": "database.sql.gz", "s3_key": "s3/key"}]}
        assert _find_s3_key(manifest, "database.sql.gz") == "s3/key"

    def test_returns_none_for_missing(self) -> None:
        manifest = {"files": [{"name": "other.tar.gz", "s3_key": "s3/other"}]}
        assert _find_s3_key(manifest, "database.sql.gz") is None


class TestExecuteRestore:
    @patch("scripts.backup_service.restore.RestoreClient")
    def test_missing_db_dump_returns_nonzero(self, mock_client_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.get_backup_manifest.return_value = {
            "type": "full",
            "files": [],
        }
        mock_client_cls.return_value = mock_client

        result = _execute_restore(
            client=mock_client,
            dates=["2026-05-15_180000"],
            date_str="2026-05-15_180000",
            restore_db=True,
            restore_logs=False,
            restore_resources=False,
            chain=False,
            database_url="postgresql://u:p@h/db",
            logs_dir="/app/logs",
            resources_dir="/app/data",
        )
        assert result == 1
