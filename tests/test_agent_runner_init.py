"""Tests for repository-local IAR initialization."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.api.cli import main
from backend.engines.agent_runner.repository_local import (
    GITIGNORE_BLOCK_FOOTER,
    GITIGNORE_BLOCK_HEADER,
    GitignoreSyncOptions,
    RepositoryInitOptions,
    _detect_default_remote,
    build_repository_local_config_text,
    detect_verification_commands,
    ensure_gitignore_entries,
    initialize_repository_local_config,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _run_git(repo_path: Path, *git_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *git_args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _init_git_repository(tmp_path: Path, name: str) -> Path:
    repo_path = tmp_path / name
    repo_path.mkdir()
    _run_git(repo_path, "init")
    _run_git(repo_path, "checkout", "-b", "main")
    _run_git(repo_path, "remote", "add", "origin", "git@github.com:example/target.git")
    return repo_path


def test_iar_init_dry_run_real_entry(tmp_path: Path) -> None:
    """uv run iar init --dry-run should print TOML and not write .iar.toml."""
    repo_path = _init_git_repository(tmp_path, "target")
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT),
            "iar",
            "init",
            "--dry-run",
        ],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "[agent_runner.repository]" in completed.stdout
    assert 'id = "target"' in completed.stdout
    assert "请检查" in completed.stderr
    assert "Please review verification_commands" in completed.stderr
    assert "git diff --check" in completed.stderr
    assert not (repo_path / ".iar.toml").exists()


def test_iar_init_result_includes_verification_commands(tmp_path: Path) -> None:
    """RepositoryInitResult should carry the detected verification commands."""
    repo_path = _init_git_repository(tmp_path, "target")
    (repo_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "target"',
                'version = "0.1.0"',
                "dependencies = []",
                "",
                "[project.optional-dependencies]",
                'dev = ["mkdocs>=1.6.1"]',
            ]
        ),
        encoding="utf-8",
    )
    (repo_path / "mkdocs.yml").write_text("site_name: target\n", encoding="utf-8")

    init_result = initialize_repository_local_config(
        RepositoryInitOptions(cwd=repo_path, dry_run=True)
    )

    assert init_result.verification_commands == [
        "git diff --check",
        "uv run --extra dev mkdocs build",
    ]


def test_iar_init_prints_review_hint_after_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`iar init` should print the bilingual review hint after writing .iar.toml."""
    repo_path = _init_git_repository(tmp_path, "target")
    monkeypatch.chdir(repo_path)
    monkeypatch.setenv("IAR_CONFIG", str(_create_isolated_config(tmp_path)))

    assert main(["init"]) == 0

    captured = capsys.readouterr()
    assert "请检查" in captured.err
    assert "Please review verification_commands" in captured.err
    assert "git diff --check" in captured.err


