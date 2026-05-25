"""Application logging configuration."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.infrastructure.config.settings import config


class Logger:
    """Singleton logger manager with daily log files."""

    _instance: Logger | None = None
    _logger: logging.Logger | None = None

    def __new__(cls) -> Logger:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if self._logger is None:
            self._setup_logger()

    def _setup_logger(self) -> None:
        self._logger = logging.getLogger(config.app_name)
        self._logger.setLevel(getattr(logging, config.log_level))

        if self._logger.handlers:
            return

        formatter = logging.Formatter(
            fmt="%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, config.log_level))
        console_handler.setFormatter(formatter)
        if hasattr(console_handler.stream, "reconfigure"):
            try:
                console_handler.stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        self._logger.addHandler(console_handler)

        try:
            log_dir = Path(config.log_file).parent
            log_dir.mkdir(parents=True, exist_ok=True)

            today = datetime.now().strftime("%Y-%m-%d")
            log_path = log_dir / f"app-{today}.log"

            file_handler = logging.FileHandler(
                filename=str(log_path),
                encoding="utf-8",
            )
            file_handler.setLevel(getattr(logging, config.log_level))
            file_handler.setFormatter(formatter)
            self._logger.addHandler(file_handler)

            self._cleanup_old_logs(log_dir, keep_days=14)
        except (OSError, PermissionError) as error:
            print(f"Warning: 无法创建日志文件处理器: {error}")

    def _cleanup_old_logs(self, log_dir: Path, keep_days: int) -> None:
        """Remove log files older than keep_days.

        Args:
            log_dir: Directory containing log files.
            keep_days: Number of days to keep log files.
        """
        cutoff = datetime.now() - timedelta(days=keep_days)
        for path in log_dir.glob("app-*.log"):
            try:
                date_str = path.stem.replace("app-", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    os.remove(path)
            except (ValueError, OSError):
                pass

    def get_logger(self) -> logging.Logger:
        """Return the underlying ``logging.Logger`` instance."""
        if self._logger is None:
            self._setup_logger()
        return self._logger

    def __getattr__(self, name: str) -> Any:
        if self._logger is None:
            self._setup_logger()
        return getattr(self._logger, name)


logger = Logger()

__all__ = ["Logger", "logger"]
