"""Regression tests for the task archive hook."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_TASKS_SCRIPT_PATH = REPO_ROOT / "hooks" / "archive_tasks.py"


def load_archive_tasks_module() -> ModuleType:
    """Load the archive_tasks hook module directly from disk.

    Returns:
        ModuleType: Loaded hook module.
    """

    module_spec = importlib.util.spec_from_file_location(
        "archive_tasks_hook", ARCHIVE_TASKS_SCRIPT_PATH
    )
    assert module_spec is not None
    assert module_spec.loader is not None
    archive_tasks_module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(archive_tasks_module)
    return archive_tasks_module


def run_command(
    command_parts: list[str], cwd_path: Path
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command with UTF-8 output handling.

    Args:
        command_parts: Command and arguments to execute.
        cwd_path: Working directory for the command.

    Returns:
        subprocess.CompletedProcess[str]: Captured subprocess result.
    """

    return subprocess.run(
        command_parts,
        cwd=cwd_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def init_git_repository(tmp_path: Path) -> None:
    """Initialize a temporary Git repository for archive hook tests.

    Args:
        tmp_path: Temporary repository root.
    """

    init_process = run_command(["git", "init", "-q"], cwd_path=tmp_path)
    assert init_process.returncode == 0


def test_staged_task_paths_only_include_root_level_active_prds(tmp_path: Path) -> None:
    """Only root-level active PRDs should be collected for archive."""

    archive_tasks_module = load_archive_tasks_module()
    init_git_repository(tmp_path)

    tasks_dir = tmp_path / "tasks"
    archive_dir = tasks_dir / "archive"
    pending_dir = tasks_dir / "pending"
    nested_dir = tasks_dir / "review"
    archive_dir.mkdir(parents=True, exist_ok=True)
    pending_dir.mkdir(parents=True, exist_ok=True)
    nested_dir.mkdir(parents=True, exist_ok=True)

    active_prd_path = tasks_dir / "20260413-100000-prd-active.md"
    pending_prd_path = pending_dir / "20260413-100001-prd-pending.md"
    archived_prd_path = archive_dir / "20260413-100002-prd-archived.md"
    nested_prd_path = nested_dir / "20260413-100003-prd-nested.md"

    active_prd_path.write_text("# active\n", encoding="utf-8")
    pending_prd_path.write_text("# pending\n", encoding="utf-8")
    archived_prd_path.write_text("# archived\n", encoding="utf-8")
    nested_prd_path.write_text("# nested\n", encoding="utf-8")

    add_process = run_command(["git", "add", "tasks"], cwd_path=tmp_path)
    assert add_process.returncode == 0

    staged_task_paths = archive_tasks_module._staged_task_paths(
        repo_root=tmp_path,
        tasks_dir=tasks_dir,
        archive_dir=archive_dir,
        pending_dir=pending_dir,
    )

    assert staged_task_paths == [active_prd_path]


def test_staged_task_paths_ignore_non_markdown_root_files(tmp_path: Path) -> None:
    """Non-Markdown root files should not be archived."""

    archive_tasks_module = load_archive_tasks_module()
    init_git_repository(tmp_path)

    tasks_dir = tmp_path / "tasks"
    archive_dir = tasks_dir / "archive"
    pending_dir = tasks_dir / "pending"
    archive_dir.mkdir(parents=True, exist_ok=True)
    pending_dir.mkdir(parents=True, exist_ok=True)

    notes_text_path = tasks_dir / "notes.txt"
    notes_text_path.write_text("ignore me\n", encoding="utf-8")

    add_process = run_command(["git", "add", "tasks"], cwd_path=tmp_path)
    assert add_process.returncode == 0

    staged_task_paths = archive_tasks_module._staged_task_paths(
        repo_root=tmp_path,
        tasks_dir=tasks_dir,
        archive_dir=archive_dir,
        pending_dir=pending_dir,
    )

    assert staged_task_paths == []
