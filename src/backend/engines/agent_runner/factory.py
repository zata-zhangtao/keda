"""Agent Runner infrastructure adapter and factory.

This module bridges the ``core/`` business layer with ``infrastructure/``
implementations by instantiating concrete clients/runners and converting
pydantic-settings configuration into the frozen dataclasses expected by use
cases.
"""

from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from backend.core.shared.interfaces.agent_runner import IContentGenerator
from backend.core.shared.models.agent_deliberation import (
    DeliberationAgentProfile,
    DeliberationConfig,
    DeliberationEvent,
    DeliberationResult,
    DeliberationSession,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    GeneratedContentConfig,
    GeneratedContentTargetConfig,
    GitConfig,
    LabelConfig,
    PostPrSupervisorConfig,
    PrePushReviewConfig,
    PromptConfig,
    RepositoryRunContext,
    RunnerConfig,
    SafetyConfig,
    WorktreeConfig,
)
from backend.engines.agent_runner.repository_local import detect_git_repository_root
from backend.infrastructure.config.settings import (
    AgentRunnerGeneratedContentSettings,
    AgentRunnerGeneratedContentTargetSettings,
    AgentRunnerLabelSettings,
    AgentRunnerPromptSettings,
    AgentRunnerRepositorySettings,
    AgentRunnerSettings,
    IAR_REPOSITORY_CONFIG_FILENAME,
    config,
    load_agent_runner_local_settings,
)
from backend.infrastructure.github_client import GitHubCliClient
from backend.infrastructure.logging.logger import logger
from backend.infrastructure.process_runner import (
    SubprocessRunner,
    _format_timestamped_line,
    run_filtered_claude_stream,
    should_filter_claude_stream,
)

