"""配置文件 - 使用 pydantic-settings 集中管理所有配置。

支持三层配置源（优先级从高到低）：
1. 环境变量 / .env / .env.local
2. config.toml（非敏感配置）
3. 代码中的默认值
"""

import os
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

_SETTINGS_FILE_PATH: Path = Path(__file__).resolve()
_CONFIG_DIR_PATH: Path = _SETTINGS_FILE_PATH.parent
_INFRASTRUCTURE_DIR_PATH: Path = _CONFIG_DIR_PATH.parent
_BACKEND_DIR_PATH: Path = _INFRASTRUCTURE_DIR_PATH.parent
_SOURCE_DIR_PATH: Path = _BACKEND_DIR_PATH.parent
_PROJECT_ROOT_PATH: Path = _SOURCE_DIR_PATH.parent
_TOML_CONFIG_FILE_PATH: Path = _PROJECT_ROOT_PATH / "config.toml"


def _load_toml_section_data(section_name: str) -> dict[str, Any]:
    """从 config.toml 加载指定 section 的配置。

    Args:
        section_name: TOML section 名称。

    Returns:
        section 内容字典，文件不存在或 section 不存在时返回空 dict。
    """
    if not _TOML_CONFIG_FILE_PATH.is_file():
        return {}
    try:
        with open(_TOML_CONFIG_FILE_PATH, "rb") as toml_file:
            toml_data: dict[str, Any] = tomllib.load(toml_file)
        return toml_data.get(section_name, {})
    except Exception:
        return {}


