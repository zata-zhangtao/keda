"""Application logging configuration."""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from typing import Any

from backend.infrastructure.config.settings import config


class Logger:
    """Singleton logger manager."""

    _instance: "Logger | None" = None
    _logger: logging.Logger | None = None

    def __new__(cls) -> "Logger":
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
            file_handler = TimedRotatingFileHandler(
                filename=str(config.log_file),
                when="midnight",
                interval=1,
                backupCount=14,
                encoding="utf-8",
                utc=False,
            )
            file_handler.suffix = "%Y-%m-%d"
            file_handler.setLevel(getattr(logging, config.log_level))
            file_handler.setFormatter(formatter)
            self._logger.addHandler(file_handler)
        except (OSError, PermissionError) as error:
            print(f"Warning: 无法创建日志文件处理器: {error}")

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
