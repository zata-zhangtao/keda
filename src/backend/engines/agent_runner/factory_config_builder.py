"""Agent Runner config builders.

Module-level functions that convert pydantic-settings models from
:mod:`backend.infrastructure.config.settings` into the frozen dataclasses
defined in :mod:`backend.core.shared.models.agent_runner`. Split out of
:mod:`backend.engines.agent_runner.factory` so the factories sub-package
can stay focused on object construction.
"""

from __future__ import annotations

from backend.core.shared.models.agent_deliberation import (
    DeliberationAgentProfile,
    DeliberationConfig,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    AutopilotConfig,
    GeneratedContentConfig,
    GeneratedContentTargetConfig,
    GitConfig,
    LabelConfig,
    MemoryConfig,
    PostPrSupervisorConfig,
    PrePrReviewConfig,
    PromptConfig,
    RunnerConfig,
    SafetyConfig,
    ValidationConfig,
    WorktreeConfig,
)
from backend.infrastructure.config.settings import (
    AgentRunnerDeliberationSettings,
    AgentRunnerGeneratedContentSettings,
    AgentRunnerGeneratedContentTargetSettings,
    AgentRunnerMemorySettings,
    AgentRunnerReplSettings,
    AgentRunnerSettings,
)
from backend.core.shared.models.agent_decision import (
    InteractiveDecisionConfig,
    ReplConfig,
)


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
        issue_from_prd=_build_generated_content_target_config(gc_settings.issue_from_prd),
        draft_pr=_build_generated_content_target_config(gc_settings.draft_pr),
        prd_from_issue=_build_generated_content_target_config(gc_settings.prd_from_issue),
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


def _build_memory_config(
    memory_settings: AgentRunnerMemorySettings,
) -> MemoryConfig:
    """Convert pydantic memory settings to frozen core config."""
    return MemoryConfig(
        enabled=memory_settings.enabled,
        base_dir=memory_settings.base_dir,
        skill_drafts_dir=memory_settings.skill_drafts_dir,
        promoted_skills_dirs=tuple(memory_settings.promoted_skills_dirs),
        top_k_skills=memory_settings.top_k_skills,
        top_k_facts=memory_settings.top_k_facts,
        auto_promote=memory_settings.auto_promote,
        auto_promote_threshold=memory_settings.auto_promote_threshold,
        auto_promote_min_success_rate=(memory_settings.auto_promote_min_success_rate),
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
    memory_settings = agent_runner_settings.memory
    safety_settings = agent_runner_settings.safety
    autopilot_settings = agent_runner_settings.autopilot
    validation_settings = agent_runner_settings.validation
    prompt_settings = agent_runner_settings.prompts

    pre_pr = agent_runner_settings.pre_pr_review
    post_supervisor = agent_runner_settings.post_pr_supervisor
    generated_content = _build_generated_content_config(agent_runner_settings.generated_content)
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
            verifier_passed=label_settings.verifier_passed,
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
            fix_agent_enabled=runner_settings.fix_agent_enabled,
            fix_timeout_seconds=runner_settings.fix_timeout_seconds,
            recovery_timeout_seconds=runner_settings.recovery_timeout_seconds,
            inactivity_timeout_seconds=runner_settings.inactivity_timeout_seconds,
            verification_commands=tuple(runner_settings.verification_commands),
            pre_commit_verification_command=runner_settings.pre_commit_verification_command,
        ),
        memory=_build_memory_config(memory_settings),
        safety=SafetyConfig(
            auto_merge=safety_settings.auto_merge,
            forbidden_path_patterns=tuple(safety_settings.forbidden_path_patterns),
        ),
        autopilot=AutopilotConfig(
            enabled=autopilot_settings.enabled,
            merge_method=autopilot_settings.merge_method,
            require_verifier_pass=autopilot_settings.require_verifier_pass,
            auto_sign_off=autopilot_settings.auto_sign_off,
            merge_check_timeout_seconds=autopilot_settings.merge_check_timeout_seconds,
        ),
        validation=ValidationConfig(
            enabled=validation_settings.enabled,
            evidence_dir=validation_settings.evidence_dir,
            branch_prefix=validation_settings.branch_prefix,
            evidence_format_check=validation_settings.evidence_format_check,
            parse_evidence_format_with_agent=validation_settings.parse_evidence_format_with_agent,
            language=validation_settings.language,
            structured_evidence=validation_settings.structured_evidence,
            require_negative_control=validation_settings.require_negative_control,
            reexecute_commands=validation_settings.reexecute_commands,
            reexecute_timeout_seconds=validation_settings.reexecute_timeout_seconds,
            reexecute_cache_enabled=validation_settings.reexecute_cache_enabled,
            verifier_enabled=validation_settings.verifier_enabled,
            verifier_agent=validation_settings.verifier_agent,
            verifier_timeout_seconds=validation_settings.verifier_timeout_seconds,
            artifact_health_enabled=validation_settings.artifact_health_enabled,
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
            crash_retry_max_backoff_seconds=(post_supervisor.crash_retry_max_backoff_seconds),
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


__all__ = [
    "build_app_config_from_settings",
]
