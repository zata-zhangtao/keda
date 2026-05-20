"""Reusable infrastructure-level helper functions."""

from collections.abc import Callable
from datetime import datetime
import json
from typing import Any, Optional


def parse_datetime(date_str: str, fmt: str = "%Y/%m/%d %H:%M") -> Optional[datetime]:
    """Parse a datetime string into a ``datetime`` object.

    Args:
        date_str (str): Input datetime string.
        fmt (str): Datetime format string.

    Returns:
        Optional[datetime]: Parsed datetime, or ``None`` when parsing fails.
    """
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, fmt)
    except ValueError:
        return None


def safe_json_loads(json_str: str, default: Any = None) -> Any:
    """Safely parse JSON text.

    Args:
        json_str (str): Raw JSON string.
        default (Any): Value to return when parsing fails.

    Returns:
        Any: Parsed object or ``default``.
    """
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return default


def safe_get_nested(data: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    """Safely read a nested dictionary path.

    Args:
        data (dict[str, Any]): Source dictionary.
        keys (list[str]): Nested key path.
        default (Any): Value returned when any lookup fails.

    Returns:
        Any: Resolved nested value or ``default``.
    """
    try:
        resolved_value: Any = data
        for nested_key in keys:
            resolved_value = resolved_value[nested_key]
        return resolved_value
    except (KeyError, TypeError, IndexError):
        return default


def truncate_string(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """Truncate a string to a maximum length.

    Args:
        text (str): Original string.
        max_length (int): Maximum output length.
        suffix (str): Suffix appended after truncation.

    Returns:
        str: Truncated string.
    """
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


def normalize_whitespace(text: str) -> str:
    """Collapse repeated whitespace into single spaces.

    Args:
        text (str): Original string.

    Returns:
        str: Normalized string.
    """
    return " ".join(text.split())


def chunks(values: list[Any], size: int) -> list[list[Any]]:
    """Split a list into fixed-size chunks.

    Args:
        values (list[Any]): Input list.
        size (int): Chunk size.

    Returns:
        list[list[Any]]: Chunked values.
    """
    return [values[index : index + size] for index in range(0, len(values), size)]


def retry_on_exception(
    func: Callable[[], Any],
    max_retries: int = 3,
    delay: float = 1.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    default: Any = None,
) -> Any:
    """Retry a callable when selected exceptions are raised.

    Args:
        func (Callable[[], Any]): Callable to execute.
        max_retries (int): Maximum retry attempts.
        delay (float): Delay between retries in seconds.
        exceptions (tuple[type[BaseException], ...]): Retryable exceptions.
        default (Any): Fallback value after final failure.

    Returns:
        Any: Callable result or ``default``.
    """
    import time

    for attempt_index in range(max_retries):
        try:
            return func()
        except exceptions:
            if attempt_index == max_retries - 1:
                return default
            time.sleep(delay)
    return default
