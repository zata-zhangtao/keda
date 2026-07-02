"""配置文件 - 使用 pydantic-settings 集中管理所有配置。

支持三层配置源（优先级从高到低）：
1. 环境变量 / .env / .env.local
2. config.toml（非敏感配置）
3. 代码中的默认值
"""

import os
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
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
IAR_REPOSITORY_CONFIG_FILENAME = ".iar.toml"


def _resolve_project_root_from_settings_path(settings_path: Path) -> Path:
    """Locate the project root by searching upward for ``pyproject.toml``.

    First searches from the settings file location, then from the current
    working directory. Falls back to the legacy directory-based heuristic when
    no marker file is found, preserving behavior for non-package invocations.
    """
    for start_path in (settings_path, Path.cwd()):
        for parent in start_path.parents:
            if (parent / "pyproject.toml").is_file():
                return parent
    return _SOURCE_DIR_PATH.parent


_PROJECT_ROOT_PATH: Path = _resolve_project_root_from_settings_path(_SETTINGS_FILE_PATH)


def _global_iar_dir() -> Path:
    """Return the global IAR state directory under the user's home."""
    return Path.home() / ".iar"


def _ensure_global_config_toml() -> Path | None:
    """Ensure ``~/.iar/config.toml`` exists, seeding from the source root.

    This provides a stable configuration home for globally-installed ``iar``
    invocations outside any project directory.
    """
    global_dir = _global_iar_dir()
    global_config = global_dir / "config.toml"
    if global_config.is_file():
        return global_config
    source_config = _PROJECT_ROOT_PATH / "config.toml"
    if not source_config.is_file():
        return None
    try:
        global_dir.mkdir(parents=True, exist_ok=True)
        global_config.write_text(
            source_config.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return global_config
    except OSError:
        return None


def _find_config_toml() -> Path | None:
    """Resolve the effective config.toml using the standard search order.

    Search order:
    1. ``IAR_CONFIG`` environment variable, if set.
    2. Walk upward from the current working directory.
    3. ``~/.iar/config.toml`` (seeded from the source root if missing).
    4. keda source root config.toml.
    """
    env_config = os.environ.get("IAR_CONFIG")
    if env_config:
        env_path = Path(env_config).expanduser()
        if env_path.is_file():
            return env_path
        if env_path.is_dir():
            candidate = env_path / "config.toml"
            if candidate.is_file():
                return candidate

    cwd = Path.cwd()
    for path in [cwd, *cwd.parents]:
        candidate = path / "config.toml"
        if candidate.is_file():
            return candidate

    global_config = _ensure_global_config_toml()
    if global_config is not None:
        return global_config

    fallback = _PROJECT_ROOT_PATH / "config.toml"
    if fallback.is_file():
        return fallback
    return None


def resolve_config_toml_path() -> Path:
    """解析当前生效的 config.toml 路径（找不到时回退到源码根目录）。"""
    return _find_config_toml() or (_PROJECT_ROOT_PATH / "config.toml")


def resolve_registry_config_toml_path() -> Path:
    """解析仓库 registry 使用的全局 config.toml 路径。

    Registry 记录的是 IAR 托管的所有仓库，必须是全局共享的，不能因为
    用户在某个项目目录内执行命令就写入该项目的 config.toml。

    解析顺序：
    1. ``IAR_CONFIG`` 环境变量（如果显式设置），用于测试或高级用户覆盖。
    2. ``~/.iar/config.toml``（首次调用时从源码根目录 seed 默认配置）。
    3. keda 源码根目录 ``config.toml`` 作为最后 fallback。
    """
    env_config = os.environ.get("IAR_CONFIG")
    if env_config:
        env_path = Path(env_config).expanduser()
        if env_path.is_file() or env_path.parent.exists():
            return env_path
    global_config = _ensure_global_config_toml()
    if global_config is not None:
        return global_config
    return _PROJECT_ROOT_PATH / "config.toml"


def resolve_project_root_path() -> Path:
    """返回 keda 项目源码根目录（托管进程的默认 cwd）。"""
    return _PROJECT_ROOT_PATH


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


def _load_registry_toml_section_data(section_name: str) -> dict[str, Any]:
    """从 registry 专用的 config.toml 加载指定 section。

    Registry 与通用配置解耦：仓库列表必须全局共享，因此优先读取
    ``IAR_CONFIG`` 或 ``~/.iar/config.toml``；仅当全局 registry 不存在时
    fallback 到当前生效的 config.toml（兼容 legacy 项目级 registry）。

    Args:
        section_name: TOML section 名称。

    Returns:
        section 内容字典，文件不存在或 section 不存在时返回空 dict。
    """
    registry_path = resolve_registry_config_toml_path()
    try:
        with open(registry_path, "rb") as toml_file:
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


class _RegistryRepositoriesSource(PydanticBaseSettingsSource):
    """从 registry 专用 config.toml 读取 ``[agent_runner.repositories]`` 的源。"""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        agent_runner_data = _load_registry_toml_section_data("agent_runner")
        self._repositories: dict[str, Any] = agent_runner_data.get("repositories", {})

    def get_field_value(
        self,
        field: Any,  # noqa: ARG002
        field_name: str,
    ) -> tuple[Any, str, bool]:
        if field_name == "repositories":
            return self._repositories, field_name, False
        return None, field_name, False  # type: ignore[return-value]

    def __call__(self) -> dict[str, Any]:
        return {"repositories": self._repositories} if self._repositories else {}


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
    rework_prd: str = "agent/rework-prd"
    deliberate: str = "agent/deliberate"

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
    # Maximum Issues processed in parallel within one daemon pass. 1 keeps the
    # sequential path (zero regression); >1 enables thread-pool parallelism.
    max_concurrent_issues: int = 1
    default_agent: str = "auto"
    max_recovery_attempts: int = 5
    recovery_retry_delay_seconds: int = 30
    # Cross-agent fallback chain. The runner tries the primary agent first, then
    # falls back to the next locally available agent when recovery is exhausted
    # or the provider is capacity-limited. Commands that are not installed on
    # this machine are automatically skipped. Set to [] to disable switching.
    agent_fallback_order: list[str] = Field(
        default_factory=lambda: ["claude", "kimi", "codex"]
    )
    # Maximum number of agent switches before the Issue is marked failed.
    # With order [a, b, c] and max_agent_switches=2, up to 3 agents are tried.
    max_agent_switches: int = 2
    # In-place retries for transient network/transport errors (Level 1).
    transient_retry_attempts: int = 2
    transient_retry_delay_seconds: int = 10
    timeout_seconds: int = 14400
    fix_timeout_seconds: int | None = None
    recovery_timeout_seconds: int | None = None
    inactivity_timeout_seconds: int = 1200
    verification_commands: list[str] = Field(
        default_factory=lambda: [
            "git diff --check",
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
    evidence_format_check: bool = True
    parse_evidence_format_with_agent: bool = True
    language: str = "zh-CN"
    structured_evidence: bool = True
    require_negative_control: bool = True
    reexecute_commands: bool = True
    reexecute_timeout_seconds: int = 300
    reexecute_cache_enabled: bool = True
    verifier_enabled: bool = False
    verifier_agent: str = "auto"
    verifier_timeout_seconds: int = 1800


class AgentRunnerConsoleSettings(BaseModel):
    """统一管理终端（运行历史落库与托管进程）配置。"""

    history_db_path: str = "~/.iar/console.db"
    process_registry_path: str = "~/.iar/processes.json"
    process_log_dir: str = "logs/agent-runner/processes"
    runner_command: list[str] = Field(default_factory=lambda: ["uv", "run", "iar"])
    stop_timeout_seconds: int = 30


class AgentRunnerDaemonSettings(BaseModel):
    """Long-running daemon polling configuration."""

    review_interval_seconds: int = 120
    run_interval_seconds: int = 120
    max_deliberation_issues: int = 1
    reclaim_stale_running: bool = True


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


class AgentRunnerPrePrReviewSettings(BaseModel):
    """Pre-PR AI review gate configuration.

    The review runs **after** the implementation commit has been pushed to the
    remote branch and **before** the Draft PR is created. Reviewer patches are
    themselves pushed so the remote branch always reflects the latest committed
    state when the PR is opened.
    """

    enabled: bool = True
    review_agent: str = "auto"
    allow_same_agent: bool = True
    max_attempts: int = 2
    timeout_seconds: int = 1800
    # When the reviewer reports findings but fails to write a commit request,
    # the runner appends a reminder and re-invokes the reviewer up to this
    # many times within the same review cycle.
    commit_request_reminder_attempts: int = 1
    # Review rules template; supports either a single string or a list of
    # lines. When the field is omitted from TOML the embedded default in
    # ``agent_review.py`` is used so out-of-the-box behavior still calls the
    # ``code-reviewer`` skill.
    review_prompt_template: str | list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_review_prompt_template(self) -> "AgentRunnerPrePrReviewSettings":
        """Collapse empty / scalar values to a stable list representation."""
        if isinstance(self.review_prompt_template, str):
            normalized = (
                [self.review_prompt_template] if self.review_prompt_template else []
            )
        else:
            normalized = [str(item) for item in self.review_prompt_template]
        # Pydantic v2 disallows assigning to a field directly after validation;
        # use object.__setattr__ to keep the field frozen-friendly.
        object.__setattr__(self, "review_prompt_template", normalized)
        return self


class AgentRunnerPostPrSupervisorSettings(BaseModel):
    """Post-PR supervisor cycle configuration."""

    enabled: bool = True
    supervisor_agent: str = "auto"
    max_repair_attempts: int = 2
    max_agent_crash_retries: int = 5
    crash_retry_initial_backoff_seconds: int = 30
    crash_retry_max_backoff_seconds: int = 600


class AgentRunnerDeliberationProfileSettings(BaseModel):
    """Participant profile for deliberation sessions."""

    agent: str = "claude"
    role: str = "participant"
    behavior_prompt: str = "Analyze requirements carefully."


class AgentRunnerInteractiveDecisionSettings(BaseModel):
    """Interactive decision (`iar ask`) configuration."""

    enabled: bool = True
    default_agent: str = "claude"
    default_output_dir: str = "logs/agent-runner/decisions"
    planner_timeout_seconds: int = 120
    max_context_chars: int = 24000
    allow_execute_yes: bool = True  # Allow --yes to skip confirmation.


class AgentRunnerReplSettings(BaseModel):
    """Interactive REPL (`iar` with no subcommand) configuration.

    The REPL entrypoint lets the user chat with a configured agent and
    grants the agent the ability to request execution of whitelisted IAR
    subcommands via ``<<IAR_EXEC>> ... <<END_IAR_EXEC>>`` markers. This
    settings block isolates the REPL's risk surface (default agent,
    command allow/confirm lists, audit directory) from the ``iar ask``
    decision planner.
    """

    enabled: bool = True
    default_agent: str = "claude"
    default_output_dir: str = "logs/agent-runner/repl"
    max_context_chars: int = 24000
    agent_timeout_seconds: int = 120
    # Commands that the executor may run without explicit confirmation.
    # Each entry is a *prefix* matched against the argv tail (everything
    # after ``iar``), so ``"labels sync --dry-run"`` auto-confirms only
    # that exact form. Anything not listed here is either matched against
    # ``confirm_commands`` (which prompts) or rejected outright.
    auto_confirm_commands: list[str] = Field(
        default_factory=lambda: [
            "labels sync --dry-run",
            "run --dry-run",
            "review --dry-run",
            "ask --plan-only",
        ]
    )
    # Commands whose execution prompts the user for confirmation. Matched
    # with the same prefix rules as ``auto_confirm_commands``.
    confirm_commands: list[str] = Field(
        default_factory=lambda: [
            "run",
            "daemon",
            "review",
            "review-daemon",
            "issue create",
            "recover",
            "blocked-continue",
            "worktree create",
            "worktree remove",
        ]
    )


class AgentRunnerDeliberationSettings(BaseModel):
    """Multi-agent deliberation configuration."""

    default_rounds: int = 2
    default_synthesizer: str = "claude"
    default_output_dir: str = "logs/agent-runner/deliberations"
    continue_on_agent_error: bool = True
    agent_failure_timeout_seconds: int = 300
    stale_rounds_before_hint: int = 3
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
    """生成内容目标配置，支持 TOML 字符串列表语法。

    用于 ``issue_from_prd``、``draft_pr``、``prd_from_issue`` 三类生成目标，
    分别控制 Issue、PR、PRD 的标题/正文生成方式。
    """

    enabled: bool = True
    # 仅接受 template / agent；非法值（如手误 "agnet"）在配置加载期直接报错，
    # 而不是静默退回 fallback。默认 template 避免未配置时调用 AI 超时。
    mode: Literal["template", "agent"] = "template"
    output: str = "json"
    title_template: str | list[str] = ""
    body_template: str | list[str] = ""
    agent: str = "auto"
    timeout_seconds: int = 120
    prompt: str | list[str] = ""
    include_commit_log: bool = True
    include_diff_stat: bool = True

    @model_validator(mode="after")
    def _join_list_templates(self) -> "AgentRunnerGeneratedContentTargetSettings":
        """将 list[str] 类型的模板字段合并为单个字符串。

        TOML 中多行模板通常以字符串列表书写，便于版本控制审阅；
        加载后需要拼接成完整模板字符串供 ``str.format()`` 渲染。
        """
        for field_name in ("title_template", "body_template", "prompt"):
            value = getattr(self, field_name)
            if isinstance(value, list):
                setattr(self, field_name, "\n".join(value))
        return self


class AgentRunnerGeneratedContentSettings(BaseModel):
    """GitHub Issue 与 PR 的生成内容全局配置。

    聚合三类生成目标（Issue、PR、PRD）的共享参数，如是否启用、
    失败回退策略、最大输入长度以及默认 agent。
    """

    enabled: bool = True
    fallback: str = "template"
    max_input_chars: int = 20000
    default_agent: str = "auto"
    issue_from_prd: AgentRunnerGeneratedContentTargetSettings = Field(
        default_factory=AgentRunnerGeneratedContentTargetSettings
    )
    draft_pr: AgentRunnerGeneratedContentTargetSettings = Field(
        default_factory=AgentRunnerGeneratedContentTargetSettings
    )
    prd_from_issue: AgentRunnerGeneratedContentTargetSettings = Field(
        # PRD 生成没有可用的内置 template，唯一有意义的模式是 agent；
        # agent 不可用时 generate_prd_content 会优雅退回 fallback。
        default_factory=lambda: AgentRunnerGeneratedContentTargetSettings(mode="agent")
    )


class AgentRunnerRepositoryMetadataSettings(BaseModel):
    """Repository identity stored in repository-local IAR config."""

    id: str | None = None
    enabled: bool = True
    display_name: str | None = None
    # Optional ``owner/name`` string passed to ``gh pr list --repo`` so the
    # PR column on ``iar issue list`` is populated. Omitting it is allowed
    # — the PR column then stays empty with a one-shot stderr warning.
    github_repo: str | None = None

    @field_validator("github_repo")
    @classmethod
    def _validate_github_repo_format(cls, value: str | None) -> str | None:
        """Reject malformed ``github_repo`` values at config load time.

        Format: ``owner/name`` with non-empty owner / name and no leading
        or trailing slash. ``None`` and empty string are accepted (the
        field is optional).
        """
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "Invalid github_repo: must be a non-empty 'owner/name' "
                "string or null."
            )
        if "/" not in value or value.startswith("/") or value.endswith("/"):
            raise ValueError(
                f"Invalid github_repo {value!r}; expected 'owner/name' format."
            )
        owner_part, _, name_part = value.partition("/")
        if not owner_part or not name_part or "/" in name_part:
            raise ValueError(
                f"Invalid github_repo {value!r}; expected 'owner/name' format."
            )
        return value


class _AgentRunnerRepositoryOverrideSettings(BaseModel):
    """Optional Agent Runner overrides shared by registry and local config."""

    labels: AgentRunnerLabelSettings | None = None
    git: AgentRunnerGitSettings | None = None
    worktree: AgentRunnerWorktreeSettings | None = None
    runner: AgentRunnerRunnerSettings | None = None
    safety: AgentRunnerSafetySettings | None = None
    validation: AgentRunnerValidationSettings | None = None
    prompts: AgentRunnerPromptSettings | None = None
    pre_pr_review: AgentRunnerPrePrReviewSettings | None = None
    post_pr_supervisor: AgentRunnerPostPrSupervisorSettings | None = None
    generated_content: AgentRunnerGeneratedContentSettings | None = None
    interactive_decision: AgentRunnerInteractiveDecisionSettings | None = None
    deliberation: AgentRunnerDeliberationSettings | None = None
    repl: AgentRunnerReplSettings | None = None


class AgentRunnerRepositorySettings(_AgentRunnerRepositoryOverrideSettings):
    """Per-repository Agent Runner configuration overrides."""

    path: str
    id: str | None = None
    enabled: bool = True
    display_name: str | None = None
    # Optional ``owner/name`` string passed to ``gh pr list --repo``.
    # Mirrors the same field on ``AgentRunnerRepositoryMetadataSettings``;
    # the local-config loader propagates the value at merge time. See
    # the field validator on the metadata class for the format contract.
    github_repo: str | None = None

    @field_validator("github_repo")
    @classmethod
    def _validate_github_repo_format(cls, value: str | None) -> str | None:
        """Mirror the metadata-level validation for registry entries."""
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "Invalid github_repo: must be a non-empty 'owner/name' "
                "string or null."
            )
        if "/" not in value or value.startswith("/") or value.endswith("/"):
            raise ValueError(
                f"Invalid github_repo {value!r}; expected 'owner/name' format."
            )
        owner_part, _, name_part = value.partition("/")
        if not owner_part or not name_part or "/" in name_part:
            raise ValueError(
                f"Invalid github_repo {value!r}; expected 'owner/name' format."
            )
        return value


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
        github_repo=repository_metadata.github_repo,
        labels=local_settings.labels,
        git=local_settings.git,
        worktree=local_settings.worktree,
        runner=local_settings.runner,
        safety=local_settings.safety,
        validation=local_settings.validation,
        prompts=local_settings.prompts,
        pre_pr_review=local_settings.pre_pr_review,
        post_pr_supervisor=local_settings.post_pr_supervisor,
        generated_content=local_settings.generated_content,
        interactive_decision=local_settings.interactive_decision,
        deliberation=local_settings.deliberation,
        repl=local_settings.repl,
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
    console: AgentRunnerConsoleSettings = Field(
        default_factory=AgentRunnerConsoleSettings
    )
    daemon: AgentRunnerDaemonSettings = Field(default_factory=AgentRunnerDaemonSettings)
    prompts: AgentRunnerPromptSettings = Field(
        default_factory=AgentRunnerPromptSettings
    )
    pre_pr_review: AgentRunnerPrePrReviewSettings = Field(
        default_factory=AgentRunnerPrePrReviewSettings
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
    interactive_decision: AgentRunnerInteractiveDecisionSettings = Field(
        default_factory=AgentRunnerInteractiveDecisionSettings
    )
    repl: AgentRunnerReplSettings = Field(default_factory=AgentRunnerReplSettings)
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
        toml_source = _TomlSectionSource(settings_cls, "agent_runner")
        return (
            env_settings,
            _RegistryRepositoriesSource(settings_cls),
            toml_source,
            init_settings,
        )


class PreviewSettings(BaseSettings):
    """Preview deployment configuration (non-sensitive structure only)."""

    model_config = SettingsConfigDict(env_prefix="PREVIEW_")

    enabled: bool = False
    base_domain: str = "preview.example.com"
    project_slug: str = "keda"
    app_dir_root: str = "/opt/preview"
    registry_host: str = "ghcr.io"
    registry_namespace: str = "zata-zhangtao"
    traefik_network: str = "traefik"
    url_scheme: str = "https"
    subdomain_template: str = "pr-{pr_number}.{base_domain}"
    compose_template: str = "{project_slug}-pr-{pr_number}"

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
            settings_cls, "preview", env_settings, init_settings
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
    preview: PreviewSettings = Field(default_factory=PreviewSettings)

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
    "AgentRunnerConsoleSettings",
    "AgentRunnerDaemonSettings",
    "AgentRunnerGeneratedContentSettings",
    "AgentRunnerGeneratedContentTargetSettings",
    "AgentRunnerGitSettings",
    "AgentRunnerLabelSettings",
    "AgentRunnerPromptSettings",
    "AgentRunnerReplSettings",
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
    "PreviewSettings",
    "QdrantSettings",
    "TimeoutSettings",
    "config",
    "load_agent_runner_local_settings",
]
