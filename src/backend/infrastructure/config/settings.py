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

from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator
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
IAR_REPOSITORY_CONFIG_FILENAME = ".iar.toml"


def _find_config_toml() -> Path | None:
    """从当前工作目录向上查找 config.toml，回退到源码根目录。"""
    cwd = Path.cwd()
    for path in [cwd, *cwd.parents]:
        candidate = path / "config.toml"
        if candidate.is_file():
            return candidate
    fallback = _PROJECT_ROOT_PATH / "config.toml"
    if fallback.is_file():
        return fallback
    return None


def _load_toml_section_data(section_name: str) -> dict[str, Any]:
    """从 config.toml 加载指定 section 的配置。

    Args:
        section_name: TOML section 名称。

    Returns:
        section 内容字典，文件不存在或 section 不存在时返回空 dict。
    """
    toml_path = _find_config_toml()
    if toml_path is None:
        return {}
    try:
        with open(toml_path, "rb") as toml_file:
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


def _env_toml_init_sources(
    settings_cls: type[BaseSettings],
    section_name: str,
    env_settings: PydanticBaseSettingsSource,
    init_settings: PydanticBaseSettingsSource,
) -> tuple[PydanticBaseSettingsSource, ...]:
    """Build the standard env > TOML > init settings source order."""
    toml_source: _TomlSectionSource = _TomlSectionSource(settings_cls, section_name)
    return (
        env_settings,
        toml_source,
        init_settings,
    )


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
        return _env_toml_init_sources(
            settings_cls, "database", env_settings, init_settings
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
        return _env_toml_init_sources(
            settings_cls, "chat_model", env_settings, init_settings
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
        return _env_toml_init_sources(
            settings_cls, "minio", env_settings, init_settings
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
        return _env_toml_init_sources(
            settings_cls, "qdrant", env_settings, init_settings
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
        return _env_toml_init_sources(
            settings_cls, "embedding", env_settings, init_settings
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
        return _env_toml_init_sources(
            settings_cls, "chunking", env_settings, init_settings
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
        return _env_toml_init_sources(
            settings_cls, "timeouts", env_settings, init_settings
        )


class AgentRunnerLabelSettings(BaseModel):
    """GitHub labels used as runner queue state."""

    ready: str = "agent/ready"
    running: str = "agent/running"
    supervising: str = "agent/supervising"
    review: str = "agent/review"
    failed: str = "agent/failed"
    blocked: str = "agent/blocked"
    waiting: str = "agent/waiting"
    validation_pending: str = "validation/pending"
    validation_passed: str = "validation/passed"
    group_prefix: str = "task-group/"
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
    """Commands used to create and locate target worktrees.

    Defaults delegate to the built-in ``iar worktree`` subcommand so the
    create / path pair can never drift apart. Override only when the target
    repository genuinely needs a custom worktree layout.
    """

    create_command: str = (
        "iar worktree create --branch issue-{issue_number} "
        "--base-branch {base_branch}"
    )
    reuse_command: str = "iar worktree path --branch issue-{issue_number}"
    path_command: str = "iar worktree path --branch issue-{issue_number}"


class AgentRunnerRunnerSettings(BaseModel):
    """Local runner behavior."""

    max_issues: int = 1
    default_agent: str = "auto"
    max_recovery_attempts: int = 5
    recovery_retry_delay_seconds: int = 30
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


class AgentRunnerValidationSettings(BaseModel):
    """Realistic Validation evidence gate configuration."""

    enabled: bool = True
    evidence_dir: str = ".iar/evidence"
    branch_prefix: str = "iar-evidence/"


class AgentRunnerPromptSettings(BaseModel):
    """Agent prompt template settings supporting TOML string-list syntax."""

    default_phase: str = "execution"
    phases: dict[str, str | list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _join_list_templates(self) -> "AgentRunnerPromptSettings":
        """Convert list[str] phase values to joined strings."""
        for phase_name, phase_value in self.phases.items():
            if isinstance(phase_value, list):
                self.phases[phase_name] = "\n".join(phase_value)
        return self


class AgentRunnerPrePushReviewSettings(BaseModel):
    """Pre-push AI review gate configuration."""

    enabled: bool = True
    review_agent: str = "auto"
    allow_same_agent: bool = True
    max_attempts: int = 2
    timeout_seconds: int = 900


class AgentRunnerPostPrSupervisorSettings(BaseModel):
    """Post-PR supervisor cycle configuration."""

    enabled: bool = True
    supervisor_agent: str = "auto"
    max_repair_attempts: int = 2


class AgentRunnerDeliberationProfileSettings(BaseModel):
    """Participant profile for deliberation sessions."""

    agent: str = "claude"
    role: str = "participant"
    behavior_prompt: str = "Analyze requirements carefully."


class AgentRunnerDeliberationSettings(BaseModel):
    """Multi-agent deliberation configuration."""

    default_rounds: int = 2
    default_synthesizer: str = "claude"
    default_output_dir: str = "logs/agent-runner/deliberations"
    profiles: dict[str, AgentRunnerDeliberationProfileSettings] = Field(
        default_factory=lambda: {
            "architect": AgentRunnerDeliberationProfileSettings(
                agent="claude",
                role="architect",
                behavior_prompt=(
                    "You are an experienced software architect. "
                    "Analyze the requirement from a system design perspective. "
                    "Focus on modularity, scalability, and maintainability."
                ),
            ),
            "skeptic": AgentRunnerDeliberationProfileSettings(
                agent="kimi",
                role="skeptic",
                behavior_prompt=(
                    "You are a skeptical reviewer. "
                    "Challenge assumptions, identify risks, and point out edge cases. "
                    "Ask hard questions that others might miss."
                ),
            ),
            "implementer": AgentRunnerDeliberationProfileSettings(
                agent="codex",
                role="implementer",
                behavior_prompt=(
                    "You are a pragmatic implementer. "
                    "Focus on feasibility, concrete steps, and implementation details. "
                    "Highlight what can be built and what resources are needed."
                ),
            ),
        }
    )


class AgentRunnerGeneratedContentTargetSettings(BaseModel):
    """Generated-content target configuration supporting TOML string-list syntax."""

    enabled: bool = False
    mode: str = "template"
    output: str = "json"
    title_template: str | list[str] = ""
    body_template: str | list[str] = ""
    agent: str = "auto"
    timeout_seconds: int = 60
    prompt: str | list[str] = ""
    include_commit_log: bool = True
    include_diff_stat: bool = True

    @model_validator(mode="after")
    def _join_list_templates(self) -> "AgentRunnerGeneratedContentTargetSettings":
        """Convert list[str] template values to joined strings."""
        for field_name in ("title_template", "body_template", "prompt"):
            value = getattr(self, field_name)
            if isinstance(value, list):
                setattr(self, field_name, "\n".join(value))
        return self


class AgentRunnerGeneratedContentSettings(BaseModel):
    """Generated-content configuration for Issues and PRs."""

    enabled: bool = False
    fallback: str = "template"
    max_input_chars: int = 20000
    default_agent: str = "auto"
    issue_from_prd: AgentRunnerGeneratedContentTargetSettings = Field(
        default_factory=AgentRunnerGeneratedContentTargetSettings
    )
    draft_pr: AgentRunnerGeneratedContentTargetSettings = Field(
        default_factory=AgentRunnerGeneratedContentTargetSettings
    )


class AgentRunnerRepositoryMetadataSettings(BaseModel):
    """Repository identity stored in repository-local IAR config."""

    id: str | None = None
    enabled: bool = True
    display_name: str | None = None


class _AgentRunnerRepositoryOverrideSettings(BaseModel):
    """Optional Agent Runner overrides shared by registry and local config."""

    labels: AgentRunnerLabelSettings | None = None
    git: AgentRunnerGitSettings | None = None
    worktree: AgentRunnerWorktreeSettings | None = None
    runner: AgentRunnerRunnerSettings | None = None
    safety: AgentRunnerSafetySettings | None = None
    validation: AgentRunnerValidationSettings | None = None
    prompts: AgentRunnerPromptSettings | None = None
    pre_push_review: AgentRunnerPrePushReviewSettings | None = None
    post_pr_supervisor: AgentRunnerPostPrSupervisorSettings | None = None
    generated_content: AgentRunnerGeneratedContentSettings | None = None


class AgentRunnerRepositorySettings(_AgentRunnerRepositoryOverrideSettings):
    """Per-repository Agent Runner configuration overrides."""

    path: str
    id: str | None = None
    enabled: bool = True
    display_name: str | None = None


class AgentRunnerLocalSettings(_AgentRunnerRepositoryOverrideSettings):
    """Repository-local Agent Runner settings loaded from ``.iar.toml``."""

    repository: AgentRunnerRepositoryMetadataSettings = Field(
        default_factory=AgentRunnerRepositoryMetadataSettings
    )


def load_agent_runner_local_settings(
    repo_root_path: Path,
) -> AgentRunnerRepositorySettings | None:
    """Load repository-local IAR settings from ``.iar.toml``.

    Args:
        repo_root_path: Target Git repository root path.

    Returns:
        Repository settings if the local config exists; otherwise ``None``.

    Raises:
        ValueError: If the local config exists but is invalid.
    """
    resolved_repo_path = repo_root_path.resolve()
    local_config_path = resolved_repo_path / IAR_REPOSITORY_CONFIG_FILENAME
    if not local_config_path.is_file():
        return None

    try:
        with open(local_config_path, "rb") as local_config_file:
            local_toml_data: dict[str, Any] = tomllib.load(local_config_file)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"Invalid IAR local config at {local_config_path}: {exc}"
        ) from exc

    agent_runner_section = local_toml_data.get("agent_runner")
    if not isinstance(agent_runner_section, dict):
        raise ValueError(
            f"Invalid IAR local config at {local_config_path}: "
            "missing [agent_runner] section."
        )

    try:
        local_settings = AgentRunnerLocalSettings(**agent_runner_section)
    except ValidationError as exc:
        raise ValueError(
            f"Invalid IAR local config at {local_config_path}: {exc}"
        ) from exc

    repository_metadata = local_settings.repository
    return AgentRunnerRepositorySettings(
        path=str(resolved_repo_path),
        id=repository_metadata.id,
        enabled=repository_metadata.enabled,
        display_name=repository_metadata.display_name,
        labels=local_settings.labels,
        git=local_settings.git,
        worktree=local_settings.worktree,
        runner=local_settings.runner,
        safety=local_settings.safety,
        validation=local_settings.validation,
        prompts=local_settings.prompts,
        pre_push_review=local_settings.pre_push_review,
        post_pr_supervisor=local_settings.post_pr_supervisor,
        generated_content=local_settings.generated_content,
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
    validation: AgentRunnerValidationSettings = Field(
        default_factory=AgentRunnerValidationSettings
    )
    prompts: AgentRunnerPromptSettings = Field(
        default_factory=AgentRunnerPromptSettings
    )
    pre_push_review: AgentRunnerPrePushReviewSettings = Field(
        default_factory=AgentRunnerPrePushReviewSettings
    )
    post_pr_supervisor: AgentRunnerPostPrSupervisorSettings = Field(
        default_factory=AgentRunnerPostPrSupervisorSettings
    )
    deliberation: AgentRunnerDeliberationSettings = Field(
        default_factory=AgentRunnerDeliberationSettings
    )
    generated_content: AgentRunnerGeneratedContentSettings = Field(
        default_factory=AgentRunnerGeneratedContentSettings
    )
    repositories: dict[str, AgentRunnerRepositorySettings] = Field(default_factory=dict)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return _env_toml_init_sources(
            settings_cls, "agent_runner", env_settings, init_settings
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
    "AgentRunnerLocalSettings",
    "AgentRunnerRepositoryMetadataSettings",
    "AgentRunnerGeneratedContentSettings",
    "AgentRunnerGeneratedContentTargetSettings",
    "AgentRunnerGitSettings",
    "AgentRunnerLabelSettings",
    "AgentRunnerPromptSettings",
    "AgentRunnerRepositorySettings",
    "AgentRunnerRunnerSettings",
    "AgentRunnerSafetySettings",
    "AgentRunnerSettings",
    "AppSettings",
    "ChatModelSettings",
    "ChunkingSettings",
    "DatabaseSettings",
    "EmbeddingSettings",
    "IAR_REPOSITORY_CONFIG_FILENAME",
    "MinioSettings",
    "QdrantSettings",
    "TimeoutSettings",
    "config",
    "load_agent_runner_local_settings",
]
