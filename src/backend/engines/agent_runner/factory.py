"""Agent Runner infrastructure adapter and factory.

This module bridges the ``core/`` business layer with ``infrastructure/``
implementations by instantiating concrete clients/runners and converting
pydantic-settings configuration into the frozen dataclasses expected by use
cases.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from backend.core.shared.interfaces.agent_output_view import IAgentOutputView
from backend.core.shared.interfaces.agent_runner import IContentGenerator
from backend.core.shared.models.agent_deliberation import (
    DeliberationAgentProfile,
    DeliberationConfig,
    DeliberationEvent,
)
from backend.core.shared.models.agent_decision import (
    InteractiveDecisionConfig,
    ReplConfig,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    GeneratedContentConfig,
    GeneratedContentTargetConfig,
    GitConfig,
    LabelConfig,
    PostPrSupervisorConfig,
    PrePrReviewConfig,
    PromptConfig,
    RepositoryRunContext,
    RunnerConfig,
    SafetyConfig,
    ValidationConfig,
    WorktreeConfig,
)
from backend.core.shared.interfaces.runner_console import (
    IRoadmapStore,
    IRepositoryRegistryEditor,
    IRunHistoryStore,
    IRunnerProcessSupervisor,
)
from backend.engines.agent_runner.repository_local import detect_git_repository_root
from backend.engines.agent_runner.transcript_runner import create_transcript_runner
from backend.infrastructure.config.registry_editor import TomlRegistryEditor
from backend.infrastructure.config.settings import (
    AgentRunnerDeliberationSettings,
    AgentRunnerGeneratedContentSettings,
    AgentRunnerGeneratedContentTargetSettings,
    AgentRunnerLabelSettings,
    AgentRunnerPromptSettings,
    AgentRunnerReplSettings,
    AgentRunnerRepositorySettings,
    AgentRunnerSettings,
    IAR_REPOSITORY_CONFIG_FILENAME,
    config,
    load_agent_runner_local_settings,
    resolve_config_toml_path,
    resolve_project_root_path,
)
from backend.infrastructure.console.process_supervisor import (
    PidfileProcessSupervisor,
)
from backend.infrastructure.github_client import GitHubCliClient
from backend.infrastructure.persistence.console_store import SqliteConsoleStore
from backend.engines.agent_runner.persistence.loop_state_json import (
    JsonLoopStateStore,
    resolve_loop_state_path,
)
from backend.infrastructure.logging.logger import logger
from backend.infrastructure.process_runner import SubprocessRunner
from backend.engines.agent_runner.scheduler.loop_clock import SystemClock
from backend.engines.agent_runner.deliberation_outputs import (
    write_deliberation_outputs,
)

__all__ = [
    "logger",
    "build_deliberation_config_from_settings",
    "create_content_generator",
    "create_event_sink",
    "create_github_client",
    "create_loop_clock",
    "create_loop_state_store",
    "create_process_runner",
    "create_transcript_runner",
    "create_planner_runner",
    "get_agent_runner_settings",
    "get_agent_runner_status_data",
    "build_app_config",
    "build_app_config_from_settings",
    "merge_repository_config",
    "resolve_repository_targets",
    "resolve_issue_from_prd_target",
    "create_transcript_runner",
    "write_deliberation_outputs",
    "create_event_sink",
    "create_roadmap_store",
    "create_repl_command_executor",
]


def _build_generated_content_target_config(
    target_settings: AgentRunnerGeneratedContentTargetSettings,
) -> GeneratedContentTargetConfig:
    """Convert pydantic target settings to frozen core config."""
    return GeneratedContentTargetConfig(
        enabled=target_settings.enabled,
        mode=target_settings.mode,
        output=target_settings.output,
        title_template=target_settings.title_template,
        body_template=target_settings.body_template,
        agent=target_settings.agent,
        timeout_seconds=target_settings.timeout_seconds,
        prompt=target_settings.prompt,
        include_commit_log=target_settings.include_commit_log,
        include_diff_stat=target_settings.include_diff_stat,
    )


def _build_generated_content_config(
    gc_settings: AgentRunnerGeneratedContentSettings,
) -> GeneratedContentConfig:
    """Convert pydantic generated-content settings to frozen core config."""
    return GeneratedContentConfig(
        enabled=gc_settings.enabled,
        fallback=gc_settings.fallback,
        max_input_chars=gc_settings.max_input_chars,
        default_agent=gc_settings.default_agent,
        issue_from_prd=_build_generated_content_target_config(
            gc_settings.issue_from_prd
        ),
        draft_pr=_build_generated_content_target_config(gc_settings.draft_pr),
        prd_from_issue=_build_generated_content_target_config(
            gc_settings.prd_from_issue
        ),
    )


def _build_deliberation_config(
    deliberation_settings: AgentRunnerDeliberationSettings,
) -> DeliberationConfig:
    """Convert pydantic deliberation settings to frozen core config."""
    profiles = tuple(
        DeliberationAgentProfile(
            profile_id=profile_id,
            agent=profile.agent,
            role=profile.role,
            behavior_prompt=profile.behavior_prompt,
        )
        for profile_id, profile in deliberation_settings.profiles.items()
    )
    return DeliberationConfig(
        default_rounds=deliberation_settings.default_rounds,
        default_synthesizer=deliberation_settings.default_synthesizer,
        default_output_dir=deliberation_settings.default_output_dir,
        continue_on_agent_error=deliberation_settings.continue_on_agent_error,
        agent_failure_timeout_seconds=deliberation_settings.agent_failure_timeout_seconds,
        stale_rounds_before_hint=deliberation_settings.stale_rounds_before_hint,
        profiles=profiles,
    )


def _build_repl_config(repl_settings: AgentRunnerReplSettings) -> ReplConfig:
    """Convert pydantic REPL settings to frozen core config."""
    return ReplConfig(
        enabled=repl_settings.enabled,
        default_agent=repl_settings.default_agent,
        default_output_dir=repl_settings.default_output_dir,
        max_context_chars=repl_settings.max_context_chars,
        agent_timeout_seconds=repl_settings.agent_timeout_seconds,
        auto_confirm_commands=tuple(repl_settings.auto_confirm_commands),
        confirm_commands=tuple(repl_settings.confirm_commands),
    )


def build_app_config_from_settings(
    agent_runner_settings: AgentRunnerSettings,
) -> AppConfig:
    """Convert pydantic-settings ``AgentRunnerSettings`` to frozen ``AppConfig``."""
    label_settings = agent_runner_settings.labels
    git_settings = agent_runner_settings.git
    worktree_settings = agent_runner_settings.worktree
    runner_settings = agent_runner_settings.runner
    safety_settings = agent_runner_settings.safety
    validation_settings = agent_runner_settings.validation
    prompt_settings = agent_runner_settings.prompts

    pre_pr = agent_runner_settings.pre_pr_review
    post_supervisor = agent_runner_settings.post_pr_supervisor
    generated_content = _build_generated_content_config(
        agent_runner_settings.generated_content
    )
    interactive_decision = agent_runner_settings.interactive_decision
    repl = _build_repl_config(agent_runner_settings.repl)
    deliberation = _build_deliberation_config(agent_runner_settings.deliberation)

    return AppConfig(
        labels=LabelConfig(
            ready=label_settings.ready,
            running=label_settings.running,
            supervising=label_settings.supervising,
            review=label_settings.review,
            failed=label_settings.failed,
            blocked=label_settings.blocked,
            waiting=label_settings.waiting,
            validation_pending=label_settings.validation_pending,
            validation_passed=label_settings.validation_passed,
            group_prefix=label_settings.group_prefix,
            rework_prd=label_settings.rework_prd,
            deliberate=label_settings.deliberate,
            agent_labels=label_settings.agent_labels,
        ),
        git=GitConfig(
            remote=git_settings.remote,
            base_branch=git_settings.base_branch,
        ),
        worktree=WorktreeConfig(
            create_command=worktree_settings.create_command,
            reuse_command=worktree_settings.reuse_command,
            path_command=worktree_settings.path_command,
            base_branch=git_settings.base_branch,
        ),
        runner=RunnerConfig(
            max_issues=runner_settings.max_issues,
            max_concurrent_issues=runner_settings.max_concurrent_issues,
            default_agent=runner_settings.default_agent,
            max_recovery_attempts=runner_settings.max_recovery_attempts,
            recovery_retry_delay_seconds=runner_settings.recovery_retry_delay_seconds,
            agent_fallback_order=tuple(runner_settings.agent_fallback_order),
            max_agent_switches=runner_settings.max_agent_switches,
            transient_retry_attempts=runner_settings.transient_retry_attempts,
            transient_retry_delay_seconds=runner_settings.transient_retry_delay_seconds,
            timeout_seconds=runner_settings.timeout_seconds,
            fix_timeout_seconds=runner_settings.fix_timeout_seconds,
            recovery_timeout_seconds=runner_settings.recovery_timeout_seconds,
            inactivity_timeout_seconds=runner_settings.inactivity_timeout_seconds,
            verification_commands=tuple(runner_settings.verification_commands),
        ),
        safety=SafetyConfig(
            auto_merge=safety_settings.auto_merge,
            forbidden_path_patterns=tuple(safety_settings.forbidden_path_patterns),
        ),
        validation=ValidationConfig(
            enabled=validation_settings.enabled,
            evidence_dir=validation_settings.evidence_dir,
            branch_prefix=validation_settings.branch_prefix,
            evidence_format_check=validation_settings.evidence_format_check,
            parse_evidence_format_with_agent=validation_settings.parse_evidence_format_with_agent,
            language=validation_settings.language,
            structured_evidence=validation_settings.structured_evidence,
        ),
        prompts=PromptConfig(
            default_phase=prompt_settings.default_phase,
            phases=dict(prompt_settings.phases),
        ),
        pre_pr_review=PrePrReviewConfig(
            enabled=pre_pr.enabled,
            review_agent=pre_pr.review_agent,
            allow_same_agent=pre_pr.allow_same_agent,
            max_attempts=pre_pr.max_attempts,
            timeout_seconds=pre_pr.timeout_seconds,
            commit_request_reminder_attempts=pre_pr.commit_request_reminder_attempts,
            review_prompt_template=tuple(pre_pr.review_prompt_template),
        ),
        post_pr_supervisor=PostPrSupervisorConfig(
            enabled=post_supervisor.enabled,
            supervisor_agent=post_supervisor.supervisor_agent,
            max_repair_attempts=post_supervisor.max_repair_attempts,
            max_agent_crash_retries=post_supervisor.max_agent_crash_retries,
            crash_retry_initial_backoff_seconds=(
                post_supervisor.crash_retry_initial_backoff_seconds
            ),
            crash_retry_max_backoff_seconds=(
                post_supervisor.crash_retry_max_backoff_seconds
            ),
        ),
        generated_content=generated_content,
        interactive_decision=InteractiveDecisionConfig(
            enabled=interactive_decision.enabled,
            default_agent=interactive_decision.default_agent,
            default_output_dir=interactive_decision.default_output_dir,
            planner_timeout_seconds=interactive_decision.planner_timeout_seconds,
            max_context_chars=interactive_decision.max_context_chars,
            allow_execute_yes=interactive_decision.allow_execute_yes,
        ),
        repl=repl,
        deliberation=deliberation,
    )


def build_app_config() -> AppConfig:
    """Convert global pydantic-settings ``AgentRunnerSettings`` to frozen ``AppConfig``."""
    return build_app_config_from_settings(config.agent_runner)


def get_agent_runner_settings() -> AgentRunnerSettings:
    """Return the global ``AgentRunnerSettings`` instance."""
    return config.agent_runner


def load_fresh_agent_runner_settings() -> AgentRunnerSettings:
    """重新从 config.toml 与环境变量加载 ``AgentRunnerSettings``。

    管理终端写回 registry 后，进程级单例 ``config`` 不会自动刷新；
    需要即时反映 registry 变化的读路径（监控 overview、console）应使用
    本函数而非 :func:`get_agent_runner_settings`。
    """
    return AgentRunnerSettings()


def get_agent_runner_status_data() -> dict:
    """Build the status response dict for the FastAPI status endpoint.

    Returns:
        A dictionary with ``daemon_mode``, ``config`` (global runner settings),
        and ``repositories`` (list of per-repository summaries).
    """
    agent_runner_settings = config.agent_runner
    app_config = build_app_config_from_settings(agent_runner_settings)
    repositories = []
    for repo_id in agent_runner_settings.repositories:
        repo = agent_runner_settings.repositories[repo_id]
        base_branch = app_config.git.base_branch
        remote = app_config.git.remote
        if repo.git is not None:
            base_branch = repo.git.base_branch or base_branch
            remote = repo.git.remote or remote
        repositories.append(
            {
                "repo_id": repo_id,
                "display_name": repo.display_name or repo_id,
                "enabled": repo.enabled,
                "base_branch": base_branch,
                "remote": remote,
            }
        )
    return {
        "daemon_mode": False,
        "config": {
            "max_issues": app_config.runner.max_issues,
            "default_agent": app_config.runner.default_agent,
            "max_recovery_attempts": app_config.runner.max_recovery_attempts,
            "recovery_retry_delay_seconds": (
                app_config.runner.recovery_retry_delay_seconds
            ),
            "ready_label": app_config.labels.ready,
            "running_label": app_config.labels.running,
            "supervising_label": app_config.labels.supervising,
            "review_label": app_config.labels.review,
            "failed_label": app_config.labels.failed,
            "base_branch": app_config.git.base_branch,
            "remote": app_config.git.remote,
            "auto_merge": app_config.safety.auto_merge,
            "forbidden_path_patterns": list(app_config.safety.forbidden_path_patterns),
            "pre_pr_review_enabled": app_config.pre_pr_review.enabled,
            "post_pr_supervisor_enabled": app_config.post_pr_supervisor.enabled,
        },
        "repositories": repositories,
    }


def _model_to_dict(model: object) -> dict:
    """Convert a pydantic BaseModel or dataclass to a plain dict."""
    if isinstance(model, BaseModel):
        return model.model_dump()
    return dataclasses.asdict(model)


def _pydantic_override_dict(override_model: BaseModel) -> dict:
    """Return only explicitly-set fields from a pydantic override model."""
    return {
        k: v
        for k, v in override_model.model_dump().items()
        if k in override_model.model_fields_set
    }


def _merge_optional_model(base_model, override_model):
    """Merge a pydantic override model into a base model, returning a new base instance."""
    if override_model is None:
        return base_model
    merged_data = {
        **_model_to_dict(base_model),
        **_pydantic_override_dict(override_model),
    }
    return type(base_model)(**merged_data)


def _merge_label_config(
    base_config: LabelConfig, override: AgentRunnerLabelSettings | None
) -> LabelConfig:
    """Merge repository-specific label overrides into a base ``LabelConfig``."""
    if override is None:
        return base_config
    override_data = _pydantic_override_dict(override)
    agent_labels = dict(base_config.agent_labels)
    for agent_key in ("codex", "claude", "kimi"):
        if agent_key in override_data:
            agent_labels[agent_key] = override_data[agent_key]
    return LabelConfig(
        ready=override_data.get("ready", base_config.ready),
        running=override_data.get("running", base_config.running),
        supervising=override_data.get("supervising", base_config.supervising),
        review=override_data.get("review", base_config.review),
        failed=override_data.get("failed", base_config.failed),
        blocked=override_data.get("blocked", base_config.blocked),
        waiting=override_data.get("waiting", base_config.waiting),
        validation_pending=override_data.get(
            "validation_pending", base_config.validation_pending
        ),
        validation_passed=override_data.get(
            "validation_passed", base_config.validation_passed
        ),
        group_prefix=override_data.get("group_prefix", base_config.group_prefix),
        rework_prd=override_data.get("rework_prd", base_config.rework_prd),
        deliberate=override_data.get("deliberate", base_config.deliberate),
        agent_labels=agent_labels,
    )


def _merge_prompt_config(
    base_config: PromptConfig, override: AgentRunnerPromptSettings | None
) -> PromptConfig:
    """Merge repository-specific prompt overrides into a base ``PromptConfig``."""
    if override is None:
        return base_config
    override_data = _pydantic_override_dict(override)
    phases = dict(base_config.phases)
    if "phases" in override_data:
        phases.update(override_data["phases"])
    return PromptConfig(
        default_phase=override_data.get("default_phase", base_config.default_phase),
        phases=phases,
    )


def _merge_generated_content_target_config(
    base_config: GeneratedContentTargetConfig,
    override: AgentRunnerGeneratedContentTargetSettings | None,
) -> GeneratedContentTargetConfig:
    """Merge repository-specific generated-content target overrides."""
    if override is None:
        return base_config
    override_data = _pydantic_override_dict(override)
    return GeneratedContentTargetConfig(
        enabled=override_data.get("enabled", base_config.enabled),
        mode=override_data.get("mode", base_config.mode),
        output=override_data.get("output", base_config.output),
        title_template=override_data.get("title_template", base_config.title_template),
        body_template=override_data.get("body_template", base_config.body_template),
        agent=override_data.get("agent", base_config.agent),
        timeout_seconds=override_data.get(
            "timeout_seconds", base_config.timeout_seconds
        ),
        prompt=override_data.get("prompt", base_config.prompt),
        include_commit_log=override_data.get(
            "include_commit_log", base_config.include_commit_log
        ),
        include_diff_stat=override_data.get(
            "include_diff_stat", base_config.include_diff_stat
        ),
    )


def _merge_generated_content_config(
    base_config: GeneratedContentConfig,
    override: AgentRunnerGeneratedContentSettings | None,
) -> GeneratedContentConfig:
    """Merge repository-specific generated-content overrides."""
    if override is None:
        return base_config
    override_data = _pydantic_override_dict(override)
    return GeneratedContentConfig(
        enabled=override_data.get("enabled", base_config.enabled),
        fallback=override_data.get("fallback", base_config.fallback),
        max_input_chars=override_data.get(
            "max_input_chars", base_config.max_input_chars
        ),
        default_agent=override_data.get("default_agent", base_config.default_agent),
        issue_from_prd=_merge_generated_content_target_config(
            base_config.issue_from_prd,
            override.issue_from_prd if "issue_from_prd" in override_data else None,
        ),
        draft_pr=_merge_generated_content_target_config(
            base_config.draft_pr,
            override.draft_pr if "draft_pr" in override_data else None,
        ),
        prd_from_issue=_merge_generated_content_target_config(
            base_config.prd_from_issue,
            override.prd_from_issue if "prd_from_issue" in override_data else None,
        ),
    )


def _merge_deliberation_config(
    base_config: DeliberationConfig,
    override: AgentRunnerDeliberationSettings | None,
) -> DeliberationConfig:
    """Merge repository-specific deliberation overrides into a base config."""
    if override is None:
        return base_config
    override_data = _pydantic_override_dict(override)
    profiles = {profile.profile_id: profile for profile in base_config.profiles}
    if "profiles" in override_data:
        profiles.update(
            {
                profile_id: DeliberationAgentProfile(
                    profile_id=profile_id,
                    agent=profile.agent,
                    role=profile.role,
                    behavior_prompt=profile.behavior_prompt,
                )
                for profile_id, profile in override.profiles.items()
            }
        )
    return DeliberationConfig(
        default_rounds=override_data.get("default_rounds", base_config.default_rounds),
        default_synthesizer=override_data.get(
            "default_synthesizer", base_config.default_synthesizer
        ),
        default_output_dir=override_data.get(
            "default_output_dir", base_config.default_output_dir
        ),
        continue_on_agent_error=override_data.get(
            "continue_on_agent_error", base_config.continue_on_agent_error
        ),
        agent_failure_timeout_seconds=override_data.get(
            "agent_failure_timeout_seconds", base_config.agent_failure_timeout_seconds
        ),
        stale_rounds_before_hint=override_data.get(
            "stale_rounds_before_hint", base_config.stale_rounds_before_hint
        ),
        profiles=tuple(profiles.values()),
    )


def merge_repository_config(
    global_config: AppConfig, repo_settings: AgentRunnerRepositorySettings
) -> AppConfig:
    """Merge repository-specific overrides into a global ``AppConfig``.

    Args:
        global_config: The global application configuration.
        repo_settings: Repository-specific override settings.

    Returns:
        A new ``AppConfig`` with per-repository overrides applied.
    """
    labels = _merge_label_config(global_config.labels, repo_settings.labels)
    git = _merge_optional_model(global_config.git, repo_settings.git)
    worktree = _merge_optional_model(global_config.worktree, repo_settings.worktree)
    runner = _merge_optional_model(global_config.runner, repo_settings.runner)
    safety = _merge_optional_model(global_config.safety, repo_settings.safety)
    validation = _merge_optional_model(
        global_config.validation, repo_settings.validation
    )
    prompts = _merge_prompt_config(global_config.prompts, repo_settings.prompts)
    pre_pr_review = _merge_optional_model(
        global_config.pre_pr_review, repo_settings.pre_pr_review
    )
    post_pr_supervisor = _merge_optional_model(
        global_config.post_pr_supervisor, repo_settings.post_pr_supervisor
    )
    generated_content = _merge_generated_content_config(
        global_config.generated_content, repo_settings.generated_content
    )
    interactive_decision = _merge_optional_model(
        global_config.interactive_decision, repo_settings.interactive_decision
    )
    deliberation = _merge_deliberation_config(
        global_config.deliberation, repo_settings.deliberation
    )
    repl = _merge_optional_model(global_config.repl, repo_settings.repl)
    return AppConfig(
        labels=labels,
        git=git,
        worktree=worktree,
        runner=runner,
        safety=safety,
        validation=validation,
        prompts=prompts,
        pre_pr_review=pre_pr_review,
        post_pr_supervisor=post_pr_supervisor,
        generated_content=generated_content,
        interactive_decision=interactive_decision,
        repl=repl,
        deliberation=deliberation,
    )


@dataclasses.dataclass(frozen=True)
class _RepositoryMatchResult:
    """Result of matching a local path against the registry."""

    matched_repo_id: str | None
    matched_entry: AgentRunnerRepositorySettings | None
    disabled_repo_id: str | None
    disabled_entry: AgentRunnerRepositorySettings | None
    enabled_candidates: tuple[tuple[str, AgentRunnerRepositorySettings], ...]

    @property
    def is_unique_enabled(self) -> bool:
        return self.matched_repo_id is not None

    @property
    def is_disabled(self) -> bool:
        return self.matched_repo_id is None and self.disabled_repo_id is not None

    @property
    def is_ambiguous(self) -> bool:
        return len(self.enabled_candidates) >= 2

    @property
    def is_no_match(self) -> bool:
        return self.matched_repo_id is None and self.disabled_repo_id is None


def find_repository_match_for_path(
    settings: AgentRunnerSettings,
    candidate_path: Path,
) -> _RepositoryMatchResult:
    """Match a local path against enabled registry entries.

    Args:
        settings: Agent runner settings.
        candidate_path: Path to match (typically the git root of cwd).

    Returns:
        Structured match result indicating unique enabled match, disabled match,
        multiple enabled matches, or no match.
    """
    resolved_candidate = candidate_path.expanduser().resolve()
    enabled_hits: list[tuple[str, AgentRunnerRepositorySettings]] = []
    disabled_hits: list[tuple[str, AgentRunnerRepositorySettings]] = []
    for repo_id, repo_settings in settings.repositories.items():
        resolved_path = Path(repo_settings.path).expanduser().resolve()
        if resolved_path != resolved_candidate:
            continue
        if repo_settings.enabled:
            enabled_hits.append((repo_id, repo_settings))
        else:
            disabled_hits.append((repo_id, repo_settings))
    if len(enabled_hits) == 1 and not disabled_hits:
        matched_repo_id, matched_entry = enabled_hits[0]
        return _RepositoryMatchResult(
            matched_repo_id=matched_repo_id,
            matched_entry=matched_entry,
            disabled_repo_id=None,
            disabled_entry=None,
            enabled_candidates=tuple(enabled_hits),
        )
    if not enabled_hits and len(disabled_hits) == 1:
        disabled_repo_id, disabled_entry = disabled_hits[0]
        return _RepositoryMatchResult(
            matched_repo_id=None,
            matched_entry=None,
            disabled_repo_id=disabled_repo_id,
            disabled_entry=disabled_entry,
            enabled_candidates=(),
        )
    return _RepositoryMatchResult(
        matched_repo_id=None,
        matched_entry=None,
        disabled_repo_id=None,
        disabled_entry=None,
        enabled_candidates=tuple(enabled_hits),
    )


def resolve_repository_targets(
    settings: AgentRunnerSettings,
    *,
    repo_id: str | None = None,
    repo_path_override: str | None = None,
    fallback_path: str = ".",
    all_repositories: bool = False,
) -> list[RepositoryRunContext]:
    """Resolve target repositories for consumer commands.

    Args:
        settings: Agent runner settings.
        repo_id: Optional configured repository ID selector.
        repo_path_override: Optional ad-hoc repository path selector.
        fallback_path: Path to use when no repositories are configured.
        all_repositories: Whether to select all enabled configured repositories.

    Returns:
        List of repository run contexts.

    Raises:
        ValueError: If both ``repo_id`` and ``repo_path_override`` are provided,
            or if selectors are invalid or disabled.
    """
    if repo_path_override is not None and repo_id is not None:
        raise ValueError("--repo and --repo-id are mutually exclusive.")
    if all_repositories and (repo_path_override is not None or repo_id is not None):
        raise ValueError("--all cannot be combined with --repo or --repo-id.")

    global_config = build_app_config_from_settings(settings)

    if repo_path_override is not None:
        repo_root_path = detect_git_repository_root(Path(repo_path_override))
        return [
            _build_repository_context_from_settings(
                global_config,
                _repository_settings_for_path(repo_root_path),
                fallback_repo_id="ad-hoc",
                prefer_settings_id=True,
            )
        ]

    if repo_id is not None:
        if repo_id not in settings.repositories:
            raise ValueError(f"Repository '{repo_id}' not found in config.")
        repo_settings = settings.repositories[repo_id]
        if not repo_settings.enabled:
            raise ValueError(f"Repository '{repo_id}' is disabled.")
        repo_root_path = detect_git_repository_root(Path(repo_settings.path))
        repository_settings = [repo_settings]
        local_settings = _load_enabled_repository_local_settings(repo_root_path)
        if local_settings is not None:
            repository_settings.append(local_settings)
        return [
            _build_merged_repository_context(
                global_config,
                tuple(repository_settings),
                fallback_repo_id=repo_id,
                prefer_settings_id=False,
            )
        ]

    if all_repositories:
        enabled_repos = {
            rid: rcfg for rid, rcfg in settings.repositories.items() if rcfg.enabled
        }
        if not enabled_repos:
            raise ValueError("--all was provided, but no enabled repositories exist.")
        contexts: list[RepositoryRunContext] = []
        for rid, repo_settings in enabled_repos.items():
            repo_root_path = detect_git_repository_root(Path(repo_settings.path))
            repository_settings = [repo_settings]
            local_settings = _load_enabled_repository_local_settings(repo_root_path)
            if local_settings is not None:
                repository_settings.append(local_settings)
            contexts.append(
                _build_merged_repository_context(
                    global_config,
                    tuple(repository_settings),
                    fallback_repo_id=rid,
                    prefer_settings_id=False,
                )
            )
        return contexts

    repo_root_path = detect_git_repository_root(Path(fallback_path))
    return [
        _build_repository_context_from_settings(
            global_config,
            _repository_settings_for_path(repo_root_path),
            fallback_repo_id=repo_root_path.name,
            prefer_settings_id=True,
        )
    ]


@dataclasses.dataclass(frozen=True)
class RepositoryResolutionFailure:
    """A registry entry that could not be resolved into a run context."""

    repo_id: str
    display_name: str
    configured_path: str
    error: str


def resolve_repository_targets_with_diagnostics(
    settings: AgentRunnerSettings,
    *,
    fallback_path: str = ".",
) -> tuple[list[RepositoryRunContext], list[RepositoryResolutionFailure]]:
    """Resolve all enabled repositories, isolating per-repository failures.

    Unlike :func:`resolve_repository_targets`, a registry entry whose path no
    longer exists (or whose local config is invalid) does not abort the whole
    resolution. This keeps read-only surfaces such as the monitoring console
    available when a single configured repository drifts.

    Args:
        settings: Agent runner settings.
        fallback_path: Path used when no repositories are configured.

    Returns:
        Tuple of (resolved contexts, per-repository resolution failures).
    """
    global_config = build_app_config_from_settings(settings)
    enabled_repos = {
        rid: rcfg for rid, rcfg in settings.repositories.items() if rcfg.enabled
    }
    if not enabled_repos:
        return (
            resolve_repository_targets(settings, fallback_path=fallback_path),
            [],
        )

    contexts: list[RepositoryRunContext] = []
    failures: list[RepositoryResolutionFailure] = []
    for rid, repo_settings in enabled_repos.items():
        try:
            repo_root_path = detect_git_repository_root(Path(repo_settings.path))
            repository_settings = [repo_settings]
            local_settings = _load_enabled_repository_local_settings(repo_root_path)
            if local_settings is not None:
                repository_settings.append(local_settings)
            contexts.append(
                _build_merged_repository_context(
                    global_config,
                    tuple(repository_settings),
                    fallback_repo_id=rid,
                    prefer_settings_id=False,
                )
            )
        except Exception as exc:  # noqa: BLE001 - isolate broken registry entries.
            logger.warning(
                "Skipping unresolvable repository '%s' (%s): %s",
                rid,
                repo_settings.path,
                exc,
            )
            failures.append(
                RepositoryResolutionFailure(
                    repo_id=rid,
                    display_name=repo_settings.display_name or rid,
                    configured_path=str(repo_settings.path),
                    error=str(exc),
                )
            )
    return contexts, failures


def _repository_settings_for_path(
    repo_root_path: Path,
) -> AgentRunnerRepositorySettings:
    local_settings = _load_enabled_repository_local_settings(repo_root_path)
    if local_settings is not None:
        return local_settings
    return AgentRunnerRepositorySettings(
        path=str(repo_root_path),
        id=repo_root_path.name,
        display_name=repo_root_path.name,
    )


def _load_enabled_repository_local_settings(
    repo_root_path: Path,
) -> AgentRunnerRepositorySettings | None:
    local_settings = load_agent_runner_local_settings(repo_root_path)
    if local_settings is not None and not local_settings.enabled:
        raise ValueError(
            "Repository-local config at "
            f"'{repo_root_path / IAR_REPOSITORY_CONFIG_FILENAME}' is disabled."
        )
    return local_settings


def _build_repository_context_from_settings(
    global_config: AppConfig,
    repo_settings: AgentRunnerRepositorySettings,
    *,
    fallback_repo_id: str,
    prefer_settings_id: bool,
) -> RepositoryRunContext:
    return _build_merged_repository_context(
        global_config,
        (repo_settings,),
        fallback_repo_id=fallback_repo_id,
        prefer_settings_id=prefer_settings_id,
    )


def _build_merged_repository_context(
    global_config: AppConfig,
    repository_settings: tuple[AgentRunnerRepositorySettings, ...],
    *,
    fallback_repo_id: str,
    prefer_settings_id: bool,
) -> RepositoryRunContext:
    effective_config = global_config
    effective_repo_id = fallback_repo_id
    effective_display_name = fallback_repo_id
    effective_repo_path = Path(".").resolve()

    for repo_settings in repository_settings:
        effective_config = merge_repository_config(effective_config, repo_settings)
        effective_repo_path = Path(repo_settings.path).resolve()
        if prefer_settings_id and repo_settings.id:
            effective_repo_id = repo_settings.id
        if repo_settings.display_name:
            effective_display_name = repo_settings.display_name

    return RepositoryRunContext(
        repo_id=effective_repo_id,
        display_name=effective_display_name,
        repo_path=effective_repo_path,
        config=effective_config,
    )


class SubprocessContentGenerator(IContentGenerator):
    """Generate content via a read-only local agent subprocess.

    Implements ``IContentGenerator`` via duck typing.
    """

    def __init__(
        self,
        process_runner: SubprocessRunner,
        *,
        read_only: bool = True,
    ) -> None:
        self._process_runner = process_runner
        self._read_only = read_only

    def generate(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        timeout: int | None = None,
    ) -> CommandResult:
        """Run a content generator and return its output.

        When the instance was constructed with ``read_only=True`` (the
        default) the agent runs in its read-only sandbox. The REPL
        entrypoint constructs the generator with ``read_only=False`` so
        the agent can mutate files inside the user's confirmation model.
        """
        command = _build_content_generation_command(
            agent_name, prompt, cwd, read_only=self._read_only
        )
        return self._process_runner.run(
            command, cwd=cwd, capture_output=True, timeout=timeout, check=False
        )


def _build_content_generation_command(
    agent_name: str,
    prompt: str,
    cwd: Path,
    *,
    read_only: bool = True,
) -> list[str]:
    """Build the agent command for content generation / REPL use.

    Args:
        agent_name: ``claude`` / ``codex`` / ``kimi`` (or any value that
            should fall back to ``claude``).
        prompt: Full prompt text passed to the agent.
        cwd: Working directory for the agent subprocess.
        read_only: When ``True`` (default), ``codex`` is invoked with
            ``--sandbox read-only --ask-for-approval never`` so it cannot
            modify the filesystem. When ``False`` (used by the REPL
            entrypoint), the sandbox flag is dropped so the agent is free
            to write files within the user's confirmation model. ``claude``
            and ``kimi`` commands are unaffected by this flag because they
            already have a single canonical invocation shape.

    Returns:
        Command argv ready to be handed to a process runner.
    """
    # codex / kimi 需显式指定；其余（"claude"、已解析的 "auto"、或任何未识别值）
    # 一律构造 claude 命令，绝不静默落到 codex。
    if agent_name == "codex":
        if read_only:
            return [
                "codex",
                "--cd",
                str(cwd),
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "exec",
                prompt,
            ]
        return [
            "codex",
            "--cd",
            str(cwd),
            "exec",
            prompt,
        ]
    if agent_name == "kimi":
        return ["kimi", "--prompt", prompt]
    return [
        "claude",
        "--dangerously-skip-permissions",
        "-p",
        prompt,
    ]


def _build_repl_command(agent_name: str, prompt: str, cwd: Path) -> list[str]:
    """Build the agent command used by the ``iar`` REPL entrypoint.

    Delegates to :func:`_build_content_generation_command` with
    ``read_only=False`` so that REPL-managed sessions do not run inside
    ``codex``'s read-only sandbox. The REPL's own command executor
    provides the safety boundary for arbitrary IAR subcommands.
    """
    return _build_content_generation_command(agent_name, prompt, cwd, read_only=False)


class SafePlannerContentGenerator(IContentGenerator):
    """Generate decision plans via a local agent subprocess.

    The planner delegates to the same agent command builders used for content
    generation.  Callers are responsible for validating and sandboxing the
    resulting plan; this runner does not enforce read-only execution.
    """

    def __init__(self, process_runner: SubprocessRunner) -> None:
        self._process_runner = process_runner

    def generate(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        timeout: int | None = None,
    ) -> CommandResult:
        """Run a planner agent and return its output."""
        command = _build_planner_command(agent_name, prompt, cwd)
        return self._process_runner.run(
            command, cwd=cwd, capture_output=True, timeout=timeout, check=False
        )


def _build_planner_command(agent_name: str, prompt: str, cwd: Path) -> list[str]:
    """Return a command for the given planner agent.

    The planner reuses the content-generation command builders so that all
    supported agents can act as planners.  Planner output is still expected
    to be a JSON DecisionPlan and is validated by the core use case.

    Raises:
        ValueError: If the agent is not one of the supported planner agents.
    """
    if agent_name not in ("claude", "codex", "kimi"):
        raise ValueError(
            f"Agent '{agent_name}' does not have a command builder "
            f"for interactive decision planning. Use 'claude', 'codex', or 'kimi'."
        )
    return _build_content_generation_command(agent_name, prompt, cwd)


def create_planner_runner(
    process_runner: SubprocessRunner | None = None,
) -> SafePlannerContentGenerator:
    """Create a safe planner runner instance."""
    return SafePlannerContentGenerator(process_runner or SubprocessRunner())


def create_content_generator(
    process_runner: SubprocessRunner | None = None,
    *,
    read_only: bool = True,
) -> SubprocessContentGenerator:
    """Create a content generator instance."""
    return SubprocessContentGenerator(
        process_runner or SubprocessRunner(), read_only=read_only
    )


def resolve_issue_from_prd_target(
    settings: AgentRunnerSettings,
    *,
    repo_id: str | None = None,
    repo_path_override: str | None = None,
    cwd: Path,
) -> RepositoryRunContext:
    """Resolve the single target repository for ``iar issue create``.

    Defaults to the current Git repository and its repository-local config.

    Args:
        settings: Agent runner settings.
        repo_id: Optional configured repository ID selector.
        repo_path_override: Optional ad-hoc repository path selector.
        cwd: Current working directory.

    Returns:
        A single repository run context.

    Raises:
        ValueError: If both ``repo_id`` and ``repo_path_override`` are provided,
            or if ``repo_id`` does not exist or is disabled.
    """
    contexts = resolve_repository_targets(
        settings,
        repo_id=repo_id,
        repo_path_override=repo_path_override,
        fallback_path=str(cwd),
    )
    return contexts[0]


def create_console_store() -> IRunHistoryStore:
    """创建管理终端的运行历史 / 审计 SQLite 存储。"""
    console_settings = get_agent_runner_settings().console
    return SqliteConsoleStore(console_settings.history_db_path)


def create_roadmap_store() -> IRoadmapStore:
    """创建 roadmap 队列 / 设置存储（与 console_store 共用同一个 SQLite 文件）。"""
    console_settings = get_agent_runner_settings().console
    return SqliteConsoleStore(console_settings.history_db_path)


def create_process_supervisor() -> IRunnerProcessSupervisor:
    """创建托管 runner 进程的监管器（pidfile + 日志目录已解析）。"""
    console_settings = get_agent_runner_settings().console
    log_dir = Path(console_settings.process_log_dir).expanduser()
    if not log_dir.is_absolute():
        log_dir = resolve_project_root_path() / log_dir
    return PidfileProcessSupervisor(
        registry_path=console_settings.process_registry_path,
        log_dir=log_dir,
    )


def create_registry_editor() -> IRepositoryRegistryEditor:
    """创建仓库 registry（config.toml）的受限写回编辑器。"""
    return TomlRegistryEditor(resolve_config_toml_path())


def resolve_console_spawn_cwd() -> Path:
    """托管进程的工作目录：keda 项目根（保证子进程读到正确配置）。"""
    return resolve_project_root_path()


def create_process_runner() -> SubprocessRunner:
    """Create a new subprocess runner instance."""
    return SubprocessRunner()


def build_deliberation_config_from_settings(
    agent_runner_settings: AgentRunnerSettings,
) -> DeliberationConfig:
    """Convert pydantic-settings deliberation config to frozen core config."""
    return _build_deliberation_config(agent_runner_settings.deliberation)


def create_event_sink(
    output_dir: Path,
    output_view: "IAgentOutputView | None" = None,
) -> "Callable[[DeliberationEvent], None]":
    """Create an event sink that writes to events.jsonl and shows a summary line.

    Args:
        output_dir: Directory where ``events.jsonl`` is appended.
        output_view: Optional live output view. When provided, the human
            readable summary line is routed through ``output_view.log`` so it
            does not corrupt an active live display; otherwise it is printed.
    """
    events_path = output_dir / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    import threading

    lock = threading.Lock()

    def _sink(event: "DeliberationEvent") -> None:
        line = json.dumps(
            {
                "session_id": event.session_id,
                "round": event.round,
                "agent": event.agent,
                "event_type": event.event_type,
                "message": event.message,
                "timestamp": event.timestamp,
            },
            ensure_ascii=False,
        )
        with lock:
            with open(events_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        summary = (
            f"[{event.session_id}] round={event.round} agent={event.agent} "
            f"event={event.event_type}"
        )
        if output_view is not None:
            output_view.log(summary)
        else:
            print(summary)

    return _sink


def create_github_client(
    repo_path: Path, process_runner: SubprocessRunner | None = None
) -> GitHubCliClient:
    """Create a new GitHub CLI client instance."""
    return GitHubCliClient(repo_path, process_runner)


def create_repl_command_executor(
    process_runner: SubprocessRunner | None = None,
    config: ReplConfig | None = None,
):
    """Create a :class:`ReplCommandExecutor` for the REPL entrypoint.

    Imports the executor lazily to avoid a hard dependency from this
    module's import time. ``config`` defaults to the merged global
    ReplConfig (``config.agent_runner.repl`` translated via
    ``build_app_config``).
    """
    from backend.engines.agent_runner.repl_command_executor import (
        ReplCommandExecutor,
    )

    if config is None:
        config = build_app_config().repl
    return ReplCommandExecutor(
        process_runner=process_runner or SubprocessRunner(), config=config
    )


def create_loop_state_store(state_path: Path | None = None) -> JsonLoopStateStore:
    """Create a JSON-backed loop state store.

    Args:
        state_path: Optional override for the on-disk JSON path. Defaults
            to ``~/.iar/loop-state.json``.

    Returns:
        A :class:`JsonLoopStateStore` instance.
    """
    return JsonLoopStateStore(state_path or resolve_loop_state_path())


def create_loop_clock() -> SystemClock:
    """Create the production wall-clock implementation for the loop daemon."""
    return SystemClock()