__all__ = [
    "logger",
    "build_deliberation_config_from_settings",
    "create_content_generator",
    "create_event_sink",
    "create_github_client",
    "create_process_runner",
    "create_transcript_runner",
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
    prompt_settings = agent_runner_settings.prompts

    pre_push = agent_runner_settings.pre_push_review
    post_supervisor = agent_runner_settings.post_pr_supervisor
    generated_content = _build_generated_content_config(
        agent_runner_settings.generated_content
    )

    return AppConfig(
        labels=LabelConfig(
            ready=label_settings.ready,
            running=label_settings.running,
            supervising=label_settings.supervising,
            review=label_settings.review,
            failed=label_settings.failed,
            blocked=label_settings.blocked,
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
            default_agent=runner_settings.default_agent,
            max_recovery_attempts=runner_settings.max_recovery_attempts,
            recovery_retry_delay_seconds=runner_settings.recovery_retry_delay_seconds,
            verification_commands=tuple(runner_settings.verification_commands),
        ),
        safety=SafetyConfig(
            auto_merge=safety_settings.auto_merge,
            forbidden_path_patterns=tuple(safety_settings.forbidden_path_patterns),
        ),
        prompts=PromptConfig(
            default_phase=prompt_settings.default_phase,
            phases=dict(prompt_settings.phases),
        ),
        pre_push_review=PrePushReviewConfig(
            enabled=pre_push.enabled,
            review_agent=pre_push.review_agent,
            allow_same_agent=pre_push.allow_same_agent,
            max_attempts=pre_push.max_attempts,
        ),
        post_pr_supervisor=PostPrSupervisorConfig(
            enabled=post_supervisor.enabled,
            supervisor_agent=post_supervisor.supervisor_agent,
            max_repair_attempts=post_supervisor.max_repair_attempts,
        ),
        generated_content=generated_content,
    )


def build_app_config() -> AppConfig:
    """Convert global pydantic-settings ``AgentRunnerSettings`` to frozen ``AppConfig``."""
    return build_app_config_from_settings(config.agent_runner)


def get_agent_runner_settings() -> AgentRunnerSettings:
    """Return the global ``AgentRunnerSettings`` instance."""
    return config.agent_runner


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
            "pre_push_review_enabled": app_config.pre_push_review.enabled,
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
    prompts = _merge_prompt_config(global_config.prompts, repo_settings.prompts)
    pre_push_review = _merge_optional_model(
        global_config.pre_push_review, repo_settings.pre_push_review
    )
    post_pr_supervisor = _merge_optional_model(
        global_config.post_pr_supervisor, repo_settings.post_pr_supervisor
    )
    generated_content = _merge_generated_content_config(
        global_config.generated_content, repo_settings.generated_content
    )
    return AppConfig(
        labels=labels,
        git=git,
        worktree=worktree,
        runner=runner,
        safety=safety,
        prompts=prompts,
        pre_push_review=pre_push_review,
        post_pr_supervisor=post_pr_supervisor,
        generated_content=generated_content,
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
        """Run a read-only content generator and return its output."""
        command = _build_content_generation_command(agent_name, prompt, cwd)
        return self._process_runner.run(
            command, cwd=cwd, capture_output=True, timeout=timeout, check=False
        )


def _build_content_generation_command(
    agent_name: str, prompt: str, cwd: Path
) -> list[str]:
    if agent_name == "claude":
        return [
            "claude",
            "--dangerously-skip-permissions",
            "-p",
            prompt,
        ]
    if agent_name == "kimi":
        return ["kimi", "--prompt", prompt]
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


def create_content_generator(
    process_runner: SubprocessRunner | None = None,
) -> SubprocessContentGenerator:
    """Create a content generator instance."""
    return SubprocessContentGenerator(process_runner or SubprocessRunner())


def resolve_issue_from_prd_target(
    settings: AgentRunnerSettings,
    *,
    repo_id: str | None = None,
    repo_path_override: str | None = None,
    cwd: Path,
) -> RepositoryRunContext:
    """Resolve the single target repository for ``issue-from-prd``.

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


def create_process_runner() -> SubprocessRunner:
    """Create a new subprocess runner instance."""
    return SubprocessRunner()


def build_deliberation_config_from_settings(
    agent_runner_settings: AgentRunnerSettings,
) -> DeliberationConfig:
    """Convert pydantic-settings deliberation config to frozen core config."""
    deliberation_settings = agent_runner_settings.deliberation
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
        profiles=profiles,
    )


class SubprocessTranscriptRunner:
    """Run agents and emit deliberation events.

    Implements ``IAgentTranscriptRunner`` via duck typing.
    """

    def __init__(self, process_runner: SubprocessRunner) -> None:
        self._process_runner = process_runner

    def run(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        event_sink: "Callable[[DeliberationEvent], None]",
        output_sink: "Callable[[str], None] | None" = None,
    ) -> "CommandResult":
        """Run an agent and emit events.

        Streams agent stdout to the terminal in real time while
        collecting it for the deliberation transcript. When
        ``output_sink`` is provided, rendered text chunks are passed
        to it as they arrive.
        """
        command = _build_deliberation_command(agent_name, prompt, cwd)
        _ = event_sink
        if should_filter_claude_stream(command):
            # Pass the prompt via stdin to avoid "Argument list too long"
            # when the transcript grows across rounds.
            command_no_prompt = [arg for arg in command if arg != "-p"]
            if command_no_prompt and command_no_prompt[-1] == prompt:
                command_no_prompt = command_no_prompt[:-1]
            completed = run_filtered_claude_stream(
                command_no_prompt,
                cwd=cwd,
                timeout=None,
                collect_stdout=True,
                prompt_text=prompt,
                output_sink=output_sink,
            )
            return CommandResult(
                command=tuple(command_no_prompt),
                return_code=completed.returncode,
                stdout=completed.stdout,
                stderr="",
            )
        if agent_name in ("kimi", "codex"):
            # Pass the prompt via stdin to avoid "Argument list too long"
            # when the transcript grows across rounds.
            return _run_agent_with_stdin_prompt(
                command, prompt, cwd, output_sink=output_sink
            )
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=None,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        return_code, stdout_text = _relay_process_stdout(
            process, output_sink=output_sink
        )
        return CommandResult(
            command=tuple(command),
            return_code=return_code,
            stdout=stdout_text,
            stderr="",
        )


def _run_agent_with_stdin_prompt(
    command: list[str],
    prompt: str,
    cwd: Path,
    output_sink: "Callable[[str], None] | None" = None,
) -> CommandResult:
    """Run an agent subprocess, passing the prompt via stdin."""
    import threading

    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=None,
        stdin=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )

    def _write_stdin() -> None:
        if process.stdin is not None:
            try:
                process.stdin.write(prompt)
            except BrokenPipeError:
                pass
            process.stdin.close()

    threading.Thread(target=_write_stdin, daemon=True).start()
    return_code, stdout_text = _relay_process_stdout(
        process, output_sink=output_sink
    )
    return CommandResult(
        command=tuple(command),
        return_code=return_code,
        stdout=stdout_text,
        stderr="",
    )


def _relay_process_stdout(
    process: subprocess.Popen[str],
    output_sink: "Callable[[str], None] | None" = None,
) -> tuple[int, str]:
    """Relay subprocess stdout to terminal and logger."""
    stdout_lines: list[str] = []
    try:
        if process.stdout is not None:
            for line in process.stdout:
                stdout_lines.append(line)
                logger.info("%s", line.rstrip("\n"))
                if output_sink is not None:
                    output_sink(line.rstrip("\n"))
                else:
                    timestamped = _format_timestamped_line(line)
                    print(timestamped, end="")
        return_code = process.wait(timeout=None)
    except Exception:
        process.kill()
        process.wait()
        raise
    return return_code, "".join(stdout_lines)


def _build_deliberation_command(agent_name: str, prompt: str, cwd: Path) -> list[str]:
    if agent_name == "claude":
        return [
            "claude",
            "--dangerously-skip-permissions",
            "--verbose",
            "-p",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            prompt,
        ]
    if agent_name == "kimi":
        return ["kimi", "--quiet", "--input-format", "text"]
    return [
        "codex",
        "--cd",
        str(cwd.resolve()),
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "exec",
    ]


def create_transcript_runner(
    process_runner: SubprocessRunner | None = None,
) -> SubprocessTranscriptRunner:
    """Create a transcript runner instance."""
    return SubprocessTranscriptRunner(process_runner or SubprocessRunner())


def create_event_sink(
    output_dir: Path,
) -> "Callable[[DeliberationEvent], None]":
    """Create an event sink that writes to events.jsonl and prints to terminal."""
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
        print(
            f"[{event.session_id}] round={event.round} agent={event.agent} "
            f"event={event.event_type}"
        )

    return _sink


def write_deliberation_outputs(
    result: "DeliberationResult",
    session: "DeliberationSession",
    output_dir: Path,
) -> None:
    """Write transcript.md, result.md, and session.json."""
    import json

    # transcript.md
    transcript_path = output_dir / "transcript.md"
    transcript_lines = [
        "# Deliberation Transcript",
        "",
        "## Session",
        "",
        f"- Session ID: `{session.session_id}`",
        f"- Prompt: {session.prompt}",
        f"- Agents: {', '.join(p.profile_id for p in session.profiles)}",
        "",
    ]
    for round_key, outputs in result.agent_outputs.items():
        transcript_lines.append(f"## {round_key}")
        transcript_lines.append("")
        for profile_id, output in outputs.items():
            transcript_lines.append(f"### {profile_id}")
            transcript_lines.append("")
            transcript_lines.append(output)
            transcript_lines.append("")
    transcript_path.write_text("\n".join(transcript_lines), encoding="utf-8")

    # result.md
    result_path = output_dir / "result.md"
    result_lines = [
        "# Deliberation Result",
        "",
        "## Recommendation",
        "",
        result.recommendation,
        "",
        "## Consensus",
        "",
        result.consensus,
        "",
        "## Disagreements",
        "",
        result.disagreements,
        "",
        "## Risks",
        "",
        result.risks,
        "",
        "## Next Actions",
        "",
        result.next_actions,
        "",
    ]
    result_path.write_text("\n".join(result_lines), encoding="utf-8")

    # session.json
    session_path = output_dir / "session.json"
    session_data = {
        "session_id": session.session_id,
        "prompt": session.prompt,
        "profiles": [
            {
                "profile_id": p.profile_id,
                "agent": p.agent,
                "role": p.role,
            }
            for p in session.profiles
        ],
        "rounds": session.rounds,
        "synthesizer": session.synthesizer,
        "output_dir": str(session.output_dir),
        "started_at": session.started_at,
        "finished_at": session.finished_at,
    }
    session_path.write_text(
        json.dumps(session_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def create_github_client(
    repo_path: Path, process_runner: SubprocessRunner | None = None
) -> GitHubCliClient:
    """Create a new GitHub CLI client instance."""
    return GitHubCliClient(repo_path, process_runner)