class _TomlSectionSource(PydanticBaseSettingsSource):
    """从 config.toml 指定 section 读取配置的自定义源。"""

    def __init__(self, settings_cls: type[BaseSettings], section_name: str) -> None:
        super().__init__(settings_cls)
        self._section_data: dict[str, Any] = _load_toml_section_data(section_name)

    def get_field_value(
        self,
        field: Any,  # noqa: ARG002
        field_name: str,
    ) -> tuple[Any, str, bool]:
        field_value: Any = self._section_data.get(field_name)
        return field_value, field_name, False

    def __call__(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field_name in self.settings_cls.model_fields:
            field_value: Any = self._section_data.get(field_name)
            if field_value is not None:
                result[field_name] = field_value
        return result


class DatabaseSettings(BaseSettings):
    """数据库连接配置（非敏感部分）。"""

    model_config = SettingsConfigDict(env_prefix="DB_")

    backend: str = "postgresql"
    host: str = "localhost"
    port: int = 5432
    name: str = "app_database"
    driver: str = "psycopg2"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_source: _TomlSectionSource = _TomlSectionSource(settings_cls, "database")
        return (
            env_settings,
            toml_source,
            init_settings,
        )


class ChatModelSettings(BaseSettings):
    """默认聊天模型配置。"""

    model_config = SettingsConfigDict(env_prefix="CHAT_MODEL_")

    name: str = "gpt-4"
    provider: str = "openai"
    temperature: float = 0.2

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_source: _TomlSectionSource = _TomlSectionSource(settings_cls, "chat_model")
        return (
            env_settings,
            toml_source,
            init_settings,
        )


class MinioSettings(BaseSettings):
    """MinIO 对象存储配置（非敏感部分）。"""

    model_config = SettingsConfigDict(env_prefix="MINIO_")

    endpoint: str = "localhost:9000"
    secure: bool = False
    bucket_raw_documents: str = "default-bucket"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_source: _TomlSectionSource = _TomlSectionSource(settings_cls, "minio")
        return (
            env_settings,
            toml_source,
            init_settings,
        )


class QdrantSettings(BaseSettings):
    """Qdrant 向量数据库配置。"""

    model_config = SettingsConfigDict(env_prefix="QDRANT_")

    host: str = "localhost"
    port: int = 6333
    collection_name: str = "default_collection"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_source: _TomlSectionSource = _TomlSectionSource(settings_cls, "qdrant")
        return (
            env_settings,
            toml_source,
            init_settings,
        )


class EmbeddingSettings(BaseSettings):
    """Embedding 模型配置。"""

    model_config = SettingsConfigDict(env_prefix="EMBEDDING_")

    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    dim: int = 384
    offline_mode: bool = True
    model_dir: str = "resources/models"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_source: _TomlSectionSource = _TomlSectionSource(settings_cls, "embedding")
        return (
            env_settings,
            toml_source,
            init_settings,
        )


class ChunkingSettings(BaseSettings):
    """文档分块配置。"""

    model_config = SettingsConfigDict(env_prefix="CHUNK_")

    size: int = 512
    overlap: int = 50

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_source: _TomlSectionSource = _TomlSectionSource(settings_cls, "chunking")
        return (
            env_settings,
            toml_source,
            init_settings,
        )


class TimeoutSettings(BaseSettings):
    """超时配置（秒）。"""

    model_config = SettingsConfigDict(env_prefix="TIMEOUT_")

    embedding_model_load_seconds: int = 300
    ingestion_document_seconds: int = 600
    ingestion_job_seconds: int = 7200
    minio_seconds: int = 60

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_source: _TomlSectionSource = _TomlSectionSource(settings_cls, "timeouts")
        return (
            env_settings,
            toml_source,
            init_settings,
        )


class AgentRunnerLabelSettings(BaseModel):
    """GitHub labels used as runner queue state."""

    ready: str = "agent/ready"
    running: str = "agent/running"
    review: str = "agent/review"
    failed: str = "agent/failed"
    blocked: str = "agent/blocked"
    codex: str = "agent/codex"
    claude: str = "agent/claude"
    kimi: str = "agent/kimi"

    @property
    def agent_labels(self) -> dict[str, str]:
        """Agent routing labels as a lookup table."""
        return {
            "codex": self.codex,
            "claude": self.claude,
            "kimi": self.kimi,
        }


class AgentRunnerGitSettings(BaseModel):
    """Git publishing configuration."""

    remote: str = "origin"
    base_branch: str = "main"


class AgentRunnerWorktreeSettings(BaseModel):
    """Commands used to create and locate target worktrees."""

    create_command: str = "just worktree issue-{issue_number} enter_shell=false"
    reuse_command: str = 'bash -c \'test -d "$(dirname "$(git rev-parse --show-toplevel)")/issue-{issue_number}"\''
    path_command: str = 'bash -c \'echo "$(dirname "$(git rev-parse --show-toplevel)")/issue-{issue_number}"\''


class AgentRunnerRunnerSettings(BaseModel):
    """Local runner behavior."""

    max_issues: int = 1
    default_agent: str = "auto"
    max_recovery_attempts: int = 2
    verification_commands: list[str] = Field(
        default_factory=lambda: [
            "git diff --check",
            "uv run mkdocs build",
        ]
    )


class AgentRunnerSafetySettings(BaseModel):
    """Safety boundaries enforced before publishing."""

    auto_merge: bool = False
    forbidden_path_patterns: list[str] = Field(
        default_factory=lambda: [
            ".env",
            ".env.*",
            "secrets/*",
            "docker-compose.prod.yml",
        ]
    )


class AgentRunnerSettings(BaseSettings):
    """Agent Runner configuration."""

    model_config = SettingsConfigDict(env_prefix="AGENT_RUNNER_")

    max_issues: int = 1
    default_agent: str = "auto"
    labels: AgentRunnerLabelSettings = Field(default_factory=AgentRunnerLabelSettings)
    git: AgentRunnerGitSettings = Field(default_factory=AgentRunnerGitSettings)
    worktree: AgentRunnerWorktreeSettings = Field(
        default_factory=AgentRunnerWorktreeSettings
    )
    runner: AgentRunnerRunnerSettings = Field(default_factory=AgentRunnerRunnerSettings)
    safety: AgentRunnerSafetySettings = Field(default_factory=AgentRunnerSafetySettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_source: _TomlSectionSource = _TomlSectionSource(
            settings_cls, "agent_runner"
        )
        return (
            env_settings,
            toml_source,
            init_settings,
        )


class AppSettings(BaseSettings):
    """应用主配置 - 聚合所有子配置。"""

    model_config = SettingsConfigDict(
        env_file=(_PROJECT_ROOT_PATH / ".env", _PROJECT_ROOT_PATH / ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="app", validation_alias="NAME")
    log_level: str = Field(default="INFO")

    postgres_user: str = ""
    postgres_password: SecretStr = SecretStr("")
    database_url: str = ""
    minio_access_key: str = Field(default="minioadmin")
    minio_secret_key: SecretStr = SecretStr("minioadmin")
    minio_root_user: str = ""
    minio_root_password: SecretStr = SecretStr("")

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    chat_model: ChatModelSettings = Field(default_factory=ChatModelSettings)
    minio: MinioSettings = Field(default_factory=MinioSettings)
    qdrant: QdrantSettings = Field(default_factory=QdrantSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    timeouts: TimeoutSettings = Field(default_factory=TimeoutSettings)
    agent_runner: AgentRunnerSettings = Field(default_factory=AgentRunnerSettings)

    base_dir: Path = _PROJECT_ROOT_PATH
    log_dir: Path = Field(default_factory=lambda: _PROJECT_ROOT_PATH / "logs")
    log_file: Path = Field(
        default_factory=lambda: _PROJECT_ROOT_PATH / "logs" / "app.log"
    )

    @property
    def resolved_database_url(self) -> str:
        """解析最终 DATABASE_URL：env var > TOML + credentials > default。"""
        if self.database_url and self.database_url.strip():
            return self.database_url.strip()

        db_config: DatabaseSettings = self.database
        encoded_user: str = quote_plus(self.postgres_user) if self.postgres_user else ""
        raw_password: str = self.postgres_password.get_secret_value()
        encoded_password: str = quote_plus(raw_password) if raw_password else ""

        credentials_part: str = ""
        if encoded_user or encoded_password:
            credentials_part = f"{encoded_user}:{encoded_password}"

        netloc: str = (
            f"{credentials_part}@{db_config.host}"
            if credentials_part
            else db_config.host
        )

        resolved_url: str = f"{db_config.backend}+{db_config.driver}://{netloc}:{db_config.port}/{db_config.name}"
        return resolved_url

    @property
    def resolved_minio_access_key(self) -> str:
        """解析 MinIO access key。"""
        if self.minio_access_key != "minioadmin":
            return self.minio_access_key
        return self.minio_root_user or "minioadmin"

    @property
    def resolved_minio_secret_key(self) -> str:
        """解析 MinIO secret key。"""
        secret_value: str = self.minio_secret_key.get_secret_value()
        if secret_value != "minioadmin":
            return secret_value
        root_password: str = self.minio_root_password.get_secret_value()
        return root_password or "minioadmin"

    def ensure_log_directory(self) -> None:
        """确保日志目录存在。"""
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_source: _TomlSectionSource = _TomlSectionSource(settings_cls, "app")
        return (
            env_settings,
            dotenv_settings,
            toml_source,
            init_settings,
        )


def _ensure_no_proxy_for_local_services() -> None:
    """确保本地服务（localhost/127.0.0.1）不经过系统 HTTP 代理。"""
    existing_no_proxy: str = os.getenv("NO_PROXY", "")
    local_hosts: set[str] = {"localhost", "127.0.0.1", "::1"}
    current_entries: set[str] = {
        entry.strip() for entry in existing_no_proxy.split(",") if entry.strip()
    }
    missing_entries: set[str] = local_hosts - current_entries

    if missing_entries:
        updated_no_proxy: str = ",".join(current_entries | local_hosts)
        os.environ["NO_PROXY"] = updated_no_proxy
        os.environ["no_proxy"] = updated_no_proxy


config: AppSettings = AppSettings()
config.ensure_log_directory()
_ensure_no_proxy_for_local_services()

__all__ = [
    "AgentRunnerSettings",
    "AppSettings",
    "ChatModelSettings",
    "ChunkingSettings",
    "DatabaseSettings",
    "EmbeddingSettings",
    "MinioSettings",
    "QdrantSettings",
    "TimeoutSettings",
    "config",
]
