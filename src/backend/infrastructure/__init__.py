"""基础设施层（infrastructure）。

放置模型客户端、数据库、HTTP 客户端、配置和日志的具体实现。
对接外部 API、物理数据库和文件系统。

依赖规则：
    - 只提供可被内层调用的具体实现
    - 不包含任何业务编排逻辑
    - 不依赖 backend/core/、backend/engines/、backend/api/
"""

from .helpers import (
    chunks,
    normalize_whitespace,
    parse_datetime,
    retry_on_exception,
    safe_get_nested,
    safe_json_loads,
    truncate_string,
)

__all__ = [
    "chunks",
    "normalize_whitespace",
    "parse_datetime",
    "retry_on_exception",
    "safe_get_nested",
    "safe_json_loads",
    "truncate_string",
]
