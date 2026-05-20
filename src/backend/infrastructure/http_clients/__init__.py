"""HTTP 客户端（http_clients）。

封装对外部 API 的 HTTP 调用。
提供可被 backend/engines/ 调用的网络请求适配器。
"""

from .proxy_manager import ClashProxyManager, TunnelProxyManager, create_proxy_manager

__all__ = ["ClashProxyManager", "TunnelProxyManager", "create_proxy_manager"]
