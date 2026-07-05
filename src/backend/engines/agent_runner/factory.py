"""Agent Runner infrastructure adapter and factory.

This module is a thin backward-compatible facade over the line-split
factory sub-modules:

- :mod:`backend.engines.agent_runner.factory_config_builder` — pydantic
  settings → frozen ``AppConfig`` conversion.
- :mod:`backend.engines.agent_runner.factory_config_merge` — repository
  override merge helpers and :func:`merge_repository_config`.
- :mod:`backend.engines.agent_runner.factory_repository_resolver` —
  :func:`resolve_repository_targets`, ``_RepositoryMatchResult``, and the
  supporting context construction helpers.
- :mod:`backend.engines.agent_runner.factories` — ``create_*`` /
  ``resolve_*`` entrypoints that bridge ``core/`` to ``infrastructure/``.

The legacy public symbols (``build_app_config``,
``merge_repository_config``, ``create_github_client``, …) remain
importable from this module so callers do not need to migrate their
``from backend.engines.agent_runner.factory import …`` lines.
"""

from __future__ import annotations

from backend.engines.agent_runner.factory_config_builder import (
    _build_generated_content_config,
    _build_generated_content_target_config,
    _build_deliberation_config,
    _build_memory_config,
    _build_repl_config,
    build_app_config_from_settings,
)
from backend.engines.agent_runner.factory_config_merge import (
    _merge_label_config,
    _merge_prompt_config,
    _merge_optional_model,
    merge_repository_config,
)
from backend.engines.agent_runner.factory_repository_resolver import (
    RepositoryResolutionFailure,
    _anchor_memory_config,
    _build_merged_repository_context,
    _build_repository_context_from_settings,
    _load_enabled_repository_local_settings,
    _merge_repositories_dict,
    _repository_identity_key,
    _repository_settings_for_path,
    _resolve_anchor_path,
    find_repository_match_for_path,
    resolve_repository_targets,
    resolve_repository_targets_with_diagnostics,
)
from backend.engines.agent_runner.factories import (
    build_app_config,
    build_deliberation_config_from_settings,
    create_content_generator,
    create_console_store,
    create_event_sink,
    create_github_client,
    create_loop_clock,
    create_loop_state_store,
    create_planner_runner,
    create_process_runner,
    create_process_supervisor,
    create_registry_editor,
    create_repl_command_executor,
    create_roadmap_store,
    create_transcript_runner,
    get_agent_runner_settings,
    get_agent_runner_status_data,
    load_fresh_agent_runner_settings,
    logger,
    resolve_console_spawn_cwd,
    resolve_issue_from_prd_target,
    write_deliberation_outputs,
)
from backend.engines.agent_runner.factories.content_generators import (
    _build_content_generation_command,
    _build_planner_command,
    _build_repl_command,
    SafePlannerContentGenerator,
    SubprocessContentGenerator,
)
from backend.infrastructure.config.settings import (
    resolve_project_root_path,
    resolve_registry_config_toml_path,
)

__all__ = [
    "RepositoryResolutionFailure",
    "SafePlannerContentGenerator",
    "SubprocessContentGenerator",
    "_anchor_memory_config",
    "_build_content_generation_command",
    "_build_generated_content_config",
    "_build_generated_content_target_config",
    "_build_deliberation_config",
    "_build_memory_config",
    "_build_merged_repository_context",
    "_build_planner_command",
    "_build_repl_config",
    "_build_repl_command",
    "_build_repository_context_from_settings",
    "_load_enabled_repository_local_settings",
    "_merge_label_config",
    "_merge_optional_model",
    "_merge_prompt_config",
    "_merge_repositories_dict",
    "_repository_identity_key",
    "_repository_settings_for_path",
    "_resolve_anchor_path",
    "build_app_config",
    "build_app_config_from_settings",
    "build_deliberation_config_from_settings",
    "create_content_generator",
    "create_console_store",
    "create_event_sink",
    "create_github_client",
    "create_loop_clock",
    "create_loop_state_store",
    "create_planner_runner",
    "create_process_runner",
    "create_process_supervisor",
    "create_registry_editor",
    "create_repl_command_executor",
    "create_roadmap_store",
    "create_transcript_runner",
    "find_repository_match_for_path",
    "get_agent_runner_settings",
    "get_agent_runner_status_data",
    "load_fresh_agent_runner_settings",
    "logger",
    "merge_repository_config",
    "resolve_console_spawn_cwd",
    "resolve_issue_from_prd_target",
    "resolve_project_root_path",
    "resolve_registry_config_toml_path",
    "resolve_repository_targets",
    "resolve_repository_targets_with_diagnostics",
    "write_deliberation_outputs",
]
