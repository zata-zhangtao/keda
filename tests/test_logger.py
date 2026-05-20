"""Tests for logger configuration."""

from logging.handlers import TimedRotatingFileHandler

from backend.infrastructure.logging.logger import Logger


def test_logger_uses_timed_rotating_file_handler() -> None:
    logger_instance = Logger().get_logger()
    handler_types = {type(handler) for handler in logger_instance.handlers}
    assert TimedRotatingFileHandler in handler_types


def test_timed_rotating_file_handler_suffix_set() -> None:
    logger_instance = Logger().get_logger()
    rotating_handlers = [
        handler
        for handler in logger_instance.handlers
        if isinstance(handler, TimedRotatingFileHandler)
    ]
    assert rotating_handlers, "TimedRotatingFileHandler is not configured."
    assert rotating_handlers[0].suffix == "%Y-%m-%d"