def _create_isolated_config(tmp_path: Path) -> Path:
    """Create a minimal config.toml for tests that touch the global registry."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("[agent_runner]\n", encoding="utf-8")
    return config_path


def test_iar_init_writes_idempotent_and_force_overwrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """iar init should write once, stay idempotent when unchanged, and honor --force."""
    repo_path = _init_git_repository(tmp_path, "target")
    monkeypatch.chdir(repo_path)
    monkeypatch.setenv("IAR_CONFIG", str(_create_isolated_config(tmp_path)))

    first_exit_code = main(["init"])
    config_path = repo_path / ".iar.toml"
    first_config_text = config_path.read_text(encoding="utf-8")

    # iAR-owned worktree commands must be present and the legacy
    # `just worktree` formula must be gone, so the historical
    # `PosixPath not found` regression cannot return.
    assert "iar worktree create --branch issue-{issue_number}" in first_config_text
    assert "iar worktree path --branch issue-{issue_number}" in first_config_text
    assert "just worktree" not in first_config_text

    second_exit_code = main(["init"])
    protected_config_text = config_path.read_text(encoding="utf-8")

    force_exit_code = main(
        [
            "init",
            "--force",
            "--id",
            "replacement",
            "--display-name",
            "Replacement",
            "--remote",
            "upstream",
            "--base-branch",
            "develop",
        ]
    )
    overwritten_config_text = config_path.read_text(encoding="utf-8")

    assert first_exit_code == 0
    assert "[agent_runner.repository]" in first_config_text
    assert "[agent_runner.git]" in first_config_text
    assert "[agent_runner.runner]" in first_config_text
    assert second_exit_code == 0
    assert protected_config_text == first_config_text
    assert force_exit_code == 0
    assert 'id = "replacement"' in overwritten_config_text
    assert 'display_name = "Replacement"' in overwritten_config_text
    assert 'remote = "upstream"' in overwritten_config_text
    assert 'base_branch = "develop"' in overwritten_config_text


def test_iar_init_protects_diverged_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """iar init should fail without --force when the existing config diverged."""
    repo_path = _init_git_repository(tmp_path, "target")
    monkeypatch.chdir(repo_path)
    monkeypatch.setenv("IAR_CONFIG", str(_create_isolated_config(tmp_path)))

    assert main(["init"]) == 0
    config_path = repo_path / ".iar.toml"
    config_path.write_text(config_path.read_text(encoding="utf-8") + "\n# extra", encoding="utf-8")

    assert main(["init"]) == 1
    assert "# extra" in config_path.read_text(encoding="utf-8")


def test_detect_verification_commands_without_pyproject(tmp_path: Path) -> None:
    """Non-Python repositories should only get the safe git baseline."""
    repo_path = _init_git_repository(tmp_path, "target")
    assert detect_verification_commands(repo_path) == ["git diff --check"]


def test_detect_verification_commands_mkdocs_in_dev_extra(tmp_path: Path) -> None:
    """mkdocs declared as an optional extra needs `uv run --extra <name>`."""
    repo_path = _init_git_repository(tmp_path, "target")
    (repo_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "target"',
                'version = "0.1.0"',
                "dependencies = []",
                "",
                "[project.optional-dependencies]",
                'dev = ["mkdocs>=1.6.1", "pytest>=8.3.0"]',
            ]
        ),
        encoding="utf-8",
    )
    (repo_path / "mkdocs.yml").write_text("site_name: target\n", encoding="utf-8")
    (repo_path / "tests").mkdir()

    assert detect_verification_commands(repo_path) == [
        "git diff --check",
        "uv run --extra dev mkdocs build",
        "uv run --extra dev pytest -q",
    ]


def test_detect_verification_commands_main_dep_and_named_group(
    tmp_path: Path,
) -> None:
    """Main dependencies need no flag; non-default groups need `--group`."""
    repo_path = _init_git_repository(tmp_path, "target")
    (repo_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "target"',
                'version = "0.1.0"',
                'dependencies = ["mkdocs>=1.6.1"]',
                "",
                "[dependency-groups]",
                'qa = ["pytest>=8.3.0"]',
            ]
        ),
        encoding="utf-8",
    )
    (repo_path / "mkdocs.yml").write_text("site_name: target\n", encoding="utf-8")
    (repo_path / "tests").mkdir()

    assert detect_verification_commands(repo_path) == [
        "git diff --check",
        "uv run mkdocs build",
        "uv run --group qa pytest -q",
    ]


def test_detect_verification_commands_prefers_just_test_recipe(
    tmp_path: Path,
) -> None:
    """A justfile ``test`` recipe is preferred over a bare ``pytest -q``.

    ``just test`` runs the same lint/format/test gate that pre-commit enforces
    at ``git commit``, keeping the runner's verification aligned with the commit
    gate so a commit cannot fail pre-commit after verification already passed.
    """
    repo_path = _init_git_repository(tmp_path, "target")
    (repo_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "target"',
                'version = "0.1.0"',
                'dependencies = ["mkdocs>=1.6.1"]',
                "",
                "[dependency-groups]",
                'qa = ["pytest>=8.3.0"]',
            ]
        ),
        encoding="utf-8",
    )
    (repo_path / "mkdocs.yml").write_text("site_name: target\n", encoding="utf-8")
    (repo_path / "tests").mkdir()
    # ``test_setup :=`` and the ``test_`` prefix must not be mistaken for a
    # ``test`` recipe header.
    (repo_path / "justfile").write_text(
        "\n".join(
            [
                'test_setup := "x"',
                "",
                "test:",
                "    uv run pytest -q",
                "",
                "lint:",
                "    uv run ruff check .",
            ]
        ),
        encoding="utf-8",
    )

    assert detect_verification_commands(repo_path) == [
        "git diff --check",
        "uv run mkdocs build",
        "just test",
    ]


def test_detect_verification_commands_justfile_without_test_recipe_uses_pytest(
    tmp_path: Path,
) -> None:
    """A justfile lacking a ``test`` recipe falls back to ``pytest -q``."""
    repo_path = _init_git_repository(tmp_path, "target")
    (repo_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "target"',
                'version = "0.1.0"',
                'dependencies = ["pytest>=8.3.0"]',
            ]
        ),
        encoding="utf-8",
    )
    (repo_path / "tests").mkdir()
    (repo_path / "justfile").write_text("lint:\n    uv run ruff check .\n", encoding="utf-8")

    assert detect_verification_commands(repo_path) == [
        "git diff --check",
        "uv run pytest -q",
    ]


def test_detect_verification_commands_follows_justfile_import(
    tmp_path: Path,
) -> None:
    """A ``test`` recipe in an imported ``justfile.shared`` is detected.

    Reproduces the shared-template layout: the top-level justfile only imports
    ``justfile.shared``, where a quiet ``@test`` recipe lives. Earlier detection
    missed both the ``import`` and the ``@`` prefix and wrongly fell back to a
    bare ``pytest -q``, which then deadlocked against the check-test-flag hook.
    """
    repo_path = _init_git_repository(tmp_path, "target")
    (repo_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "target"',
                'version = "0.1.0"',
                'dependencies = ["pytest>=8.3.0"]',
            ]
        ),
        encoding="utf-8",
    )
    (repo_path / "tests").mkdir()
    (repo_path / "justfile").write_text("import 'justfile.shared'\n", encoding="utf-8")
    (repo_path / "justfile.shared").write_text(
        '@test type="local":\n    uv run pytest -q\n', encoding="utf-8"
    )

    assert detect_verification_commands(repo_path) == [
        "git diff --check",
        "just test",
    ]


def test_detect_verification_commands_generic_fallback_adds_precommit(
    tmp_path: Path,
) -> None:
    """Without a ``just test`` recipe, a pre-commit config adds ``pre-commit run``."""
    repo_path = _init_git_repository(tmp_path, "target")
    (repo_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "target"',
                'version = "0.1.0"',
                'dependencies = ["pre-commit>=3.7.0", "pytest>=8.3.0"]',
            ]
        ),
        encoding="utf-8",
    )
    (repo_path / "tests").mkdir()
    (repo_path / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: ruff\n        name: ruff\n",
        encoding="utf-8",
    )

    assert detect_verification_commands(repo_path) == [
        "git diff --check",
        "uv run pre-commit run --all-files",
        "uv run pytest -q",
    ]


def test_detect_verification_commands_skips_precommit_with_check_test_flag(
    tmp_path: Path,
) -> None:
    """A ``check-test-flag`` gate without ``just test`` must not add ``pre-commit run``.

    The hook only accepts a marker written by ``just test``; emitting a bare
    ``pre-commit run`` would deadlock the runner's commit, so only pytest is kept.
    """
    repo_path = _init_git_repository(tmp_path, "target")
    (repo_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "target"',
                'version = "0.1.0"',
                'dependencies = ["pre-commit>=3.7.0", "pytest>=8.3.0"]',
            ]
        ),
        encoding="utf-8",
    )
    (repo_path / "tests").mkdir()
    (repo_path / ".pre-commit-config.yaml").write_text(
        "\n".join(
            [
                "repos:",
                "  - repo: local",
                "    hooks:",
                "      - id: check-test-flag",
                "        name: Check just test flag",
                "        entry: bash scripts/shared/hooks/check_test_flag.sh",
                "        language: system",
            ]
        ),
        encoding="utf-8",
    )

    assert detect_verification_commands(repo_path) == [
        "git diff --check",
        "uv run pytest -q",
    ]


def test_detect_verification_commands_skips_undeclared_tools(
    tmp_path: Path,
) -> None:
    """mkdocs.yml or tests/ without the matching dependency adds no command."""
    repo_path = _init_git_repository(tmp_path, "target")
    (repo_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "target"',
                'version = "0.1.0"',
                "dependencies = []",
            ]
        ),
        encoding="utf-8",
    )
    (repo_path / "mkdocs.yml").write_text("site_name: target\n", encoding="utf-8")
    (repo_path / "tests").mkdir()

    assert detect_verification_commands(repo_path) == ["git diff --check"]


def test_iar_init_renders_detected_commands_and_validation_section(
    tmp_path: Path,
) -> None:
    """The rendered template carries detected commands and the validation gate."""
    repo_path = _init_git_repository(tmp_path, "target")
    (repo_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "target"',
                'version = "0.1.0"',
                "dependencies = []",
                "",
                "[project.optional-dependencies]",
                'dev = ["mkdocs>=1.6.1"]',
            ]
        ),
        encoding="utf-8",
    )
    (repo_path / "mkdocs.yml").write_text("site_name: target\n", encoding="utf-8")

    _, config_text, verification_commands = build_repository_local_config_text(
        RepositoryInitOptions(cwd=repo_path, dry_run=True)
    )

    assert "uv run --extra dev mkdocs build" in config_text
    assert "[agent_runner.validation]" in config_text
    assert "enabled = true" in config_text
    assert 'evidence_dir = ".iar/evidence"' in config_text
    assert verification_commands == [
        "git diff --check",
        "uv run --extra dev mkdocs build",
    ]


def test_iar_init_renders_interactive_decision_and_deliberation_sections(
    tmp_path: Path,
) -> None:
    """The rendered .iar.toml template includes ask and deliberate config."""
    repo_path = _init_git_repository(tmp_path, "target")
    _, config_text, _ = build_repository_local_config_text(
        RepositoryInitOptions(cwd=repo_path, dry_run=True)
    )

    assert "[agent_runner.interactive_decision]" in config_text
    assert 'default_agent = "claude"' in config_text
    assert 'default_output_dir = "logs/agent-runner/decisions"' in config_text
    assert "[agent_runner.deliberation]" in config_text
    assert "default_rounds = 2" in config_text
    assert 'default_synthesizer = "claude"' in config_text
    assert "[agent_runner.deliberation.profiles.architect]" in config_text
    assert "[agent_runner.deliberation.profiles.skeptic]" in config_text
    assert "[agent_runner.deliberation.profiles.implementer]" in config_text


def test_detect_default_remote_falls_back_when_upstream_missing(
    tmp_path: Path,
) -> None:
    """A stale branch upstream remote that no longer exists should fall back."""
    repo_path = _init_git_repository(tmp_path, "target")
    _run_git(repo_path, "config", "branch.main.remote", "zata")

    remote = _detect_default_remote(repo_path, None)

    assert remote == "origin"


def test_iar_init_registers_repository_in_global_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """iar init should add the current repository to the global registry."""
    repo_path = _init_git_repository(tmp_path, "target")
    monkeypatch.chdir(repo_path)
    config_path = _create_isolated_config(tmp_path)
    monkeypatch.setenv("IAR_CONFIG", str(config_path))

    assert main(["init"]) == 0

    config_text = config_path.read_text(encoding="utf-8")
    assert "[agent_runner.repositories.target]" in config_text
    assert f'path = "{repo_path}"' in config_text
    assert "enabled = true" in config_text
    assert 'display_name = "target"' in config_text

    # A second init with an unchanged config must stay idempotent.
    assert main(["init"]) == 0
    second_text = config_path.read_text(encoding="utf-8")
    assert second_text.count("[agent_runner.repositories.target]") == 1


def test_iar_init_updates_registry_path_when_repository_moves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """iar init should update the registry path if the same repo_id is reused."""
    old_path = _init_git_repository(tmp_path, "old-target")
    new_path = _init_git_repository(tmp_path, "target")
    config_path = _create_isolated_config(tmp_path)
    monkeypatch.setenv("IAR_CONFIG", str(config_path))

    monkeypatch.chdir(old_path)
    assert main(["init"]) == 0
    assert f'path = "{old_path}"' in config_path.read_text(encoding="utf-8")

    monkeypatch.chdir(new_path)
    assert main(["init"]) == 0
    config_text = config_path.read_text(encoding="utf-8")
    assert f'path = "{new_path}"' in config_text
    assert f'path = "{old_path}"' not in config_text
    assert config_text.count("[agent_runner.repositories.target]") == 1


def test_iar_init_does_not_pollute_target_repo_config_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without IAR_CONFIG, iar init must write registry to ~/.iar/config.toml only.

    Regression guard: previously ``create_registry_editor()`` resolved the
    registry path via ``resolve_config_toml_path()``, which walks upward from
    the current working directory and finds the target repository's own
    ``config.toml``. That polluted the target repo with
    ``[agent_runner.repositories.<repo_id>]`` entries.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    repo_path = _init_git_repository(tmp_path, "target")
    repo_config_path = repo_path / "config.toml"
    repo_config_path.write_text(
        '[app]\nname = "target-app"\n',
        encoding="utf-8",
    )
    original_repo_config = repo_config_path.read_text(encoding="utf-8")

    monkeypatch.chdir(repo_path)
    assert main(["init"]) == 0

    # The target repository's application config.toml must remain untouched.
    assert repo_config_path.read_text(encoding="utf-8") == original_repo_config

    # Registry must land in the global IAR config instead.
    global_config_path = fake_home / ".iar" / "config.toml"
    assert global_config_path.is_file()
    global_config_text = global_config_path.read_text(encoding="utf-8")
    assert "[agent_runner.repositories.target]" in global_config_text
    assert f'path = "{repo_path}"' in global_config_text


# ---------------------------------------------------------------------------
# .gitignore sync
# ---------------------------------------------------------------------------


def test_ensure_gitignore_inserts_block_when_missing(tmp_path: Path) -> None:
    """Fresh repo (no .gitignore) should get a managed block with all entries."""
    repo_path = tmp_path / "target"
    repo_path.mkdir()

    result = ensure_gitignore_entries(GitignoreSyncOptions(repo_root_path=repo_path))

    gitignore_path = repo_path / ".gitignore"
    text = gitignore_path.read_text(encoding="utf-8")
    assert result.block_inserted is True
    assert result.block_updated is False
    assert result.entries_added == (".iar/", ".agent-runner/", ".iar-worktrees/")
    assert result.entries_skipped_external == ()
    assert GITIGNORE_BLOCK_HEADER in text
    assert GITIGNORE_BLOCK_FOOTER in text
    assert ".iar/" in text
    assert ".agent-runner/" in text
    assert ".iar-worktrees/" in text


def test_ensure_gitignore_skips_patterns_already_outside_block(
    tmp_path: Path,
) -> None:
    """Patterns already declared outside the block must not be duplicated."""
    repo_path = tmp_path / "target"
    repo_path.mkdir()
    gitignore = repo_path / ".gitignore"
    gitignore.write_text(
        "# project rules\nfoo/\n.agent-runner/\n",
        encoding="utf-8",
    )

    result = ensure_gitignore_entries(GitignoreSyncOptions(repo_root_path=repo_path))

    text = gitignore.read_text(encoding="utf-8")
    assert result.block_inserted is True
    assert result.entries_added == (".iar/", ".iar-worktrees/")
    assert ".agent-runner/" in result.entries_skipped_external
    # Project rules must be preserved verbatim above the block.
    assert text.startswith("# project rules\nfoo/\n.agent-runner/\n")
    # The block must be appended after a blank line.
    assert GITIGNORE_BLOCK_HEADER in text
    # .agent-runner/ must appear exactly once in the file (in the project
    # section, not duplicated inside the block).
    assert text.count(".agent-runner/") == 1


def test_ensure_gitignore_is_idempotent_with_existing_block(tmp_path: Path) -> None:
    """Re-running on a repo with the same block must not modify .gitignore."""
    repo_path = tmp_path / "target"
    repo_path.mkdir()

    ensure_gitignore_entries(GitignoreSyncOptions(repo_root_path=repo_path))
    initial = (repo_path / ".gitignore").read_text(encoding="utf-8")

    result = ensure_gitignore_entries(GitignoreSyncOptions(repo_root_path=repo_path))

    after = (repo_path / ".gitignore").read_text(encoding="utf-8")
    assert result.block_inserted is False
    assert result.block_updated is False
    assert after == initial


def test_ensure_gitignore_updates_block_when_patterns_missing(
    tmp_path: Path,
) -> None:
    """Existing block missing a pattern should be updated in place."""
    repo_path = tmp_path / "target"
    repo_path.mkdir()
    gitignore = repo_path / ".gitignore"
    # Minimal block with only one pattern; the others should be added.
    gitignore.write_text(
        f"{GITIGNORE_BLOCK_HEADER}\n.iar/\n{GITIGNORE_BLOCK_FOOTER}\n",
        encoding="utf-8",
    )

    result = ensure_gitignore_entries(GitignoreSyncOptions(repo_root_path=repo_path))

    text = gitignore.read_text(encoding="utf-8")
    assert result.block_inserted is False
    assert result.block_updated is True
    assert ".agent-runner/" in result.entries_added
    assert ".iar-worktrees/" in result.entries_added
    assert ".agent-runner/" in text
    assert ".iar-worktrees/" in text
    # Header / footer order must be preserved.
    assert text.index(GITIGNORE_BLOCK_HEADER) < text.index(GITIGNORE_BLOCK_FOOTER)


def test_ensure_gitignore_does_not_insert_empty_block_when_all_external(
    tmp_path: Path,
) -> None:
    """If every pattern already lives outside the block, do not insert anything."""
    repo_path = tmp_path / "target"
    repo_path.mkdir()
    gitignore = repo_path / ".gitignore"
    gitignore.write_text(
        ".iar/\n.agent-runner/\n.iar-worktrees/\n",
        encoding="utf-8",
    )

    result = ensure_gitignore_entries(GitignoreSyncOptions(repo_root_path=repo_path))

    text = gitignore.read_text(encoding="utf-8")
    assert result.block_inserted is False
    assert result.block_updated is False
    assert result.entries_added == ()
    assert set(result.entries_skipped_external) == {
        ".iar/",
        ".agent-runner/",
        ".iar-worktrees/",
    }
    assert GITIGNORE_BLOCK_HEADER not in text


def test_ensure_gitignore_skips_when_opted_out(tmp_path: Path) -> None:
    """``skip=True`` must not touch .gitignore (or even create it)."""
    repo_path = tmp_path / "target"
    repo_path.mkdir()

    result = ensure_gitignore_entries(GitignoreSyncOptions(repo_root_path=repo_path, skip=True))

    assert result.skipped is True
    assert result.block_inserted is False
    assert result.block_updated is False
    assert not (repo_path / ".gitignore").exists()


def test_ensure_gitignore_dry_run_does_not_write(tmp_path: Path) -> None:
    """``dry_run=True`` reports the would-be plan but does not write .gitignore."""
    repo_path = tmp_path / "target"
    repo_path.mkdir()

    result = ensure_gitignore_entries(GitignoreSyncOptions(repo_root_path=repo_path, dry_run=True))

    assert result.dry_run is True
    assert result.block_inserted is True
    assert not (repo_path / ".gitignore").exists()


def test_ensure_gitignore_refuses_to_touch_corrupted_block(tmp_path: Path) -> None:
    """A block missing its footer is treated as corrupted and left untouched."""
    repo_path = tmp_path / "target"
    repo_path.mkdir()
    gitignore = repo_path / ".gitignore"
    # Header present, footer missing — must be a no-op so we don't lose data.
    initial = f"{GITIGNORE_BLOCK_HEADER}\n.iar/\n"
    gitignore.write_text(initial, encoding="utf-8")

    result = ensure_gitignore_entries(GitignoreSyncOptions(repo_root_path=repo_path))

    assert result.block_inserted is False
    assert result.block_updated is False
    assert gitignore.read_text(encoding="utf-8") == initial


def test_ensure_gitignore_hints_legacy_info_exclude(tmp_path: Path) -> None:
    """Legacy ``.git/info/exclude`` entries should surface as a hint to the user."""
    repo_path = tmp_path / "target"
    repo_path.mkdir()
    info_dir = repo_path / ".git" / "info"
    info_dir.mkdir(parents=True)
    (info_dir / "exclude").write_text("/.iar/evidence/\n", encoding="utf-8")

    result = ensure_gitignore_entries(GitignoreSyncOptions(repo_root_path=repo_path))

    assert result.info_exclude_hint is True


def test_ensure_gitignore_preserves_postlude_text(tmp_path: Path) -> None:
    """Block replacement must keep trailing content byte-for-byte."""
    repo_path = tmp_path / "target"
    repo_path.mkdir()
    gitignore = repo_path / ".gitignore"
    initial = (
        f"{GITIGNORE_BLOCK_HEADER}\n"
        f".iar/\n"
        f"{GITIGNORE_BLOCK_FOOTER}\n"
        "\n"
        "# trailing section\n"
        "bar/\n"
    )
    gitignore.write_text(initial, encoding="utf-8")

    ensure_gitignore_entries(GitignoreSyncOptions(repo_root_path=repo_path))

    text = gitignore.read_text(encoding="utf-8")
    # Trailing content (after the iar block) must be preserved verbatim.
    assert text.endswith("\n# trailing section\nbar/\n")


def test_iar_init_writes_gitignore_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``iar init`` should add the iar .gitignore block on a fresh repo."""
    repo_path = _init_git_repository(tmp_path, "target")
    monkeypatch.chdir(repo_path)
    monkeypatch.setenv("IAR_CONFIG", str(_create_isolated_config(tmp_path)))

    assert main(["init"]) == 0

    gitignore_text = (repo_path / ".gitignore").read_text(encoding="utf-8")
    assert GITIGNORE_BLOCK_HEADER in gitignore_text
    assert GITIGNORE_BLOCK_FOOTER in gitignore_text
    assert ".iar/" in gitignore_text
    assert ".agent-runner/" in gitignore_text
    assert ".iar-worktrees/" in gitignore_text


