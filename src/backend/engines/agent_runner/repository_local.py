"""Repository-local configuration helpers for issue-agent-runner."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

from backend.infrastructure.config.settings import (
    AgentRunnerGeneratedContentSettings,
    AgentRunnerGitSettings,
    AgentRunnerLocalSettings,
    AgentRunnerPrePushReviewSettings,
    AgentRunnerPromptSettings,
    AgentRunnerRepositoryMetadataSettings,
    AgentRunnerRunnerSettings,
    AgentRunnerSafetySettings,
    AgentRunnerValidationSettings,
    AgentRunnerWorktreeSettings,
    AgentRunnerPostPrSupervisorSettings,
    IAR_REPOSITORY_CONFIG_FILENAME,
)
from backend.infrastructure.process_runner import CommandResult, SubprocessRunner


@dataclass(frozen=True)
class RepositoryInitOptions:
    """Options for creating repository-local IAR configuration."""

    cwd: Path
    repo_id_override: str | None = None
    display_name_override: str | None = None
    remote_override: str | None = None
    base_branch_override: str | None = None
    dry_run: bool = False
    force: bool = False


@dataclass(frozen=True)
class RepositoryInitResult:
    """Result of rendering or writing repository-local IAR configuration."""

    repo_root_path: Path
    config_path: Path
    config_text: str
    wrote_file: bool


def detect_git_repository_root(
    start_path: Path,
    process_runner: SubprocessRunner | None = None,
) -> Path:
    """Detect the Git repository root containing a path.

    Args:
        start_path: Directory or file path to inspect.
        process_runner: Optional subprocess runner.

    Returns:
        Resolved Git repository root path.

    Raises:
        ValueError: If the path does not exist or is outside a Git repository.
    """
    resolved_start_path = start_path.resolve()
    if not resolved_start_path.exists():
        raise ValueError(f"Path '{resolved_start_path}' does not exist.")

    cwd_path = (
        resolved_start_path
        if resolved_start_path.is_dir()
        else resolved_start_path.parent
    )
    git_result = _run_git(["rev-parse", "--show-toplevel"], cwd_path, process_runner)
    git_root_text = git_result.stdout.strip()
    if git_result.return_code != 0 or not git_root_text:
        raise ValueError(
            f"Path '{resolved_start_path}' is not inside a Git repository. "
            "Run iar from a target repository or pass --repo/--repo-id."
        )
    return Path(git_root_text).resolve()


TOML_HEADER_COMMENT = """# IAR local configuration for this repository.
# Empty fields inherit defaults from config.toml / environment variables.
# See: https://github.com/anthropics/claude-code (or your internal docs)
"""

IAR_CONFIG_COMMENT = """

