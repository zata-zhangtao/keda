"""代理管理器模块

提供 Clash 和 HTTP 隧道代理的管理功能。
"""

import os
import urllib.parse
import random
import time
import requests
from typing import List, Optional, Dict, Union

from backend.infrastructure.logging.logger import logger
from backend.infrastructure.config.settings import config


class ClashProxyManager:
    """Clash代理管理器，用于管理Clash代理的切换"""

    def __init__(
        self,
        api_url: Optional[str] = None,
        secret: Optional[str] = None,
        group_name: Optional[str] = None,
        proxy_port: Optional[int] = None,
    ):
        """初始化Clash代理管理器

        Args:
            api_url (str, optional): Clash API地址，不指定则从配置文件加载
            secret (str, optional): Clash API密钥，不指定则从配置文件加载
            group_name (str, optional): 代理组名称，不指定则从配置文件加载
            proxy_port (int, optional): 代理端口，不指定则从配置文件加载

        Examples:
            >>> # 使用配置文件中的默认值
            >>> manager = ClashProxyManager()
            >>>
            >>> # 指定自定义参数
            >>> manager = ClashProxyManager(
            >>>     api_url="http://localhost:9090",
            >>>     secret="your-secret",
            >>>     group_name="Proxy",
            >>>     proxy_port=7890
            >>> )
        """
        # 从配置文件加载默认值，如果没有提供参数的话
        self.api_url = api_url or getattr(config, "CLASH_API_URL", None)
        self.secret = secret or getattr(config, "CLASH_API_SECRET", None)
        self.group_name = group_name or getattr(config, "CLASH_PROXY_GROUP", None)
        self.proxy_port = proxy_port or getattr(config, "CLASH_PROXY_PORT", None)

        self.api_url = self.api_url.rstrip("/")

        # 对组名进行URL编码，用于API调用
        self.encoded_group_name = urllib.parse.quote(self.group_name)
        self.proxies_list: List[str] = []
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if self.secret:
            self.headers["Authorization"] = f"Bearer {self.secret}"

        # HTTP代理配置
        self.http_proxy = f"http://127.0.0.1:{self.proxy_port}"
        self.https_proxy = f"http://127.0.0.1:{self.proxy_port}"
        self.proxy_config = {"http": self.http_proxy, "https": self.https_proxy}

    def get_proxies(self) -> List[str]:
        """获取指定组的所有可用代理节点

        Returns:
            List[str]: 代理节点名称列表

        Examples:
            >>> manager = ClashProxyManager()
            >>> proxies = manager.get_proxies()
            >>> print(f"Found {len(proxies)} proxies")
        """
        try:
            resp = requests.get(
                f"{self.api_url}/proxies", headers=self.headers, timeout=10
            )
            resp.raise_for_status()

            proxies_data = resp.json()["proxies"]
            logger.info("Available proxy groups:")
            for group_name in proxies_data:
                if proxies_data[group_name].get("type") == "select":
                    try:
                        logger.info(f"  - {group_name}")
                    except UnicodeEncodeError:
                        safe_name = group_name.encode("ascii", "ignore").decode("ascii")
                        if safe_name:
                            logger.info(f"  - {safe_name}")
                        else:
                            logger.info(
                                f"  - [包含特殊字符的组名] ({len(group_name)} chars)"
                            )

            if self.group_name not in proxies_data:
                logger.warning(
                    f"Group '{self.group_name}' not found. Using first available select group."
                )
                for group_name in proxies_data:
                    if proxies_data[group_name].get("type") == "select":
                        self.group_name = group_name
                        self.encoded_group_name = urllib.parse.quote(group_name)
                        break

            if self.group_name in proxies_data:
                self.proxies_list = list(proxies_data[self.group_name]["all"])
                try:
                    logger.info(
                        f"Found {len(self.proxies_list)} proxies in group '{self.group_name}'"
                    )
                except UnicodeEncodeError:
                    safe_name = self.group_name.encode("ascii", "ignore").decode(
                        "ascii"
                    )
                    logger.info(
                        f"Found {len(self.proxies_list)} proxies in group '{safe_name or 'proxy-group'}'"
                    )
                return self.proxies_list
            else:
                logger.error("No select-type proxy group found")
                return []

        except Exception as e:
            logger.error(f"Failed to get proxies: {e}")
            return []

    def switch_proxy(
        self, proxy_name: Optional[str] = None, max_attempts: int = 5
    ) -> bool:
        """切换到指定的代理节点，如果不指定则随机选择

        如果切换后的代理不可用，会继续尝试其他代理。

        Args:
            proxy_name (str, optional): 要切换到的代理节点名称，不指定则随机选择
            max_attempts (int): 最大尝试次数，默认5次

        Returns:
            bool: 切换是否成功

        Examples:
            >>> manager = ClashProxyManager()
            >>> manager.get_proxies()
            >>> # 随机切换
            >>> manager.switch_proxy()
            >>> # 切换到指定代理
            >>> manager.switch_proxy("HK-01")
        """
        if not self.proxies_list:
            logger.warning("No proxies available. Call get_proxies() first.")
            self.get_proxies()

        if not self.proxies_list:
            logger.error("Still no proxies available after refresh")
            return False

        tried_proxies = set()
        attempts = 0

        while attempts < max_attempts and len(tried_proxies) < len(self.proxies_list):
            try:
                # 选择代理
                if proxy_name is None or attempts > 0:
                    available_proxies = [
                        p for p in self.proxies_list if p not in tried_proxies
                    ]
                    if not available_proxies:
                        break
                    new_proxy = random.choice(available_proxies)
                else:
                    if proxy_name not in self.proxies_list:
                        logger.error(
                            f"Proxy '{proxy_name}' not found in available proxies"
                        )
                        return False
                    new_proxy = proxy_name

                tried_proxies.add(new_proxy)
                attempts += 1

                try:
                    logger.info(
                        f"Switching to proxy: {new_proxy} (attempt {attempts}/{max_attempts})"
                    )
                except UnicodeEncodeError:
                    safe_name = new_proxy.encode("ascii", "ignore").decode("ascii")
                    logger.info(
                        f"Switching to proxy: {safe_name or 'proxy-node'} (attempt {attempts}/{max_attempts})"
                    )

                resp = requests.put(
                    f"{self.api_url}/proxies/{self.encoded_group_name}",
                    json={"name": new_proxy},
                    headers=self.headers,
                    timeout=10,
                )

                if resp.status_code == 204:
                    try:
                        logger.info(f"Successfully switched to proxy: {new_proxy}")
                    except UnicodeEncodeError:
                        safe_name = new_proxy.encode("ascii", "ignore").decode("ascii")
                        logger.info(
                            f"Successfully switched to proxy: {safe_name or 'proxy-node'}"
                        )
                    time.sleep(2)

                    if self.test_connection():
                        return True
                    else:
                        logger.warning(
                            f"Proxy {new_proxy} switched but connection test failed, trying another proxy"
                        )
                        continue
                else:
                    logger.error(
                        f"Failed to switch proxy. Status: {resp.status_code}, Response: {resp.text}"
                    )
                    continue

            except Exception as e:
                logger.error(f"Exception during proxy switch attempt {attempts}: {e}")
                continue

        logger.error(f"Failed to find a working proxy after {attempts} attempts")
        return False

    def get_current_proxy(self) -> Optional[str]:
        """获取当前选中的代理节点

        Returns:
            str: 当前代理节点名称，失败返回 None

        Examples:
            >>> manager = ClashProxyManager()
            >>> current = manager.get_current_proxy()
            >>> print(f"Current proxy: {current}")
        """
        try:
            resp = requests.get(
                f"{self.api_url}/proxies/{self.encoded_group_name}",
                headers=self.headers,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("now")
        except Exception as e:
            logger.error(f"Failed to get current proxy: {e}")
            return None

    def get_proxy_config(self) -> Dict[str, str]:
        """获取代理配置，用于 requests 库

        Returns:
            Dict[str, str]: 代理配置字典，格式 {"http": "...", "https": "..."}

        Examples:
            >>> manager = ClashProxyManager()
            >>> proxies = manager.get_proxy_config()
            >>> response = requests.get("https://example.com", proxies=proxies)
        """
        return self.proxy_config.copy()

    def test_connection(self, test_url: str = "https://httpbin.org/ip") -> bool:
        """测试代理连接

        Args:
            test_url (str): 测试URL，默认使用 httpbin.org

        Returns:
            bool: 连接是否成功

        Examples:
            >>> manager = ClashProxyManager()
            >>> if manager.test_connection():
            >>>     print("Proxy is working")
        """
        try:
            resp = requests.get(test_url, proxies=self.proxy_config, timeout=10)
            resp.raise_for_status()
            logger.info(
                f"Proxy test successful. IP: {resp.json().get('origin', 'unknown')}"
            )
            return True
        except Exception as e:
            logger.error(f"Proxy test failed: {e}")
            return False


class HttpProxyManager:
    """HTTP代理管理器，用于管理HTTP隧道代理的配置"""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tunnel: Optional[str] = None,
    ):
        """初始化HTTP代理管理器

        Args:
            username (str, optional): 代理用户名，不指定则从环境变量 PROXY_USERNAME 加载
            password (str, optional): 代理密码，不指定则从环境变量 PROXY_PASSWORD 加载
            tunnel (str, optional): 代理隧道地址 (host:port格式)，不指定则从环境变量 PROXY_TUNNEL 加载

        Examples:
            >>> # 使用环境变量
            >>> manager = HttpProxyManager()
            >>>
            >>> # 指定参数
            >>> manager = HttpProxyManager(
            >>>     username="user",
            >>>     password="pass",
            >>>     tunnel="proxy.example.com:8080"
            >>> )
        """
        self.username = username or os.getenv("PROXY_USERNAME")
        self.password = password or os.getenv("PROXY_PASSWORD")
        self.tunnel = tunnel or os.getenv("PROXY_TUNNEL")

        self.proxy_config = self._build_proxy_config()

    def _build_proxy_config(self) -> Dict[str, str]:
        """构建代理配置字典

        Returns:
            Dict[str, str]: 代理配置字典，格式与requests库兼容
        """
        if not all([self.username, self.password, self.tunnel]):
            logger.warning(
                "HTTP代理配置不完整，请确保设置了PROXY_USERNAME、PROXY_PASSWORD和PROXY_TUNNEL环境变量"
            )
            return {}

        try:
            proxy_url = f"http://{self.username}:{self.password}@{self.tunnel}/"
            return {"http": proxy_url, "https": proxy_url}
        except Exception as e:
            logger.error(f"构建代理配置失败: {e}")
            return {}

    def get_proxy_config(self) -> Dict[str, str]:
        """获取代理配置，用于 requests 库

        Returns:
            Dict[str, str]: 代理配置字典

        Examples:
            >>> manager = HttpProxyManager()
            >>> proxies = manager.get_proxy_config()
            >>> response = requests.get("https://example.com", proxies=proxies)
        """
        return self.proxy_config.copy()

    def test_connection(self, test_url: str = "https://httpbin.org/ip") -> bool:
        """测试代理连接

        Args:
            test_url (str): 测试URL

        Returns:
            bool: 连接是否成功

        Examples:
            >>> manager = HttpProxyManager()
            >>> if manager.test_connection():
            >>>     print("Proxy is working")
        """
        if not self.proxy_config:
            logger.error("代理配置为空，无法测试连接")
            return False

        try:
            resp = requests.get(test_url, proxies=self.proxy_config, timeout=10)
            resp.raise_for_status()
            logger.info(f"HTTP代理测试成功. IP: {resp.json().get('origin', 'unknown')}")
            return True
        except Exception as e:
            logger.error(f"HTTP代理测试失败: {e}")
            return False

    def is_configured(self) -> bool:
        """检查代理是否已正确配置

        Returns:
            bool: 配置是否完整

        Examples:
            >>> manager = HttpProxyManager()
            >>> if not manager.is_configured():
            >>>     print("Please configure proxy settings")
        """
        return bool(
            self.proxy_config and self.username and self.password and self.tunnel
        )


