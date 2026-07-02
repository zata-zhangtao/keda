"""Global repository takeover for the issue-agent-runner CLI.

This module lets ``iar takeover`` discover GitHub repositories via the
authenticated ``gh`` CLI, clone them into a managed directory, initialize each
with ``iar init``, register them in the global registry, and optionally start
managed ``daemon`` and ``review-daemon`` processes.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from backend.engines.agent_runner.repository_local import (
    RepositoryInitOptions,
    initialize_repository_local_config,
    normalize_repository_id,
)
from backend.infrastructure.config.settings import IAR_REPOSITORY_CONFIG_FILENAME

if TYPE_CHECKING:
    from backend.core.shared.interfaces.runner_console import IRepositoryRegistryEditor
    from backend.infrastructure.process_runner import SubprocessRunner

_logger = logging.getLogger(__name__)

_DEFAULT_CLONE_ROOT = Path.home() / ".iar" / "repos"


@dataclass(frozen=True)
class GitHubRepositoryCandidate:
    """A repository returned by ``gh repo list``."""

    owner: str
    name: str
    full_name: str
    description: str | None
    viewer_permission: str | None

    @property
    def normalized_repo_id(self) -> str:
        """Return a registry-safe repo_id from ``owner/name``."""
        return normalize_repository_id(self.full_name.replace("/", "-"))


@dataclass(frozen=True)
class TakeoverOptions:
    """Options controlling a takeover run."""

    clone_root: Path
    owner: str | None
    limit: int
    selected_repos: tuple[str, ...]
    start_daemons: bool
    dry_run: bool


@dataclass(frozen=True)
class TakeoverRepositoryResult:
    """Outcome for a single repository in a takeover batch."""

    full_name: str
    repo_id: str
    repo_path: Path
    cloned: bool
    initialized: bool
    registered: bool
    daemon_started: bool
    review_daemon_started: bool
    error: str | None


@dataclass(frozen=True)
class TakeoverResult:
    """Summary of a takeover batch."""

    attempted: int
    succeeded: int
    started_daemons: int
    started_review_daemons: int
    repositories: tuple[TakeoverRepositoryResult, ...]


def _default_clone_root() -> Path:
    """Return the default managed clone root ``~/.iar/repos``."""
    clone_root = _DEFAULT_CLONE_ROOT
    clone_root.mkdir(parents=True, exist_ok=True)
    return clone_root


def build_takeover_options(
    *,
    clone_root: str | None = None,
    owner: str | None = None,
    limit: int = 100,
    selected_repos: tuple[str, ...] = (),
    start_daemons: bool = True,
    dry_run: bool = False,
) -> TakeoverOptions:
    """Build validated takeover options from CLI arguments."""
    resolved_clone_root = (
        Path(clone_root).expanduser() if clone_root else _default_clone_root()
    )
    resolved_clone_root.mkdir(parents=True, exist_ok=True)
    return TakeoverOptions(
        clone_root=resolved_clone_root,
        owner=owner,
        limit=max(limit, 1),
        selected_repos=selected_repos,
        start_daemons=start_daemons,
        dry_run=dry_run,
    )


def list_github_repositories(
    *,
    owner: str | None,
    limit: int,
    process_runner: SubprocessRunner,
) -> list[GitHubRepositoryCandidate]:
    """List repositories visible to the authenticated ``gh`` user.

    Args:
        owner: Optional GitHub user or organization name. Defaults to the
            currently authenticated user.
        limit: Maximum number of repositories to fetch.
        process_runner: Subprocess runner for invoking ``gh``.

    Returns:
        List of repository candidates.

    Raises:
        RuntimeError: If ``gh repo list`` fails.
    """
    command = [
        "gh",
        "repo",
        "list",
        "--limit",
        str(limit),
        "--json",
        "nameWithOwner,description,viewerPermission",
    ]
    if owner:
        command.append(owner)

    result = process_runner.run(
        command, cwd=Path.cwd(), check=False, capture_output=True
    )
    if result.return_code != 0:
        error_message = (
            result.stderr.strip() or result.stdout.strip() or "unknown error"
        )
        raise RuntimeError(f"Failed to list GitHub repositories: {error_message}")

    try:
        raw_repositories = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from gh repo list: {exc}") from exc

    candidates: list[GitHubRepositoryCandidate] = []
    for raw_repo in raw_repositories:
        if not isinstance(raw_repo, dict):
            continue
        full_name = str(raw_repo.get("nameWithOwner", "")).strip()
        if not full_name or "/" not in full_name:
            continue
        owner_part, _, name_part = full_name.partition("/")
        candidates.append(
            GitHubRepositoryCandidate(
                owner=owner_part,
                name=name_part,
                full_name=full_name,
                description=str(raw_repo.get("description") or "") or None,
                viewer_permission=str(raw_repo.get("viewerPermission") or "") or None,
            )
        )
    return candidates


def parse_selected_repositories(
    selected_repos: tuple[str, ...],
) -> list[GitHubRepositoryCandidate]:
    """Parse explicit ``owner/repo`` arguments into candidates.

    Raises:
        ValueError: If any entry is not in ``owner/repo`` format.
    """
    candidates: list[GitHubRepositoryCandidate] = []
    for full_name in selected_repos:
        full_name = full_name.strip()
        if not full_name or "/" not in full_name:
            raise ValueError(f"Repository must be in owner/name format: {full_name!r}")
        owner_part, _, name_part = full_name.partition("/")
        candidates.append(
            GitHubRepositoryCandidate(
                owner=owner_part,
                name=name_part,
                full_name=full_name,
                description=None,
                viewer_permission=None,
            )
        )
    return candidates


def _repository_path(clone_root: Path, candidate: GitHubRepositoryCandidate) -> Path:
    """Return the local path where a repository should be cloned."""
    return clone_root / candidate.owner / candidate.name


def clone_github_repository(
    *,
    candidate: GitHubRepositoryCandidate,
    clone_root: Path,
    process_runner: SubprocessRunner,
) -> Path:
    """Clone a GitHub repository into the managed clone root.

    Args:
        candidate: Repository to clone.
        clone_root: Managed clone root directory.
        process_runner: Subprocess runner for invoking ``gh``.

    Returns:
        Path to the cloned repository root.

    Raises:
        RuntimeError: If cloning fails.
    """
    repo_path = _repository_path(clone_root, candidate)
    repo_path.parent.mkdir(parents=True, exist_ok=True)

    if (repo_path / ".git").exists():
        _logger.info("Repository already cloned at %s", repo_path)
        return repo_path

    command = [
        "gh",
        "repo",
        "clone",
        candidate.full_name,
        str(repo_path),
    ]
    result = process_runner.run(
        command, cwd=clone_root, check=False, capture_output=True
    )
    if result.return_code != 0:
        error_message = (
            result.stderr.strip() or result.stdout.strip() or "unknown error"
        )
        raise RuntimeError(f"Failed to clone {candidate.full_name}: {error_message}")
    return repo_path


def ensure_repository_initialized(
    *,
    repo_path: Path,
    repo_id: str,
    display_name: str,
    process_runner: SubprocessRunner,
    dry_run: bool = False,
) -> None:
    """Ensure a repository has an ``.iar.toml`` config.

    If the local config already exists, it is left untouched unless ``dry_run``
    is set, in which case nothing is written.

    Args:
        repo_path: Repository root path.
        repo_id: Registry identifier to write into ``.iar.toml``.
        display_name: Display name to write into ``.iar.toml``.
        process_runner: Subprocess runner for git operations.
        dry_run: When ``True``, skip actual writes.
    """
    config_path = repo_path / IAR_REPOSITORY_CONFIG_FILENAME
    if config_path.exists():
        _logger.info("Repository already initialized: %s", config_path)
        return
    if dry_run:
        _logger.info("Would initialize repository at %s", repo_path)
        return

    initialize_repository_local_config(
        RepositoryInitOptions(
            cwd=repo_path,
            repo_id_override=repo_id,
            display_name_override=display_name,
            remote_override="origin",
        ),
        process_runner,
    )


@dataclasses.dataclass(frozen=True)
class UpsertRepositoryResult:
    """Outcome of registering or updating a repository in the global registry."""

    repo_id: str
    path: str
    display_name: str | None
    action: str  # "added", "updated", "unchanged"
    previous_path: str | None = None


def register_repository(
    *,
    repo_id: str,
    repo_path: Path,
    display_name: str,
    editor: IRepositoryRegistryEditor,
    dry_run: bool = False,
) -> bool:
    """Register a repository in the global registry.

    Args:
        repo_id: Registry identifier.
        repo_path: Repository root path.
        display_name: Human-readable display name.
        editor: Registry editor.
        dry_run: When ``True``, skip actual writes.

    Returns:
        ``True`` if newly registered, ``False`` if already present.

    Raises:
        ValueError: If registration fails for a reason other than duplication.
    """
    registered_entries = {entry.repo_id: entry for entry in editor.list_repositories()}
    if repo_id in registered_entries:
        _logger.info("Repository already registered: %s", repo_id)
        return False
    if dry_run:
        _logger.info("Would register repository: %s -> %s", repo_id, repo_path)
        return True

    editor.add_repository(
        repo_id=repo_id,
        path=str(repo_path),
        display_name=display_name,
    )
    return True


def upsert_repository(
    *,
    repo_id: str,
    repo_path: Path,
    display_name: str,
    editor: IRepositoryRegistryEditor,
    dry_run: bool = False,
) -> UpsertRepositoryResult:
    """Add or update a repository entry in the global registry.

    If ``repo_id`` already exists but points to a different resolved path, the
    existing entry is replaced with the new path so that ``iar init`` in a new
    location for the same logical repository keeps ``iar daemon`` working.

    Args:
        repo_id: Registry identifier.
        repo_path: Repository root path.
        display_name: Human-readable display name.
        editor: Registry editor.
        dry_run: When ``True``, skip actual writes.

    Returns:
        Structured result describing whether the entry was added, updated, or
        left unchanged.

    Raises:
        ValueError: If reading or writing the registry fails.
    """
    resolved_path = repo_path.expanduser().resolve()
    registered_entries = {entry.repo_id: entry for entry in editor.list_repositories()}
    existing = registered_entries.get(repo_id)

    if existing is None:
        if dry_run:
            return UpsertRepositoryResult(
                repo_id=repo_id,
                path=str(resolved_path),
                display_name=display_name,
                action="added",
            )
        editor.add_repository(
            repo_id=repo_id,
            path=str(resolved_path),
            display_name=display_name,
        )
        return UpsertRepositoryResult(
            repo_id=repo_id,
            path=str(resolved_path),
            display_name=display_name,
            action="added",
        )

    existing_path = Path(existing.path).expanduser().resolve()
    if existing_path == resolved_path:
        return UpsertRepositoryResult(
            repo_id=repo_id,
            path=str(resolved_path),
            display_name=display_name,
            action="unchanged",
        )

    if dry_run:
        return UpsertRepositoryResult(
            repo_id=repo_id,
            path=str(resolved_path),
            display_name=display_name,
            action="updated",
            previous_path=existing.path,
        )

    editor.remove_repository(repo_id)
    editor.add_repository(
        repo_id=repo_id,
        path=str(resolved_path),
        display_name=display_name,
    )
    return UpsertRepositoryResult(
        repo_id=repo_id,
        path=str(resolved_path),
        display_name=display_name,
        action="updated",
        previous_path=existing.path,
    )


def filter_unregistered_candidates(
    candidates: list[GitHubRepositoryCandidate],
    editor: IRepositoryRegistryEditor,
    clone_root: Path,
) -> list[GitHubRepositoryCandidate]:
    """Drop candidates that are already registered and cloned."""
    registered = {entry.repo_id: entry for entry in editor.list_repositories()}
    filtered: list[GitHubRepositoryCandidate] = []
    for candidate in candidates:
        repo_id = candidate.normalized_repo_id
        repo_path = _repository_path(clone_root, candidate)
        if (
            repo_id in registered
            and repo_path.exists()
            and (repo_path / ".git").exists()
        ):
            _logger.info("Skipping already-managed repository: %s", candidate.full_name)
            continue
        filtered.append(candidate)
    return filtered


def execute_takeover(
    *,
    options: TakeoverOptions,
    candidates: list[GitHubRepositoryCandidate],
    editor: IRepositoryRegistryEditor,
    process_runner: SubprocessRunner,
    start_daemon_callback: "Callable[[str, Path], None] | None" = None,
    progress_callback: "Callable[[str, str], None] | None" = None,
) -> TakeoverResult:
    """Execute the takeover plan for a list of candidates.

    Args:
        options: Takeover options.
        candidates: Repositories to take over.
        editor: Registry editor.
        process_runner: Subprocess runner for git/gh operations.
        start_daemon_callback: Optional callback invoked for each repository
            when ``options.start_daemons`` is ``True``. Receives ``repo_id``
            and ``repo_path``.
        progress_callback: Optional callback invoked as stages complete for
            each repository. Receives ``full_name`` and a stage identifier
            such as ``"clone"``, ``"init"``, ``"register"``,
            ``"start_daemons"``, or ``"complete"``.

    Returns:
        Summary of the takeover batch.
    """
    results: list[TakeoverRepositoryResult] = []
    succeeded = 0
    started_daemons = 0
    started_review_daemons = 0

    def _report_progress(stage: str) -> None:
        if progress_callback is not None:
            progress_callback(candidate.full_name, stage)

    for candidate in candidates:
        repo_path = _repository_path(options.clone_root, candidate)
        repo_id = candidate.normalized_repo_id
        result = TakeoverRepositoryResult(
            full_name=candidate.full_name,
            repo_id=repo_id,
            repo_path=repo_path,
            cloned=False,
            initialized=False,
            registered=False,
            daemon_started=False,
            review_daemon_started=False,
            error=None,
        )

        try:
            if not options.dry_run:
                clone_github_repository(
                    candidate=candidate,
                    clone_root=options.clone_root,
                    process_runner=process_runner,
                )
            result = dataclasses.replace(result, cloned=True)
            _report_progress("clone")

            ensure_repository_initialized(
                repo_path=repo_path,
                repo_id=repo_id,
                display_name=candidate.name,
                process_runner=process_runner,
                dry_run=options.dry_run,
            )
            result = dataclasses.replace(result, initialized=True)
            _report_progress("init")

            registered = register_repository(
                repo_id=repo_id,
                repo_path=repo_path,
                display_name=candidate.name,
                editor=editor,
                dry_run=options.dry_run,
            )
            result = dataclasses.replace(result, registered=registered)
            _report_progress("register")

            if options.start_daemons and not options.dry_run:
                if start_daemon_callback is not None:
                    start_daemon_callback(repo_id, repo_path)
                    result = dataclasses.replace(
                        result,
                        daemon_started=True,
                        review_daemon_started=True,
                    )
                    started_daemons += 1
                    started_review_daemons += 1
                    _report_progress("start_daemons")

            if result.error is None:
                succeeded += 1
            _report_progress("complete")

        except Exception as exc:  # noqa: BLE001 - batch should isolate failures.
            _logger.error("Takeover failed for %s: %s", candidate.full_name, exc)
            result = dataclasses.replace(result, error=str(exc))

        results.append(result)

    return TakeoverResult(
        attempted=len(candidates),
        succeeded=succeeded,
        started_daemons=started_daemons,
        started_review_daemons=started_review_daemons,
        repositories=tuple(results),
    )
