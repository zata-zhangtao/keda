"""Tests for model loader configuration loading.

Tests focus on configuration parsing, credential resolution,
and model metadata without requiring actual API calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.infrastructure.models import (
    _expand_base_urls,
    _find_model_providers,
    _infer_provider,
    _resolve_config_path,
    _resolve_provider_api_key,
    load_models_config,
    list_models,
    resolve_model_credentials,
)


class TestConfigPathResolution:
    """Tests for configuration path resolution."""

    def test_resolve_default_config_path(self) -> None:
        """Test that default config path resolves to existing file."""
        config_path: Path = _resolve_config_path(None)

        assert config_path.exists()
        assert config_path.name == "models.json"

    def test_resolve_custom_config_path(self, tmp_path: Path) -> None:
        """Test resolution of custom config path."""
        custom_config: Path = tmp_path / "custom_models.json"
        custom_config.write_text(json.dumps({"test": {}}), encoding="utf-8")

        resolved_path: Path = _resolve_config_path(custom_config)

        assert resolved_path == custom_config

    def test_resolve_nonexistent_config_raises_error(self, tmp_path: Path) -> None:
        """Test that nonexistent config path raises FileNotFoundError."""
        nonexistent_path: Path = tmp_path / "nonexistent.json"

        with pytest.raises(FileNotFoundError):
            _resolve_config_path(nonexistent_path)


class TestModelsConfigLoading:
    """Tests for models configuration loading."""

    def test_load_default_models_config(self) -> None:
        """Test loading default models.json configuration."""
        config: dict[str, Any] = load_models_config()

        assert isinstance(config, dict)
        assert "openai" in config

    def test_load_models_config_cached(self) -> None:
        """Test that models config is cached."""
        config1: dict[str, Any] = load_models_config()
        config2: dict[str, Any] = load_models_config()

        assert config1 is config2

    def test_load_custom_models_config(self, tmp_path: Path) -> None:
        """Test loading custom models configuration."""
        custom_config: dict[str, Any] = {
            "test_provider": {
                "api_key_env": "TEST_API_KEY",
                "base_url": "https://test.example.com/v1",
                "chat_models": [{"name": "test-model", "description": "Test model"}],
            }
        }
        config_file: Path = tmp_path / "test_models.json"
        config_file.write_text(json.dumps(custom_config), encoding="utf-8")

        loaded_config: dict[str, Any] = load_models_config(config_file)

        assert "test_provider" in loaded_config
        assert loaded_config["test_provider"]["api_key_env"] == "TEST_API_KEY"


class TestProviderInference:
    """Tests for provider inference from model names."""

    def test_infer_anthropic_from_claude(self) -> None:
        """Test inferring anthropic provider from claude model name."""
        assert _infer_provider("claude-3") == "anthropic"
        assert _infer_provider("claude-3.5-sonnet") == "anthropic"

    def test_infer_openai_by_default(self) -> None:
        """Test defaulting to openai for unknown models."""
        assert _infer_provider("gpt-4") == "openai"
        assert _infer_provider("unknown-model") == "openai"


class TestListModels:
    """Tests for listing available models."""

    def test_list_openai_chat_models(self) -> None:
        """Test listing openai chat models."""
        models: list[dict[str, Any]] = list_models("openai", "chat_models")

        assert isinstance(models, list)
        assert len(models) > 0
        assert any(m["name"] == "gpt-4" for m in models)

    def test_list_openai_all_models(self) -> None:
        """Test listing all openai models without category filter."""
        models: list[dict[str, Any]] = list_models("openai")

        assert isinstance(models, list)
        assert len(models) > 0

    def test_list_unknown_provider_returns_empty(self) -> None:
        """Test that unknown provider returns empty list."""
        models: list[dict[str, Any]] = list_models("nonexistent_provider")

        assert models == []


class TestFindModelProviders:
    """Tests for finding providers for a specific model."""

    def test_find_gpt4_providers(self) -> None:
        """Test finding providers for gpt-4 model."""
        config: dict[str, Any] = load_models_config()
        providers: list[tuple[str, Any]] = _find_model_providers("gpt-4", config)

        assert len(providers) > 0
        assert any(p[0] == "openai" for p in providers)

    def test_find_unknown_model_infers_provider(self) -> None:
        """Test that unknown model infers provider."""
        config: dict[str, Any] = load_models_config()
        providers: list[tuple[str, Any]] = _find_model_providers("unknown-model", config)

        assert len(providers) == 1
        assert providers[0][0] == "openai"


class TestExpandBaseUrls:
    """Tests for base URL expansion."""

    def test_expand_string_base_url(self) -> None:
        """Test expanding string base_url."""
        config: dict[str, Any] = {"base_url": "https://api.example.com/v1"}
        urls: list[str | None] = _expand_base_urls("test", config)

        assert urls == ["https://api.example.com/v1"]

    def test_expand_list_base_url(self) -> None:
        """Test expanding list base_url."""
        config: dict[str, Any] = {
            "base_url": ["https://api1.example.com", "https://api2.example.com"]
        }
        urls: list[str | None] = _expand_base_urls("test", config)

        assert "https://api1.example.com" in urls
        assert "https://api2.example.com" in urls

    def test_expand_dict_base_url_with_preferred_key(self) -> None:
        """Test expanding dict base_url with preferred key."""
        config: dict[str, Any] = {
            "base_url": {
                "us": "https://us.example.com",
                "eu": "https://eu.example.com",
            }
        }
        urls: list[str | None] = _expand_base_urls("test", config, preferred_key="eu")

        assert urls == ["https://eu.example.com"]

    def test_expand_empty_base_url_returns_none(self) -> None:
        """Test that empty base_url returns [None]."""
        config: dict[str, Any] = {}
        urls: list[str | None] = _expand_base_urls("test", config)

        assert urls == [None]


class TestResolveProviderApiKey:
    """Tests for API key resolution."""

    def test_resolve_explicit_api_key(self) -> None:
        """Test that explicit API key takes priority."""
        config: dict[str, Any] = {"api_key_env": "TEST_ENV_KEY"}
        resolved_key: str | None = _resolve_provider_api_key("test", config, "explicit-api-key")

        assert resolved_key == "explicit-api-key"

    def test_resolve_from_config_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test resolving API key from config-specified env var."""
        monkeypatch.setenv("CUSTOM_API_KEY", "custom-key-value")
        config: dict[str, Any] = {"api_key_env": "CUSTOM_API_KEY"}

        resolved_key: str | None = _resolve_provider_api_key("test", config, None)

        assert resolved_key == "custom-key-value"

    def test_resolve_from_default_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test resolving API key from default env var."""
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key-value")
        config: dict[str, Any] = {}

        resolved_key: str | None = _resolve_provider_api_key("openai", config, None)

        assert resolved_key == "openai-key-value"

    def test_resolve_no_api_key_returns_none(self) -> None:
        """Test that missing API key returns None."""
        config: dict[str, Any] = {}

        resolved_key: str | None = _resolve_provider_api_key("unknown", config, None)

        assert resolved_key is None


class TestResolveModelCredentials:
    """Tests for model credential resolution."""

    def test_resolve_gpt4_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test resolving credentials for gpt-4."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

        credentials: list[tuple[str, str | None, str | None]] = resolve_model_credentials("gpt-4")

        assert len(credentials) > 0
        provider, base_url, api_key = credentials[0]
        assert provider == "openai"
        assert api_key == "test-openai-key"
        assert base_url is not None

    def test_resolve_with_explicit_api_key(self) -> None:
        """Test resolving with explicit API key override."""
        credentials: list[tuple[str, str | None, str | None]] = resolve_model_credentials(
            "gpt-4", api_key="my-explicit-key"
        )

        assert len(credentials) > 0
        assert any(c[2] == "my-explicit-key" for c in credentials)

    def test_resolve_deduplicates_credentials(self) -> None:
        """Test that duplicate credentials are deduplicated."""
        credentials: list[tuple[str, str | None, str | None]] = resolve_model_credentials("gpt-4")

        # Convert to set to check for duplicates
        unique_credentials: set[tuple[str | None, str | None, str | None]] = set(
            (c[0], c[1], c[2]) for c in credentials
        )
        assert len(unique_credentials) == len(credentials)


class TestConfigurationIntegration:
    """Integration tests for configuration loading."""

    def test_config_matches_settings_integration(self) -> None:
        """Test that model loader config aligns with settings module.

        This test verifies the integration between the infrastructure model loader
        and the centralized settings configuration.
        """
        from backend.infrastructure.config.settings import config as app_config

        # Load model configuration
        models_config: dict[str, Any] = load_models_config()

        # Verify that configured providers exist in models.json
        if app_config.chat_model.provider == "openai":
            assert "openai" in models_config
            openai_models: list[dict[str, Any]] = list_models("openai")
            model_names: list[str] = [m["name"] for m in openai_models]
            assert app_config.chat_model.name in model_names

    def test_embedding_config_has_model(self) -> None:
        """Test that embedding model configuration is valid."""
        from backend.infrastructure.config.settings import config as app_config

        # Embedding model should be a valid sentence-transformers model
        # or other supported format
        assert app_config.embedding.model
        assert app_config.embedding.dim > 0
        assert app_config.embedding.model_dir
