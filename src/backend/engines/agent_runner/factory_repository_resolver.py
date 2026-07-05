"""Agent Runner repository resolution.

Holds :func:`resolve_repository_targets`,
:func:`resolve_repository_targets_with_diagnostics`, and
:func:`find_repository_match_for_path` plus the supporting helpers used
by the consolidated multi-repo flows. These functions were originally
part of :mod:`backend.engines.agent_runner.factory`.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from backend.core.shared.models.agent_runner import (
    AppConfig,
    MemoryConfig,
    RepositoryIdentity,
    RepositoryRunContext,
)
from backend.engines.agent_runner.factory_config_builder import (
    build_app_config_from_settings,
)
from backend.engines.agent_runner.factory_config_merge import merge_repository_config
from backend.engines.agent_runner.repository_local import detect_git_repository_root
from backend.infrastructure.config.settings import (
    AgentRunnerRepositorySettings,
    AgentRunnerSettings,
    IAR_REPOSITORY_CONFIG_FILENAME,
    load_agent_runner_local_settings,
)
from backend.infrastructure.logging.logger import logger


def _repository_identity_key(repo_settings: AgentRunnerRepositorySettings) -> str:
    """Choose the dict key for a repository under ``AppConfig.repositories``.

    The registry key (when present) takes precedence so that
    ``iar issue list --repo-id foo`` and ``--all-registered`` produce the
    same identity lookup; ad-hoc single-repo flows that only set ``path``
    fall back to the resolved path string.
    """
    if repo_settings.id:
        return repo_settings.id
    return str(Path(repo_settings.path).resolve())


def _merge_repositories_dict(
    base: dict[str, RepositoryIdentity],
    repo_settings: AgentRunnerRepositorySettings,
    *,
    key_override: str | None = None,
) -> dict[str, RepositoryIdentity]:
    """Merge a Pydantic repository entry into the ``repositories`` dict view.

    The base dict is left untouched; a new dict is returned with the
    identity for ``repo_settings`` (converted at the engines/ boundary
    so core/ never sees the Pydantic type) keyed by ``key_override``
    when supplied, otherwise by ``id`` when set, else by resolved path.

    ``key_override`` lets the caller force the registry key
    (e.g. ``"keda-main"``) so that ``_repo_label_for`` can look up
    the identity by ``context.repo_id``.
    """
    if key_override is not None:
        key = key_override
    else:
        key = _repository_identity_key(repo_settings)
    identity = RepositoryIdentity(
        id=repo_settings.id,
        path=str(repo_settings.path),
        display_name=repo_settings.display_name,
        github_repo=repo_settings.github_repo,
    )
    merged = dict(base)
    merged[key] = identity
    return merged


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
                registry_key=repo_id,
            )
        ]

    if all_repositories:
        enabled_repos = {rid: rcfg for rid, rcfg in settings.repositories.items() if rcfg.enabled}
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
                    registry_key=rid,
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
    enabled_repos = {rid: rcfg for rid, rcfg in settings.repositories.items() if rcfg.enabled}
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
                    registry_key=rid,
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


def _anchor_memory_config(
    memory: MemoryConfig,
    repo_root_path: Path,
) -> MemoryConfig:
    """把 ``MemoryConfig`` 中的相对路径解析到 ``repo_root_path`` 下。

    由 :func:`_build_merged_repository_context` 在构造
    :class:`RepositoryRunContext` 前调用，确保任何运行时会按"主仓库
    根"解析相对路径，并把``~`` 展开为绝对路径。

    语义：

    - 先对每个字符串调用 :meth:`Path.expanduser`（处理 ``~``）。
    - 剩余为相对路径者，与 ``repo_root_path`` 拼接形成绝对路径。
    - 已经是绝对路径（含 ``expanduser`` 产物）的字符串原样保留。

    ``MemoryConfig`` 是 frozen dataclass，构造替换通过
    :func:`dataclasses.replace` 完成。
    """
    resolved_base = _resolve_anchor_path(memory.base_dir, repo_root_path)
    resolved_drafts = _resolve_anchor_path(memory.skill_drafts_dir, repo_root_path)
    resolved_promoted = tuple(
        _resolve_anchor_path(directory, repo_root_path) for directory in memory.promoted_skills_dirs
    )
    return dataclasses.replace(
        memory,
        base_dir=resolved_base,
        skill_drafts_dir=resolved_drafts,
        promoted_skills_dirs=resolved_promoted,
    )


def _resolve_anchor_path(raw_path: str, repo_root_path: Path) -> str:
    """Expand ``~`` and resolve relative paths against ``repo_root_path``."""

    expanded = Path(str(raw_path)).expanduser()
    if expanded.is_absolute():
        return str(expanded)
    return str(repo_root_path / expanded)


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
    registry_key: str | None = None,
) -> RepositoryRunContext:
    effective_config = global_config
    effective_repo_id = fallback_repo_id
    effective_display_name = fallback_repo_id
    effective_repo_path = Path(".").resolve()

    for index, repo_settings in enumerate(repository_settings):
        # The first settings entry is the registry entry whose dict key
        # is the caller-supplied ``registry_key`` (typically the
        # ``AgentRunnerSettings.repositories`` key). For ad-hoc cwd
        # paths the caller passes no ``registry_key``; we still want
        # the identity view to be populated using the local entry's
        # own ``id``/path-derived key.
        if index == 0:
            repo_key = registry_key
            skip_identity = False
        else:
            # Subsequent entries (local .iar.toml overrides) merge
            # sub-config fields but do not touch the identity view.
            repo_key = None
            skip_identity = True
        effective_config = merge_repository_config(
            effective_config,
            repo_settings,
            repo_key=repo_key,
            skip_identity=skip_identity,
        )
        effective_repo_path = Path(repo_settings.path).resolve()
        if prefer_settings_id and repo_settings.id:
            effective_repo_id = repo_settings.id
        if repo_settings.display_name:
            effective_display_name = repo_settings.display_name

    anchored_memory = _anchor_memory_config(effective_config.memory, effective_repo_path)
    effective_config = dataclasses.replace(effective_config, memory=anchored_memory)

    return RepositoryRunContext(
        repo_id=effective_repo_id,
        display_name=effective_display_name,
        repo_path=effective_repo_path,
        config=effective_config,
    )


__all__ = [
    "RepositoryResolutionFailure",
    "find_repository_match_for_path",
    "resolve_repository_targets",
    "resolve_repository_targets_with_diagnostics",
]
