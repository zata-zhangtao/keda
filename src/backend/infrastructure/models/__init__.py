"""Model loading and client factories.

This package is the canonical location for provider configuration parsing and
LLM client instantiation.
"""

from .model_loader import (
    ModelConfigError,
    _expand_base_urls,
    _find_model_providers,
    _infer_provider,
    _resolve_config_path,
    _resolve_provider_api_key,
    create_chat_model,
    list_models,
    load_models_config,
    resolve_model_credentials,
)

__all__ = [
    "ModelConfigError",
    "_expand_base_urls",
    "_find_model_providers",
    "_infer_provider",
    "_resolve_config_path",
    "_resolve_provider_api_key",
    "create_chat_model",
    "list_models",
    "load_models_config",
    "resolve_model_credentials",
]
