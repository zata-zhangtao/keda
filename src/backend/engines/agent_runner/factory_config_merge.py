"""Agent Runner config-merge helpers.

Holds the ``_merge_*`` helpers and :func:`merge_repository_config` that
fuse a repository-specific override into a global :class:`AppConfig`.
Extracted out of :mod:`backend.engines.agent_runner.factory` so each
helper module stays under the file-line ceiling.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from pydantic import BaseModel

from backend.core.shared.models.agent_deliberation import (
    DeliberationAgentProfile,
    DeliberationConfig,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    GeneratedContentConfig,
    GeneratedContentTargetConfig,
    LabelConfig,
    PromptConfig,
    RepositoryIdentity,
)
from backend.infrastructure.config.settings import (
    AgentRunnerDeliberationSettings,
    AgentRunnerGeneratedContentSettings,
    AgentRunnerGeneratedContentTargetSettings,
    AgentRunnerLabelSettings,
    AgentRunnerPromptSettings,
    AgentRunnerRepositorySettings,
)


def _model_to_dict(model: object) -> dict:
    """Convert a pydantic BaseModel or dataclass to a plain dict."""
    if isinstance(model, BaseModel):
        return model.model_dump()
    return dataclasses.asdict(model)


def _pydantic_override_dict(override_model: BaseModel) -> dict:
    """Return only explicitly-set fields from a pydantic override model."""
    return {
        k: v for k, v in override_model.model_dump().items() if k in override_model.model_fields_set
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
        validation_pending=override_data.get("validation_pending", base_config.validation_pending),
        validation_passed=override_data.get("validation_passed", base_config.validation_passed),
        verifier_passed=override_data.get("verifier_passed", base_config.verifier_passed),
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


def _drop_empty_template_overrides(
    override_data: dict[str, object],
) -> dict[str, object]:
    """Drop empty string template/prompt overrides so base config defaults survive.

    ``.iar.toml`` often materializes ``title_template = ""``, ``body_template = ""``
    and ``prompt = ""`` as placeholders. Without this filter, those empty strings
    would wipe out meaningful defaults from ``config.toml``.
    """
    filtered = dict(override_data)
    for key in ("title_template", "body_template", "prompt"):
        if filtered.get(key) == "":
            del filtered[key]
    return filtered


def _merge_generated_content_target_config(
    base_config: GeneratedContentTargetConfig,
    override: AgentRunnerGeneratedContentTargetSettings | None,
) -> GeneratedContentTargetConfig:
    """Merge repository-specific generated-content target overrides."""
    if override is None:
        return base_config
    override_data = _drop_empty_template_overrides(_pydantic_override_dict(override))
    return GeneratedContentTargetConfig(
        enabled=override_data.get("enabled", base_config.enabled),
        mode=override_data.get("mode", base_config.mode),
        output=override_data.get("output", base_config.output),
        title_template=override_data.get("title_template", base_config.title_template),
        body_template=override_data.get("body_template", base_config.body_template),
        agent=override_data.get("agent", base_config.agent),
        timeout_seconds=override_data.get("timeout_seconds", base_config.timeout_seconds),
        prompt=override_data.get("prompt", base_config.prompt),
        include_commit_log=override_data.get("include_commit_log", base_config.include_commit_log),
        include_diff_stat=override_data.get("include_diff_stat", base_config.include_diff_stat),
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
        max_input_chars=override_data.get("max_input_chars", base_config.max_input_chars),
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
        default_output_dir=override_data.get("default_output_dir", base_config.default_output_dir),
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
    global_config: AppConfig,
    repo_settings: AgentRunnerRepositorySettings,
    *,
    repo_key: str | None = None,
    skip_identity: bool = False,
) -> AppConfig:
    """Merge repository-specific overrides into a global ``AppConfig``.

    Args:
        global_config: The global application configuration.
        repo_settings: Repository-specific override settings.
        repo_key: Optional explicit key for the ``repositories`` dict
            view. Defaults to ``repo_settings.id`` (if set) or the
            resolved path; the caller is expected to pass the registry
            key (``settings.repositories[<key>]``) for registry paths
            so that ``_repo_label_for`` can look up the identity via
            ``context.repo_id``.
        skip_identity: When ``True`` the caller's identity view is left
            untouched. Used for local ``.iar.toml`` overrides whose
            ``github_repo`` should not clobber the registry value.

    Returns:
        A new ``AppConfig`` with per-repository overrides applied.
    """
    labels = _merge_label_config(global_config.labels, repo_settings.labels)
    git = _merge_optional_model(global_config.git, repo_settings.git)
    worktree = _merge_optional_model(global_config.worktree, repo_settings.worktree)
    runner = _merge_optional_model(global_config.runner, repo_settings.runner)
    memory = _merge_optional_model(global_config.memory, repo_settings.memory)
    safety = _merge_optional_model(global_config.safety, repo_settings.safety)
    autopilot = _merge_optional_model(global_config.autopilot, repo_settings.autopilot)
    validation = _merge_optional_model(global_config.validation, repo_settings.validation)
    prompts = _merge_prompt_config(global_config.prompts, repo_settings.prompts)
    pre_pr_review = _merge_optional_model(global_config.pre_pr_review, repo_settings.pre_pr_review)
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
    repositories = (
        global_config.repositories
        if skip_identity
        else _merge_repositories_dict_minimal(
            global_config.repositories, repo_settings, key_override=repo_key
        )
    )
    return AppConfig(
        labels=labels,
        git=git,
        worktree=worktree,
        runner=runner,
        memory=memory,
        safety=safety,
        autopilot=autopilot,
        validation=validation,
        prompts=prompts,
        pre_pr_review=pre_pr_review,
        post_pr_supervisor=post_pr_supervisor,
        generated_content=generated_content,
        interactive_decision=interactive_decision,
        repl=repl,
        deliberation=deliberation,
        repositories=repositories,
    )


def _merge_repositories_dict_minimal(
    base: dict[str, RepositoryIdentity],
    repo_settings: AgentRunnerRepositorySettings,
    *,
    key_override: str | None = None,
) -> dict[str, RepositoryIdentity]:
    """Identity-only variant of ``_merge_repositories_dict`` used by the merge step.

    Kept here so :func:`merge_repository_config` does not depend on
    :mod:`backend.engines.agent_runner.factory_repository_resolver`'s
    identity-key resolution; the heavier registry matching lives there.
    """
    if key_override is not None:
        key = key_override
    elif repo_settings.id:
        key = repo_settings.id
    else:
        key = str(Path(repo_settings.path).resolve())
    identity = RepositoryIdentity(
        id=repo_settings.id,
        path=str(repo_settings.path),
        display_name=repo_settings.display_name,
        github_repo=repo_settings.github_repo,
    )
    merged = dict(base)
    merged[key] = identity
    return merged


__all__ = ["merge_repository_config"]
