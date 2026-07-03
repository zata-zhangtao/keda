"""Tests for repository registry validation and tomlkit write-back."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.use_cases.repository_registry import (
    RegistryValidationError,
    add_registry_repository,
    list_registry_repositories,
    set_registry_repository_enabled,
)
from backend.infrastructure.config.registry_editor import TomlRegistryEditor

_CONFIG_TEMPLATE = """\
# 顶部注释必须保留
[app]
name = "my-app"  # 行内注释

# registry 注释也必须保留
[agent_runner.repositories.existing]
path = "{existing_path}"
enabled = true
display_name = "Existing"
"""


@pytest.fixture
def config_with_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a temp config.toml plus a real git-like repo directory."""
    repo_path = tmp_path / "existing-repo"
    (repo_path / ".git").mkdir(parents=True)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _CONFIG_TEMPLATE.format(existing_path=repo_path),
        encoding="utf-8",
    )
    return config_path, repo_path


def test_list_repositories(config_with_repo: tuple[Path, Path]) -> None:
    """Existing entries should be listed with path existence flags."""
    config_path, repo_path = config_with_repo
    entries = list_registry_repositories(TomlRegistryEditor(config_path))
    assert len(entries) == 1
    assert entries[0].repo_id == "existing"
    assert entries[0].enabled is True
    assert entries[0].path_exists is True
    assert entries[0].display_name == "Existing"


def test_add_repository_preserves_comments(
    config_with_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    """Adding an entry must keep existing comments and formatting."""
    config_path, _existing = config_with_repo
    new_repo = tmp_path / "new-repo"
    (new_repo / ".git").mkdir(parents=True)

    add_registry_repository(
        editor=TomlRegistryEditor(config_path),
        repo_id="new-repo",
        path=str(new_repo),
        display_name="New Repo",
    )

    written_text = config_path.read_text(encoding="utf-8")
    assert "# 顶部注释必须保留" in written_text
    assert "# registry 注释也必须保留" in written_text
    assert "# 行内注释" in written_text
    assert "[agent_runner.repositories.new-repo]" in written_text

    entries = list_registry_repositories(TomlRegistryEditor(config_path))
    assert {entry.repo_id for entry in entries} == {"existing", "new-repo"}


def test_add_rejects_invalid_repo_id(config_with_repo: tuple[Path, Path]) -> None:
    """repo_id outside ^[a-z0-9][a-z0-9-]*$ must be rejected."""
    config_path, repo_path = config_with_repo
    with pytest.raises(RegistryValidationError, match="repo_id"):
        add_registry_repository(
            editor=TomlRegistryEditor(config_path),
            repo_id="Bad_ID",
            path=str(repo_path),
            display_name=None,
        )


def test_add_rejects_missing_path(config_with_repo: tuple[Path, Path]) -> None:
    """A non-existent path must be rejected before any write."""
    config_path, _ = config_with_repo
    original_text = config_path.read_text(encoding="utf-8")
    with pytest.raises(RegistryValidationError, match="does not exist"):
        add_registry_repository(
            editor=TomlRegistryEditor(config_path),
            repo_id="ghost",
            path="/definitely/not/here",
            display_name=None,
        )
    assert config_path.read_text(encoding="utf-8") == original_text


def test_add_rejects_non_git_path(config_with_repo: tuple[Path, Path], tmp_path: Path) -> None:
    """A directory without .git must be rejected."""
    config_path, _ = config_with_repo
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    with pytest.raises(RegistryValidationError, match="not a git repository"):
        add_registry_repository(
            editor=TomlRegistryEditor(config_path),
            repo_id="plain",
            path=str(plain_dir),
            display_name=None,
        )


def test_add_rejects_duplicate_id(config_with_repo: tuple[Path, Path]) -> None:
    """Duplicate repo_id must be rejected."""
    config_path, repo_path = config_with_repo
    with pytest.raises(RegistryValidationError, match="already exists"):
        add_registry_repository(
            editor=TomlRegistryEditor(config_path),
            repo_id="existing",
            path=str(repo_path),
            display_name=None,
        )


def test_set_enabled_round_trip(config_with_repo: tuple[Path, Path]) -> None:
    """Disabling then enabling must round-trip through the file."""
    config_path, _ = config_with_repo
    editor = TomlRegistryEditor(config_path)
    set_registry_repository_enabled(editor=editor, repo_id="existing", enabled=False)
    assert list_registry_repositories(editor)[0].enabled is False
    set_registry_repository_enabled(editor=editor, repo_id="existing", enabled=True)
    assert list_registry_repositories(editor)[0].enabled is True
    # 注释依旧保留。
    assert "# registry 注释也必须保留" in config_path.read_text(encoding="utf-8")


def test_set_enabled_unknown_repo(config_with_repo: tuple[Path, Path]) -> None:
    """Toggling an unknown repo_id must raise RegistryValidationError."""
    config_path, _ = config_with_repo
    with pytest.raises(RegistryValidationError):
        set_registry_repository_enabled(
            editor=TomlRegistryEditor(config_path), repo_id="ghost", enabled=True
        )


def test_remove_repository_deletes_entry_and_preserves_comments(
    config_with_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    """Removing an entry must delete its table while keeping other comments."""
    config_path, _ = config_with_repo
    new_repo = tmp_path / "new-repo"
    (new_repo / ".git").mkdir(parents=True)
    editor = TomlRegistryEditor(config_path)
    editor.add_repository(repo_id="new-repo", path=str(new_repo), display_name="New Repo")

    editor.remove_repository("existing")

    entries = list_registry_repositories(editor)
    assert [entry.repo_id for entry in entries] == ["new-repo"]
    written_text = config_path.read_text(encoding="utf-8")
    assert "# 顶部注释必须保留" in written_text
    assert "# registry 注释也必须保留" in written_text
    assert "[agent_runner.repositories.existing]" not in written_text


def test_remove_repository_unknown_repo(config_with_repo: tuple[Path, Path]) -> None:
    """Removing an unknown repo_id must raise KeyError."""
    config_path, _ = config_with_repo
    with pytest.raises(KeyError, match="not found"):
        TomlRegistryEditor(config_path).remove_repository("ghost")