def create_clash_proxy_manager(
    api_url: Optional[str] = None,
    secret: Optional[str] = None,
    group_name: Optional[str] = None,
    proxy_port: Optional[int] = None,
) -> ClashProxyManager:
    """创建Clash代理管理器实例（便捷函数）

    Args:
        api_url (str, optional): Clash API地址
        secret (str, optional): Clash API密钥
        group_name (str, optional): 代理组名称
        proxy_port (int, optional): 代理端口

    Returns:
        ClashProxyManager: ClashProxyManager实例

    Examples:
        >>> manager = create_clash_proxy_manager()
        >>> manager.get_proxies()
        >>> manager.switch_proxy()
    """
    return ClashProxyManager(api_url, secret, group_name, proxy_port)


def create_http_proxy_manager(
    username: Optional[str] = None,
    password: Optional[str] = None,
    tunnel: Optional[str] = None,
) -> HttpProxyManager:
    """创建HTTP代理管理器实例（便捷函数）

    Args:
        username (str, optional): 代理用户名
        password (str, optional): 代理密码
        tunnel (str, optional): 代理隧道地址 (host:port格式)

    Returns:
        HttpProxyManager: HttpProxyManager实例

    Examples:
        >>> manager = create_http_proxy_manager()
        >>> proxies = manager.get_proxy_config()
        >>> if manager.test_connection():
        >>>     print("Proxy works!")
    """
    return HttpProxyManager(username, password, tunnel)


