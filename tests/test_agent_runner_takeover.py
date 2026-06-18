"""Tests for the global repository takeover flow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.shared.interfaces.runner_console import IRepositoryRegistryEditor
from backend.engines.agent_runner.takeover import (
    GitHubRepositoryCandidate,
    build_takeover_options,
    clone_github_repository,
    execute_takeover,
    filter_unregistered_candidates,
    list_github_repositories,
    parse_selected_repositories,
    register_repository,
)
from backend.infrastructure.config.registry_editor import RegistryRepositoryEntry
from backend.infrastructure.process_runner import CommandResult
from tests.conftest import FakeProcessRunner


class _InMemoryRegistryEditor(IRepositoryRegistryEditor):
    """Registry editor that stores entries in memory for tests."""

    def __init__(self, entries: list[RegistryRepositoryEntry] | None = None) -> None:
        self._entries = list(entries or [])

    def list_repositories(self) -> list[RegistryRepositoryEntry]:
        return list(self._entries)

    def add_repository(
        self, *, repo_id: str, path: str, display_name: str | None
    ) -> None:
        if any(entry.repo_id == repo_id for entry in self._entries):
            raise ValueError(f"Repository '{repo_id}' already exists in the registry.")
        self._entries.append(
            RegistryRepositoryEntry(
                repo_id=repo_id,
                path=path,
                enabled=True,
                display_name=display_name,
                path_exists=Path(path).exists(),
            )
        )

    def set_enabled(self, repo_id: str, *, enabled: bool) -> None:
        for index, entry in enumerate(self._entries):
            if entry.repo_id == repo_id:
                self._entries[index] = RegistryRepositoryEntry(
                    repo_id=entry.repo_id,
                    path=entry.path,
                    enabled=enabled,
                    display_name=entry.display_name,
                    path_exists=entry.path_exists,
                )
                return
        raise KeyError(f"Repository '{repo_id}' not found in the registry.")


def _gh_list_response(repositories: list[dict]) -> CommandResult:
    return CommandResult(
        command=("gh", "repo", "list"),
        return_code=0,
        stdout=json.dumps(repositories),
        stderr="",
    )


def test_build_takeover_options_defaults(tmp_path: Path) -> None:
    """Default options should use ~/.iar/repos and enable daemon starts."""
    options = build_takeover_options()
    assert options.clone_root == Path.home() / ".iar" / "repos"
    assert options.owner is None
    assert options.limit == 100
    assert options.selected_repos == ()
    assert options.start_daemons is True
    assert options.dry_run is False


def test_build_takeover_options_custom_clone_root(tmp_path: Path) -> None:
    """Custom clone root should be expanded and created."""
    custom_root = tmp_path / "managed-repos"
    options = build_takeover_options(clone_root=str(custom_root), start_daemons=False)
    assert options.clone_root == custom_root
    assert custom_root.exists()


def test_parse_selected_repositories() -> None:
    """Explicit owner/repo arguments should parse into candidates."""
    candidates = parse_selected_repositories(("owner/repo-a", "owner/repo-b"))
    assert len(candidates) == 2
    assert candidates[0].full_name == "owner/repo-a"
    assert candidates[1].full_name == "owner/repo-b"


def test_parse_selected_repositories_rejects_invalid() -> None:
    """Invalid repository formats should raise ValueError."""
    with pytest.raises(ValueError, match="owner/name format"):
        parse_selected_repositories(("not-a-repo",))


def test_list_github_repositories() -> None:
    """list_github_repositories should parse gh repo list JSON output."""
    runner = FakeProcessRunner(
        responses={
            (
                "gh",
                "repo",
                "list",
                "--limit",
                "100",
                "--json",
                "nameWithOwner,description,viewerPermission",
            ): _gh_list_response(
                [
                    {
                        "nameWithOwner": "owner/repo-a",
                        "description": "Repo A",
                        "viewerPermission": "ADMIN",
                    },
                    {
                        "nameWithOwner": "owner/repo-b",
                        "description": None,
                        "viewerPermission": "WRITE",
                    },
                ]
            ),
        }
    )
    candidates = list_github_repositories(owner=None, limit=100, process_runner=runner)
    assert len(candidates) == 2
    assert candidates[0].full_name == "owner/repo-a"
    assert candidates[0].owner == "owner"
    assert candidates[0].name == "repo-a"
    assert candidates[0].description == "Repo A"
    assert candidates[0].viewer_permission == "ADMIN"
    assert candidates[1].description is None


def test_list_github_repositories_with_owner() -> None:
    """The owner argument should be passed to gh repo list."""
    runner = FakeProcessRunner(
        responses={
            (
                "gh",
                "repo",
                "list",
                "--limit",
                "50",
                "--json",
                "nameWithOwner,description,viewerPermission",
                "myorg",
            ): _gh_list_response([{"nameWithOwner": "myorg/repo"}]),
        }
    )
    candidates = list_github_repositories(
        owner="myorg", limit=50, process_runner=runner
    )
    assert len(candidates) == 1
    assert candidates[0].full_name == "myorg/repo"


def test_list_github_repositories_failure() -> None:
    """Failures from gh should raise RuntimeError."""
    runner = FakeProcessRunner(
        responses={
            (
                "gh",
                "repo",
                "list",
                "--limit",
                "100",
                "--json",
                "nameWithOwner,description,viewerPermission",
            ): CommandResult(
                command=("gh", "repo", "list"),
                return_code=1,
                stdout="",
                stderr="not authenticated",
            ),
        }
    )
    with pytest.raises(RuntimeError, match="not authenticated"):
        list_github_repositories(owner=None, limit=100, process_runner=runner)


def test_clone_github_repository(tmp_path: Path) -> None:
    """clone_github_repository should invoke gh repo clone into the managed root."""
    clone_root = tmp_path / "repos"
    runner = FakeProcessRunner()
    candidate = GitHubRepositoryCandidate(
        owner="owner",
        name="repo-a",
        full_name="owner/repo-a",
        description=None,
        viewer_permission=None,
    )
    repo_path = clone_github_repository(
        candidate=candidate,
        clone_root=clone_root,
        process_runner=runner,
    )
    assert repo_path == clone_root / "owner" / "repo-a"
    assert ["gh", "repo", "clone", "owner/repo-a", str(repo_path)] in runner.calls


def test_clone_github_repository_skips_existing(tmp_path: Path) -> None:
    """clone_github_repository should not clone if the repo already exists."""
    clone_root = tmp_path / "repos"
    repo_path = clone_root / "owner" / "repo-a"
    repo_path.mkdir(parents=True)
    (repo_path / ".git").mkdir()
    runner = FakeProcessRunner()
    candidate = GitHubRepositoryCandidate(
        owner="owner",
        name="repo-a",
        full_name="owner/repo-a",
        description=None,
        viewer_permission=None,
    )
    result_path = clone_github_repository(
        candidate=candidate,
        clone_root=clone_root,
        process_runner=runner,
    )
    assert result_path == repo_path
    assert not runner.calls


def test_filter_unregistered_candidates(tmp_path: Path) -> None:
    """Already registered and cloned repositories should be skipped."""
    clone_root = tmp_path / "repos"
    repo_path = clone_root / "owner" / "repo-a"
    repo_path.mkdir(parents=True)
    (repo_path / ".git").mkdir()
    editor = _InMemoryRegistryEditor(
        [
            RegistryRepositoryEntry(
                repo_id="owner-repo-a",
                path=str(repo_path),
                enabled=True,
                display_name="Repo A",
                path_exists=True,
            )
        ]
    )
    candidates = [
        GitHubRepositoryCandidate(
            owner="owner",
            name="repo-a",
            full_name="owner/repo-a",
            description=None,
            viewer_permission=None,
        ),
        GitHubRepositoryCandidate(
            owner="owner",
            name="repo-b",
            full_name="owner/repo-b",
            description=None,
            viewer_permission=None,
        ),
    ]
    filtered = filter_unregistered_candidates(candidates, editor, clone_root)
    assert len(filtered) == 1
    assert filtered[0].full_name == "owner/repo-b"


def test_register_repository(tmp_path: Path) -> None:
    """register_repository should add a new entry to the registry."""
    editor = _InMemoryRegistryEditor()
    registered = register_repository(
        repo_id="owner-repo-a",
        repo_path=tmp_path / "repo-a",
        display_name="Repo A",
        editor=editor,
    )
    assert registered is True
    entries = editor.list_repositories()
    assert len(entries) == 1
    assert entries[0].repo_id == "owner-repo-a"


def test_register_repository_idempotent(tmp_path: Path) -> None:
    """register_repository should return False for already-registered repos."""
    editor = _InMemoryRegistryEditor()
    register_repository(
        repo_id="owner-repo-a",
        repo_path=tmp_path / "repo-a",
        display_name="Repo A",
        editor=editor,
    )
    registered = register_repository(
        repo_id="owner-repo-a",
        repo_path=tmp_path / "repo-a",
        display_name="Repo A",
        editor=editor,
    )
    assert registered is False


def test_execute_takeover_dry_run(tmp_path: Path) -> None:
    """Dry run should not clone, init, or register."""
    clone_root = tmp_path / "repos"
    editor = _InMemoryRegistryEditor()
    runner = FakeProcessRunner()
    options = build_takeover_options(
        clone_root=str(clone_root), start_daemons=False, dry_run=True
    )
    candidate = GitHubRepositoryCandidate(
        owner="owner",
        name="repo-a",
        full_name="owner/repo-a",
        description=None,
        viewer_permission=None,
    )
    result = execute_takeover(
        options=options,
        candidates=[candidate],
        editor=editor,
        process_runner=runner,
    )
    assert result.attempted == 1
    assert result.succeeded == 1
    assert not editor.list_repositories()
    assert not runner.calls


def _prepare_cloned_repo(clone_root: Path, full_name: str) -> Path:
    """Create a fake cloned repository root with .git metadata."""
    owner, _, name = full_name.partition("/")
    repo_path = clone_root / owner / name
    repo_path.mkdir(parents=True)
    (repo_path / ".git").mkdir()
    return repo_path


def _fake_git_runner(repo_path: Path) -> FakeProcessRunner:
    """Return a FakeProcessRunner that answers git rev-parse with repo_path."""
    repo_path_str = str(repo_path)
    return FakeProcessRunner(
        responses={
            ("git", "rev-parse", "--show-toplevel", "-C", repo_path_str): CommandResult(
                command=("git", "rev-parse", "--show-toplevel"),
                return_code=0,
                stdout=repo_path_str,
                stderr="",
            ),
            ("git", "rev-parse", "--show-toplevel"): CommandResult(
                command=("git", "rev-parse", "--show-toplevel"),
                return_code=0,
                stdout=repo_path_str,
                stderr="",
            ),
        }
    )


def test_execute_takeover_clones_inits_registers(tmp_path: Path) -> None:
    """execute_takeover should clone, init, and register a repository."""
    clone_root = tmp_path / "repos"
    repo_path = _prepare_cloned_repo(clone_root, "owner/repo-a")
    editor = _InMemoryRegistryEditor()
    runner = _fake_git_runner(repo_path)
    options = build_takeover_options(
        clone_root=str(clone_root), start_daemons=False, dry_run=False
    )
    candidate = GitHubRepositoryCandidate(
        owner="owner",
        name="repo-a",
        full_name="owner/repo-a",
        description=None,
        viewer_permission=None,
    )
    result = execute_takeover(
        options=options,
        candidates=[candidate],
        editor=editor,
        process_runner=runner,
    )
    assert result.attempted == 1
    assert result.succeeded == 1
    assert result.repositories[0].cloned
    assert result.repositories[0].initialized
    assert result.repositories[0].registered
    entries = editor.list_repositories()
    assert len(entries) == 1
    assert entries[0].repo_id == "owner-repo-a"


def test_execute_takeover_starts_daemons(tmp_path: Path) -> None:
    """execute_takeover should invoke the daemon start callback when enabled."""
    clone_root = tmp_path / "repos"
    repo_path = _prepare_cloned_repo(clone_root, "owner/repo-a")
    editor = _InMemoryRegistryEditor()
    runner = _fake_git_runner(repo_path)
    options = build_takeover_options(
        clone_root=str(clone_root), start_daemons=True, dry_run=False
    )
    candidate = GitHubRepositoryCandidate(
        owner="owner",
        name="repo-a",
        full_name="owner/repo-a",
        description=None,
        viewer_permission=None,
    )
    started: list[tuple[str, Path]] = []

    def _start(repo_id: str, repo_path: Path) -> None:
        started.append((repo_id, repo_path))

    result = execute_takeover(
        options=options,
        candidates=[candidate],
        editor=editor,
        process_runner=runner,
        start_daemon_callback=_start,
    )
    assert result.started_daemons == 1
    assert result.started_review_daemons == 1
    assert len(started) == 1
    assert started[0][0] == "owner-repo-a"
