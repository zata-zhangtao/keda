"""Model-loading helpers for provider-configured chat models."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from dotenv import load_dotenv
from langchain_core.language_models import BaseLanguageModel

# Load repository-level environment variables when present.
PROJECT_ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT_DIR / ".env", override=False)


DEFAULT_MODELS_CONFIG = Path(__file__).resolve().parent / "models.json"

DEFAULT_API_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


class ModelConfigError(RuntimeError):
    """当模型配置无法满足请求时抛出的异常。

    当尝试创建模型但配置不完整或无效时会引发此异常。
    """


def _resolve_config_path(config_path: str | Path | None) -> Path:
    """返回应从中加载提供商配置的路径。

    Args:
        config_path (str | Path | None): 配置文件的可选路径。

    Returns:
        Path: 配置文件的解析路径。

    Raises:
        FileNotFoundError: 如果配置文件不存在。

    Examples:
        >>> from pathlib import Path
        >>> path = _resolve_config_path(None)
        >>> path.exists()
        True

        >>> path = _resolve_config_path("custom_config.json")
        >>> # 如果文件存在，则返回该路径
    """

    path = Path(config_path) if config_path else DEFAULT_MODELS_CONFIG
    if not path.exists():
        raise FileNotFoundError(f"Model config file not found: {path}")
    return path


@lru_cache(maxsize=4)
def load_models_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """加载并缓存提供商配置文件。

    Args:
        config_path (str | Path | None): 配置文件的可选路径覆盖。

    Returns:
        dict[str, Any]: 解析后的提供商配置数据。

    Raises:
        FileNotFoundError: 如果无法找到配置文件。
        json.JSONDecodeError: 如果文件包含无效的 JSON。

    Examples:
        >>> config = load_models_config()
        >>> isinstance(config, dict)
        True
        >>> "openai" in config
        True

        >>> config = load_models_config("custom_models.json")
        >>> # 从自定义路径加载配置
    """

    path = _resolve_config_path(config_path)
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def list_models(
    provider: str,
    category: str | None = None,
    *,
    config_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """返回给定提供商/类别的已配置模型。

    Args:
        provider (str): 配置文件中定义的提供商键。
        category (str | None): 可选的类别键，例如 ``omni_models``。
        config_path (str | Path | None): 配置文件路径的覆盖。

    Returns:
        list[dict[str, Any]]: 包含元数据的匹配模型条目。

    Examples:
        >>> models = list_models("openai")
        >>> isinstance(models, list)
        True
        >>> len(models) > 0
        True

        >>> chat_models = list_models("openai", "chat_models")
        >>> # 获取 OpenAI 的聊天模型列表
    """

    config = load_models_config(config_path)
    provider_data = config.get(provider.lower())
    if provider_data is None:
        return []
    if not category:
        models: list[dict[str, Any]] = []
        for value in provider_data.values():
            if isinstance(value, list):
                models.extend(value)
        return models
    return provider_data.get(category, [])


def _infer_provider(model_name: str) -> str:
    """从模型名称推测上游提供商。

    模板只保留最基础的启发式规则。
    项目应在 models.json 中显式配置模型，而非依赖推断。

    Args:
        model_name (str): 模型的名称标识符。

    Returns:
        str: 推测的提供商名称。

    Examples:
        >>> _infer_provider("gpt-4")
        'openai'
        >>> _infer_provider("claude-3")
        'anthropic'
    """

    lowered = model_name.lower()
    if "claude" in lowered:
        return "anthropic"
    return "openai"


def _find_model_providers(
    model_name: str,
    config: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any] | None]]:
    """查找可提供指定模型的全部提供商。

    Args:
        model_name (str): 模型的名称标识符。
        config (Mapping[str, Any]): 解析后的提供商配置。

    Returns:
        list[tuple[str, Mapping[str, Any] | None]]: 每个提供商及其配置。
    """

    normalized = model_name.strip().lower()
    matches: list[tuple[str, Mapping[str, Any] | None]] = []
    for provider_name, provider_config in config.items():
        for value in provider_config.values():
            if isinstance(value, list):
                for entry in value:
                    if entry.get("name", "").strip().lower() == normalized:
                        matches.append((provider_name, provider_config))
                        break
                else:
                    continue
                break
    if matches:
        return matches

    inferred = _infer_provider(model_name)
    matches.append((inferred, config.get(inferred)))
    return matches


def _expand_base_urls(
    provider: str,
    provider_config: Mapping[str, Any],
    *,
    preferred_key: str | None = None,
) -> list[str | None]:
    """生成提供商配置中声明的基础 URL 列表。

    Args:
        provider (str): 提供商名称。
        provider_config (Mapping[str, Any]): 提供商特定配置。
        preferred_key (str | None): 可选的首选 base_url 键（用于区域）。

    Returns:
        list[str | None]: 候选基础 URL，若未配置则返回 ``[None]``。
    """

    base_value = provider_config.get("base_url")
    urls: list[str] = []

    if isinstance(base_value, str):
        urls = [base_value]
    elif isinstance(base_value, list):
        urls = [value for value in base_value if isinstance(value, str)]
    elif isinstance(base_value, Mapping):
        if preferred_key and preferred_key in base_value:
            target = base_value.get(preferred_key)
            if isinstance(target, str):
                return [target]
        for value in base_value.values():
            if isinstance(value, str) and value not in urls:
                urls.append(value)

    if not urls:
        return [None]
    return urls


def _resolve_provider_api_key(
    provider: str,
    provider_config: Mapping[str, Any],
    explicit_api_key: str | None,
) -> str | None:
    """解析提供商可用的 API 密钥。

    Args:
        provider (str): 提供商名称。
        provider_config (Mapping[str, Any]): 提供商特定配置。
        explicit_api_key (str | None): 用户显式传入的 API 密钥。

    Returns:
        str | None: 解析后的 API 密钥（若无法解析则为 ``None``）。
    """

    if explicit_api_key:
        return explicit_api_key

    env_name = provider_config.get("api_key_env")
    if env_name:
        env_value = os.getenv(env_name)
        if env_value:
            return env_value

    default_env = DEFAULT_API_KEY_ENVS.get(provider)
    if default_env:
        env_value = os.getenv(default_env)
        if env_value:
            return env_value

    return None


def _collect_provider_credentials(
    provider: str,
    provider_config: Mapping[str, Any] | None,
    *,
    explicit_api_key: str | None,
    dashscope_region: str | None,
) -> list[tuple[str, str | None, str | None]]:
    """生成指定提供商的全部凭据信息组合。

    Args:
        provider (str): 提供商名称。
        provider_config (Mapping[str, Any] | None): 提供商配置。
        explicit_api_key (str | None): 显式 API 密钥。
        dashscope_region (str | None): DashScope 区域选择器。

    Returns:
        list[tuple[str, str | None, str | None]]: (provider, base_url, api_key) 列表。
    """

    normalized_config = provider_config or {}
    base_urls = _expand_base_urls(
        provider,
        normalized_config,
        preferred_key=dashscope_region if provider == "dashscope" else None,
    )
    resolved_api_key = _resolve_provider_api_key(provider, normalized_config, explicit_api_key)
    return [(provider, base_url, resolved_api_key) for base_url in base_urls]


def resolve_model_credentials(
    model_name: str,
    *,
    config_path: str | Path | None = None,
    api_key: str | None = None,
    dashscope_region: str | None = None,
) -> list[tuple[str, str | None, str | None]]:
    """解析模型可用的全部提供商、基础 URL 和 API 密钥组合。

    Args:
        model_name (str): 要检查的模型标识符。
        config_path (str | Path | None): 可选的配置文件覆盖。
        api_key (str | None): 优先使用的显式 API 密钥。
        dashscope_region (str | None): DashScope 基础 URL 的区域选择器。

    Returns:
        list[tuple[str, str | None, str | None]]: (provider, base_url, api_key)
        组合，覆盖所有匹配的提供商和 URL。

    Examples:
        >>> resolve_model_credentials("gpt-4")
        [('openai', None, 'sk-...')]

        >>> resolve_model_credentials("qwen-turbo", dashscope_region="beijing")
        [('dashscope', 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'ak-...')]
    """

    config = load_models_config(config_path)
    providers = _find_model_providers(model_name, config)
    credentials: list[tuple[str, str | None, str | None]] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()

    for provider, provider_config in providers:
        combos = _collect_provider_credentials(
            provider,
            provider_config,
            explicit_api_key=api_key,
            dashscope_region=dashscope_region,
        )
        for combo in combos:
            dedupe_key = (combo[0], combo[1], combo[2])
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                credentials.append(combo)

    return credentials


def create_chat_model(
    model_name: str,
    *,
    temperature: float = 0.0,
    config_path: str | Path | None = None,
    api_key: str | None = None,
    dashscope_region: str | None = None,
    client_kwargs: Mapping[str, Any] | None = None,
) -> BaseLanguageModel:
    """根据提供商配置实例化聊天模型。

    Args:
        model_name (str): 要实例化的模型名称。
        temperature (float): 传递给底层客户端的温度值。
        config_path (str | Path | None): 可选的配置路径覆盖。
        api_key (str | None): 显式传入的 API 密钥。
        dashscope_region (str | None): DashScope 基础 URL 的区域选择器。
        client_kwargs (Mapping[str, Any] | None): 额外传递的客户端参数。

    Returns:
        BaseLanguageModel: 可直接调用的 LLM 客户端实例。

    Raises:
        ModelConfigError: 当无法解析 API 密钥或配置缺失时抛出。
        FileNotFoundError: 当配置路径不存在时抛出。
    """

    llm_kwargs: MutableMapping[str, Any] = {
        "model": model_name,
        "temperature": temperature,
    }
    if client_kwargs:
        llm_kwargs.update(client_kwargs)

    credential_options = resolve_model_credentials(
        model_name,
        config_path=config_path,
        api_key=api_key,
        dashscope_region=dashscope_region,
    )
    if not credential_options:
        raise ModelConfigError(f"No credentials configured for model '{model_name}'.")

    selected_provider, selected_base_url, resolved_api_key = credential_options[0]
    for provider, base_url, candidate_key in credential_options:
        if candidate_key:
            selected_provider = provider
            selected_base_url = base_url
            resolved_api_key = candidate_key
            break

    if selected_base_url:
        llm_kwargs.setdefault("base_url", selected_base_url)

    from langchain_anthropic import ChatAnthropic
    from langchain_openai import ChatOpenAI

    if selected_provider == "anthropic":
        llm_class = ChatAnthropic
    else:
        llm_class = ChatOpenAI

    if resolved_api_key:
        llm_kwargs.setdefault("api_key", resolved_api_key)
    if "api_key" not in llm_kwargs:
        raise ModelConfigError(
            f"API key missing for provider '{selected_provider}' and model '{model_name}'."
        )
    return llm_class(**llm_kwargs)
