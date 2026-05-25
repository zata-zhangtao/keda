"""Tests for logger configuration."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

from backend.infrastructure.logging.logger import Logger


def test_logger_uses_daily_file_handler(tmp_path: Path) -> None:
    """Logger should use daily-named FileHandler."""
    log_file = str(tmp_path / "app-2026-05-24.log")
    with patch("backend.infrastructure.logging.logger.config") as mock_config:
        mock_config.app_name = "test_daily_fh"
        mock_config.log_level = "INFO"
        mock_config.log_file = log_file
        # Reset singleton for clean test
        Logger._instance = None
        Logger._logger = None
        try:
            logger_instance = Logger().get_logger()
            handler_types = {type(handler) for handler in logger_instance.handlers}
            assert logging.FileHandler in handler_types
            assert logging.StreamHandler in handler_types
        finally:
            # Clean up handlers to avoid leaking to other tests
            for handler in logger_instance.handlers[:]:
                handler.close()
                logger_instance.removeHandler(handler)
            Logger._instance = None
            Logger._logger = None


def test_logger_file_handler_uses_daily_filename(tmp_path: Path) -> None:
    """FileHandler should write to app-YYYY-MM-DD.log."""
    from datetime import datetime

    log_dir = tmp_path / "logs"
    log_file = str(log_dir / "app.log")
    with patch("backend.infrastructure.logging.logger.config") as mock_config:
        mock_config.app_name = "test_daily_name"
        mock_config.log_level = "INFO"
        mock_config.log_file = log_file
        Logger._instance = None
        Logger._logger = None
        try:
            logger_instance = Logger().get_logger()
            file_handlers = [
                h
                for h in logger_instance.handlers
                if isinstance(h, logging.FileHandler)
            ]
            assert file_handlers, "FileHandler is not configured."
            today = datetime.now().strftime("%Y-%m-%d")
            assert f"app-{today}.log" in file_handlers[0].baseFilename
        finally:
            for handler in logger_instance.handlers[:]:
                handler.close()
                logger_instance.removeHandler(handler)
            Logger._instance = None
            Logger._logger = None


def test_logger_propagate_false(tmp_path: Path) -> None:
    """Logger singleton should have propagate=False after configure_cli_logging."""
    log_file = str(tmp_path / "app.log")
    with patch("backend.infrastructure.logging.logger.config") as mock_config:
        mock_config.app_name = "test_propagate"
        mock_config.log_level = "INFO"
        mock_config.log_file = log_file
        Logger._instance = None
        Logger._logger = None
        try:
            logger_instance = Logger().get_logger()
            # Before configure_cli_logging, propagate may be True (default).
            # After calling configure_cli_logging, it should be False.
            from backend.engines.agent_runner.factory import configure_cli_logging

            configure_cli_logging()
            assert logger_instance.propagate is False
        finally:
            for handler in logger_instance.handlers[:]:
                handler.close()
                logger_instance.removeHandler(handler)
            Logger._instance = None
            Logger._logger = None


def test_cleanup_old_logs(tmp_path: Path) -> None:
    """_cleanup_old_logs should remove log files older than keep_days."""
    from datetime import datetime, timedelta

    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Create old log file
    old_date = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
    old_file = log_dir / f"app-{old_date}.log"
    old_file.write_text("old log content", encoding="utf-8")

    # Create recent log file
    recent_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    recent_file = log_dir / f"app-{recent_date}.log"
    recent_file.write_text("recent log content", encoding="utf-8")

    log_file = str(log_dir / "app.log")
    with patch("backend.infrastructure.logging.logger.config") as mock_config:
        mock_config.app_name = "test_cleanup"
        mock_config.log_level = "INFO"
        mock_config.log_file = log_file
        Logger._instance = None
        Logger._logger = None
        try:
            logger_instance = Logger()
            # Old file should be cleaned up, recent file should remain
            assert not old_file.exists()
            assert recent_file.exists()
        finally:
            for handler in logger_instance.handlers[:]:
                handler.close()
                logger_instance.removeHandler(handler)
            Logger._instance = None
            Logger._logger = None