def create_proxy_manager(
    proxy_type: Optional[str] = None,
    api_url: Optional[str] = None,
    secret: Optional[str] = None,
    group_name: Optional[str] = None,
    proxy_port: Optional[int] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    tunnel: Optional[str] = None,
) -> Union[ClashProxyManager, HttpProxyManager]:
    """创建代理管理器实例，根据代理类型自动选择合适的代理管理器

    Args:
        proxy_type (str, optional): 代理类型 ('TUNNEL' 或 'CLASH')，不指定则从环境变量 PROXY_TYPE 加载
        api_url (str, optional): Clash API地址 (仅当 proxy_type='CLASH' 时有效)
        secret (str, optional): Clash API密钥 (仅当 proxy_type='CLASH' 时有效)
        group_name (str, optional): Clash代理组名称 (仅当 proxy_type='CLASH' 时有效)
        proxy_port (int, optional): Clash代理端口 (仅当 proxy_type='CLASH' 时有效)
        username (str, optional): HTTP代理用户名 (仅当 proxy_type='TUNNEL' 时有效)
        password (str, optional): HTTP代理密码 (仅当 proxy_type='TUNNEL' 时有效)
        tunnel (str, optional): HTTP代理隧道地址 (仅当 proxy_type='TUNNEL' 时有效)

    Returns:
        Union[ClashProxyManager, HttpProxyManager]: 对应类型的代理管理器实例

    Examples:
        >>> # 从环境变量 PROXY_TYPE 自动选择代理类型
        >>> manager = create_proxy_manager()
        >>>
        >>> # 明确指定代理类型
        >>> manager = create_proxy_manager(proxy_type='TUNNEL')
        >>> manager = create_proxy_manager(proxy_type='CLASH')
    """
    if proxy_type is None:
        proxy_type = os.getenv("PROXY_TYPE", "CLASH")

    proxy_type = proxy_type.upper()

    if proxy_type == "TUNNEL":
        logger.info("使用隧道代理 (HTTP Proxy)")
        return HttpProxyManager(username, password, tunnel)
    elif proxy_type == "CLASH":
        logger.info("使用Clash代理")
        return ClashProxyManager(api_url, secret, group_name, proxy_port)
    else:
        logger.warning(f"未知的代理类型 '{proxy_type}'，默认使用Clash代理")
        return ClashProxyManager(api_url, secret, group_name, proxy_port)