def test_iar_init_no_update_gitignore_skips_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-update-gitignore`` should not create .gitignore."""
    repo_path = _init_git_repository(tmp_path, "target")
    monkeypatch.chdir(repo_path)
    monkeypatch.setenv("IAR_CONFIG", str(_create_isolated_config(tmp_path)))

    assert main(["init", "--no-update-gitignore"]) == 0
    assert not (repo_path / ".gitignore").exists()


def test_iar_init_dry_run_does_not_write_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` must not touch .gitignore even when the block would be added."""
    repo_path = _init_git_repository(tmp_path, "target")
    monkeypatch.chdir(repo_path)
    monkeypatch.setenv("IAR_CONFIG", str(_create_isolated_config(tmp_path)))

    assert main(["init", "--dry-run"]) == 0
    assert not (repo_path / ".gitignore").exists()


def test_iar_init_dry_run_emits_gitignore_block_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The dry-run plan should print the managed block header and entries."""
    repo_path = _init_git_repository(tmp_path, "target")
    monkeypatch.chdir(repo_path)
    monkeypatch.setenv("IAR_CONFIG", str(_create_isolated_config(tmp_path)))

    assert main(["init", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert GITIGNORE_BLOCK_HEADER in out
    assert ".iar/" in out
    assert ".agent-runner/" in out
    assert ".iar-worktrees/" in out
    assert GITIGNORE_BLOCK_FOOTER in out


def test_iar_init_idempotent_does_not_rewrite_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running ``iar init`` twice must not change .gitignore after the first write."""
    repo_path = _init_git_repository(tmp_path, "target")
    monkeypatch.chdir(repo_path)
    monkeypatch.setenv("IAR_CONFIG", str(_create_isolated_config(tmp_path)))

    assert main(["init"]) == 0
    first = (repo_path / ".gitignore").read_text(encoding="utf-8")

    assert main(["init"]) == 0
    second = (repo_path / ".gitignore").read_text(encoding="utf-8")

    assert first == second
