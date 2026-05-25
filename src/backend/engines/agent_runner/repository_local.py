"""Repository-local configuration helpers for issue-agent-runner."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from backend.infrastructure.config.settings import IAR_REPOSITORY_CONFIG_FILENAME
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

    toml_text = "\n".join(
        [
            "[agent_runner.repository]",
            f"id = {_toml_quote(selected_repo_id)}",
            "enabled = true",
            f"display_name = {_toml_quote(selected_display_name)}",
            "",
            "[agent_runner.git]",
            f"remote = {_toml_quote(selected_remote)}",
            f"base_branch = {_toml_quote(selected_base_branch)}",
            "",
            "[agent_runner.runner]",
            "verification_commands = [",
            f"  {_toml_quote('git diff --check')},",
            "]",
            "",
        ]
    )
    return repo_root_path, toml_text


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


def _toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