# [agent_runner] section - agent runner settings
# Uncomment and set values to override global defaults
"""


def settings_to_toml_string(settings: AgentRunnerLocalSettings) -> str:
    """Serialize AgentRunnerLocalSettings to formatted TOML string."""

    data = _filter_none_dict(settings.model_dump())
    # Wrap in [agent_runner] to match .iar.toml structure
    wrapped = {"agent_runner": data}
    toml_body = tomli_w.dumps(wrapped)
    return TOML_HEADER_COMMENT + toml_body


def _filter_none_dict(data: dict) -> dict:
    """Recursively remove None values from dict for TOML serialization."""

    def filter_value(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, dict):
            return filter_dict(v)
        if isinstance(v, list):
            return [filter_value(item) for item in v]
        return v

    def filter_dict(d: dict) -> dict:
        result = {k: filter_value(v) for k, v in d.items() if v is not None}
        return result if result else {}

    return filter_dict(data)


def _dependency_name_matches(dependency_spec: str, package_name: str) -> bool:
    """Check whether a PEP 508 dependency spec names the given package."""
    spec_package_name = re.split(r"[<>=!~\[ ;]", dependency_spec.strip(), maxsplit=1)[0]
    return spec_package_name.replace("_", "-").lower() == package_name


def _uv_dependency_flag(
    pyproject_data: dict[str, Any], package_name: str
) -> str | None:
    """Return the ``uv run`` flag needed to reach a declared dependency.

    Returns ``""`` when the package is in the main dependencies or in the
    default ``dev`` dependency group (both installed by ``uv run`` without
    extra flags), ``" --group <name>"`` / ``" --extra <name>"`` when it lives
    in a non-default group or an optional-dependencies extra, and ``None``
    when the package is not declared at all.
    """
    project_table = pyproject_data.get("project") or {}
    for dependency_spec in project_table.get("dependencies") or []:
        if _dependency_name_matches(dependency_spec, package_name):
            return ""
    dependency_groups = pyproject_data.get("dependency-groups") or {}
    for group_name, group_entries in dependency_groups.items():
        group_specs = [entry for entry in group_entries if isinstance(entry, str)]
        if any(_dependency_name_matches(spec, package_name) for spec in group_specs):
            return "" if group_name == "dev" else f" --group {group_name}"
    optional_extras = project_table.get("optional-dependencies") or {}
    for extra_name, extra_specs in optional_extras.items():
        if any(_dependency_name_matches(spec, package_name) for spec in extra_specs):
            return f" --extra {extra_name}"
    return None


def detect_verification_commands(repo_root_path: Path) -> list[str]:
    """Detect verification commands that actually run in the target repository.

    ``iar init`` previously copied this tool's own defaults (such as
    ``uv run mkdocs build``) into every repository, which fails in any project
    that does not install mkdocs by default. Detection keeps the safe
    ``git diff --check`` baseline and adds tool commands only when the target
    repository declares the tool in ``pyproject.toml``, using the ``uv run``
    invocation (``--extra`` / ``--group``) that matches where the dependency
    is declared.
    """
    verification_commands = ["git diff --check"]
    pyproject_path = repo_root_path / "pyproject.toml"
    if not pyproject_path.is_file():
        return verification_commands
    try:
        with open(pyproject_path, "rb") as pyproject_file:
            pyproject_data: dict[str, Any] = tomllib.load(pyproject_file)
    except tomllib.TOMLDecodeError:
        return verification_commands

    if (repo_root_path / "mkdocs.yml").is_file():
        mkdocs_flag = _uv_dependency_flag(pyproject_data, "mkdocs")
        if mkdocs_flag is not None:
            verification_commands.append(f"uv run{mkdocs_flag} mkdocs build")

    if (repo_root_path / "tests").is_dir():
        pytest_flag = _uv_dependency_flag(pyproject_data, "pytest")
        if pytest_flag is not None:
            verification_commands.append(f"uv run{pytest_flag} pytest -q")

    return verification_commands


def build_repository_local_config_text(
    options: RepositoryInitOptions,
    process_runner: SubprocessRunner | None = None,
) -> tuple[Path, str]:
    """Render repository-local IAR TOML for a Git repository.

    Args:
        options: Init options, including cwd and explicit overrides.
        process_runner: Optional subprocess runner.

    Returns:
        A tuple of the detected repository root path and rendered TOML text.
    """
    repo_root_path = detect_git_repository_root(options.cwd, process_runner)
    selected_remote = options.remote_override or _detect_default_remote(
        repo_root_path, process_runner
    )
    detected_repo_id = _detect_repository_id(
        repo_root_path, selected_remote, process_runner
    )
    selected_repo_id = options.repo_id_override or detected_repo_id
    selected_display_name = options.display_name_override or repo_root_path.name
    selected_base_branch = options.base_branch_override or _detect_default_base_branch(
        repo_root_path, selected_remote, process_runner
    )

    settings = AgentRunnerLocalSettings(
        repository=AgentRunnerRepositoryMetadataSettings(
            id=selected_repo_id,
            enabled=True,
            display_name=selected_display_name,
        ),
        git=AgentRunnerGitSettings(
            remote=selected_remote,
            base_branch=selected_base_branch,
        ),
        worktree=AgentRunnerWorktreeSettings(),
        runner=AgentRunnerRunnerSettings(
            verification_commands=detect_verification_commands(repo_root_path)
        ),
        safety=AgentRunnerSafetySettings(),
        validation=AgentRunnerValidationSettings(),
        prompts=AgentRunnerPromptSettings(),
        pre_push_review=AgentRunnerPrePushReviewSettings(),
        post_pr_supervisor=AgentRunnerPostPrSupervisorSettings(),
        generated_content=AgentRunnerGeneratedContentSettings(),
    )

    return repo_root_path, settings_to_toml_string(settings)


def initialize_repository_local_config(
    options: RepositoryInitOptions,
    process_runner: SubprocessRunner | None = None,
) -> RepositoryInitResult:
    """Render or write ``.iar.toml`` for the current Git repository.

    Args:
        options: Init options controlling overrides and write behavior.
        process_runner: Optional subprocess runner.

    Returns:
        Init result with generated TOML and write status.

    Raises:
        ValueError: If ``.iar.toml`` already exists and overwrite was not forced.
    """
    repo_root_path, config_text = build_repository_local_config_text(
        options, process_runner
    )
    config_path = repo_root_path / IAR_REPOSITORY_CONFIG_FILENAME
    if config_path.exists() and not options.force and not options.dry_run:
        raise ValueError(
            f"IAR local config already exists at {config_path}. "
            "Use --force to overwrite it."
        )
    if options.dry_run:
        return RepositoryInitResult(
            repo_root_path=repo_root_path,
            config_path=config_path,
            config_text=config_text,
            wrote_file=False,
        )

    config_path.write_text(config_text, encoding="utf-8")
    return RepositoryInitResult(
        repo_root_path=repo_root_path,
        config_path=config_path,
        config_text=config_text,
        wrote_file=True,
    )


def _run_git(
    git_args: list[str],
    cwd_path: Path,
    process_runner: SubprocessRunner | None,
) -> CommandResult:
    runner = process_runner or SubprocessRunner()
    return runner.run(
        ["git", *git_args],
        cwd=cwd_path,
        check=False,
        capture_output=True,
    )


def _detect_default_remote(
    repo_root_path: Path,
    process_runner: SubprocessRunner | None,
) -> str:
    current_branch_result = _run_git(
        ["branch", "--show-current"], repo_root_path, process_runner
    )
    current_branch = current_branch_result.stdout.strip()
    if current_branch:
        upstream_remote_result = _run_git(
            ["config", f"branch.{current_branch}.remote"],
            repo_root_path,
            process_runner,
        )
        upstream_remote = upstream_remote_result.stdout.strip()
        if upstream_remote:
            return upstream_remote

    configured_remotes = _list_git_remotes(repo_root_path, process_runner)
    if "origin" in configured_remotes:
        return "origin"
    if configured_remotes:
        return configured_remotes[0]
    return "origin"


def _list_git_remotes(
    repo_root_path: Path,
    process_runner: SubprocessRunner | None,
) -> list[str]:
    remotes_result = _run_git(["remote"], repo_root_path, process_runner)
    return [line.strip() for line in remotes_result.stdout.splitlines() if line.strip()]


def _detect_repository_id(
    repo_root_path: Path,
    remote_name: str,
    process_runner: SubprocessRunner | None,
) -> str:
    remote_url_result = _run_git(
        ["remote", "get-url", remote_name], repo_root_path, process_runner
    )
    remote_url = remote_url_result.stdout.strip()
    if remote_url:
        remote_repo_name = _repository_name_from_remote_url(remote_url)
        if remote_repo_name:
            return _normalize_repository_id(remote_repo_name)
    return _normalize_repository_id(repo_root_path.name)


def _repository_name_from_remote_url(remote_url: str) -> str:
    trimmed_url = remote_url.rstrip("/")
    if trimmed_url.endswith(".git"):
        trimmed_url = trimmed_url[:-4]
    return re.split(r"[:/]", trimmed_url)[-1]


def _normalize_repository_id(raw_repo_name: str) -> str:
    normalized_repo_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_repo_name)
    stripped_repo_name = normalized_repo_name.strip("-_.").lower()
    return stripped_repo_name or "repository"


def _detect_default_base_branch(
    repo_root_path: Path,
    remote_name: str,
    process_runner: SubprocessRunner | None,
) -> str:
    remote_candidates = tuple(dict.fromkeys((remote_name, "origin")))
    for candidate_remote in remote_candidates:
        remote_head_branch = _remote_head_branch(
            repo_root_path, candidate_remote, process_runner
        )
        if remote_head_branch:
            return remote_head_branch

    for branch_name in ("main", "master"):
        branch_result = _run_git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            repo_root_path,
            process_runner,
        )
        if branch_result.return_code == 0:
            return branch_name

    current_branch_result = _run_git(
        ["branch", "--show-current"], repo_root_path, process_runner
    )
    current_branch = current_branch_result.stdout.strip()
    return current_branch or "main"


def _remote_head_branch(
    repo_root_path: Path,
    remote_name: str,
    process_runner: SubprocessRunner | None,
) -> str | None:
    remote_head_result = _run_git(
        ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote_name}/HEAD"],
        repo_root_path,
        process_runner,
    )
    remote_head_text = remote_head_result.stdout.strip()
    prefix = f"{remote_name}/"
    if remote_head_result.return_code == 0 and remote_head_text.startswith(prefix):
        return remote_head_text[len(prefix) :]
    return None
