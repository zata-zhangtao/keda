"""Regression tests for the template sync script."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_TEMPLATE_SCRIPT_PATH = REPO_ROOT / "scripts" / "template" / "sync_template.sh"


def run_command(
    command_parts: list[str], cwd_path: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command with captured UTF-8 output."""

    return subprocess.run(
        command_parts,
        cwd=cwd_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def write_text_file(repo_path: Path, relative_path: str, content: str) -> None:
    """Create or overwrite a UTF-8 text file inside a repository."""

    file_path = repo_path / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def init_git_repo(repo_path: Path, *, commit_all: bool) -> None:
    """Initialize a git repository and optionally commit current files."""

    init_process = run_command(["git", "init", "-b", "main"], cwd_path=repo_path)
    assert init_process.returncode == 0, init_process.stderr

    config_name_process = run_command(
        ["git", "config", "user.name", "Codex Test"],
        cwd_path=repo_path,
    )
    assert config_name_process.returncode == 0, config_name_process.stderr

    config_email_process = run_command(
        ["git", "config", "user.email", "codex-tests@example.com"],
        cwd_path=repo_path,
    )
    assert config_email_process.returncode == 0, config_email_process.stderr

    if commit_all:
        add_process = run_command(["git", "add", "."], cwd_path=repo_path)
        assert add_process.returncode == 0, add_process.stderr
        commit_process = run_command(
            ["git", "commit", "-m", "Initial commit"],
            cwd_path=repo_path,
        )
        assert commit_process.returncode == 0, commit_process.stderr


def create_repo(
    repo_path: Path, file_contents_by_path: dict[str, str], *, commit_all: bool
) -> None:
    """Create a git repository populated with the provided files."""

    repo_path.mkdir(parents=True, exist_ok=True)
    for relative_path, content in file_contents_by_path.items():
        write_text_file(repo_path, relative_path, content)
    init_git_repo(repo_path, commit_all=commit_all)


def run_sync_template(
    project_repo_path: Path,
    template_repo_path: Path,
    *extra_args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the sync script in list-only mode against a local template repository."""

    environment = os.environ.copy()
    environment["SYNC_TEMPLATE_LIST_ONLY"] = "1"
    environment["SYNC_TEMPLATE_TEMPLATE_REPO"] = str(template_repo_path)
    if extra_env is not None:
        environment.update(extra_env)
    return run_command(
        ["bash", str(SYNC_TEMPLATE_SCRIPT_PATH), *extra_args],
        cwd_path=project_repo_path,
        env=environment,
    )


def test_sync_template_skips_configured_project_paths_by_default(
    tmp_path: Path,
) -> None:
    """Default sync listing should ignore configured project-specific paths."""

    template_repo_path = tmp_path / "template-repo"
    project_repo_path = tmp_path / "project-repo"

    template_files = {
        "README.md": "Template README\n",
        "scripts/tool.sh": "echo template\n",
        "src/backend/api/api.py": "print('template backend')\n",
        "frontend/src/App.tsx": "export const App = 'template';\n",
        "infra/main.tf": "# template infra\n",
        "deploy/prod.yml": "name: template deploy\n",
    }
    project_files = {
        "README.md": "Project README\n",
        "scripts/tool.sh": "echo project\n",
        "src/backend/api/api.py": "print('project backend')\n",
        "frontend/src/App.tsx": "export const App = 'project';\n",
        "infra/main.tf": "# project infra\n",
        "deploy/prod.yml": "name: project deploy\n",
    }

    create_repo(template_repo_path, template_files, commit_all=True)
    create_repo(project_repo_path, project_files, commit_all=False)

    completed_process = run_sync_template(project_repo_path, template_repo_path)

    assert completed_process.returncode == 0, completed_process.stderr
    assert "Found 1 changed + 0 new entry/entries." in completed_process.stdout
    assert "CHANGED\tscripts/tool.sh" in completed_process.stdout
    assert "README.md" not in completed_process.stdout
    assert "src/backend/api/api.py" not in completed_process.stdout
    assert "frontend/src/App.tsx" not in completed_process.stdout
    assert "infra/main.tf" not in completed_process.stdout
    assert "deploy/prod.yml" not in completed_process.stdout


def test_sync_template_all_includes_configured_project_paths(tmp_path: Path) -> None:
    """The --all mode should include configured project-specific paths."""

    template_repo_path = tmp_path / "template-repo"
    project_repo_path = tmp_path / "project-repo"

    template_files = {
        "README.md": "Template README\n",
        "scripts/tool.sh": "echo template\n",
        "src/backend/api/api.py": "print('template backend')\n",
        "frontend/src/App.tsx": "export const App = 'template';\n",
        "infra/main.tf": "# template infra\n",
        "deploy/prod.yml": "name: template deploy\n",
    }
    project_files = {
        "README.md": "Project README\n",
        "scripts/tool.sh": "echo project\n",
        "src/backend/api/api.py": "print('project backend')\n",
        "frontend/src/App.tsx": "export const App = 'project';\n",
        "infra/main.tf": "# project infra\n",
        "deploy/prod.yml": "name: project deploy\n",
    }

    create_repo(template_repo_path, template_files, commit_all=True)
    create_repo(project_repo_path, project_files, commit_all=False)

    completed_process = run_sync_template(
        project_repo_path,
        template_repo_path,
        "--all",
    )

    assert completed_process.returncode == 0, completed_process.stderr
    assert "Found 6 changed + 0 new entry/entries." in completed_process.stdout
    assert "CHANGED\tREADME.md" in completed_process.stdout
    assert "CHANGED\tscripts/tool.sh" in completed_process.stdout
    assert "CHANGED\tsrc/backend/api/api.py" in completed_process.stdout
    assert "CHANGED\tfrontend/src/App.tsx" in completed_process.stdout
    assert "CHANGED\tinfra/main.tf" in completed_process.stdout
    assert "CHANGED\tdeploy/prod.yml" in completed_process.stdout


def test_sync_template_uses_project_skip_paths_from_config(tmp_path: Path) -> None:
    """Project config should control default project path skips."""

    template_repo_path = tmp_path / "template-repo"
    project_repo_path = tmp_path / "project-repo"

    template_files = {
        "scripts/tool.sh": "echo template\n",
        "src/backend/api/api.py": "print('template backend')\n",
        "frontend/src/App.tsx": "export const App = 'template';\n",
    }
    project_files = {
        "config.toml": '[template_sync]\nproject_skip_paths = ["src/backend/"]\n',
        "scripts/tool.sh": "echo project\n",
        "src/backend/api/api.py": "print('project backend')\n",
        "frontend/src/App.tsx": "export const App = 'project';\n",
    }

    create_repo(template_repo_path, template_files, commit_all=True)
    create_repo(project_repo_path, project_files, commit_all=False)

    completed_process = run_sync_template(project_repo_path, template_repo_path)

    assert completed_process.returncode == 0, completed_process.stderr
    assert "Found 2 changed + 0 new entry/entries." in completed_process.stdout
    assert "CHANGED\tscripts/tool.sh" in completed_process.stdout
    assert "CHANGED\tfrontend/src/App.tsx" in completed_process.stdout
    assert "src/backend/api/api.py" not in completed_process.stdout


def test_sync_template_project_include_paths_override_project_skips(
    tmp_path: Path,
) -> None:
    """Configured include paths should keep selected project paths visible."""

    template_repo_path = tmp_path / "template-repo"
    project_repo_path = tmp_path / "project-repo"

    template_files = {
        "scripts/tool.sh": "echo template\n",
        "src/backend/api/api.py": "print('template backend')\n",
        "frontend/src/App.tsx": "export const App = 'template';\n",
    }
    project_files = {
        "config.toml": (
            "[template_sync]\n"
            'project_skip_paths = ["src/backend/", "frontend/"]\n'
            'project_include_paths = ["frontend/"]\n'
        ),
        "scripts/tool.sh": "echo project\n",
        "src/backend/api/api.py": "print('project backend')\n",
        "frontend/src/App.tsx": "export const App = 'project';\n",
    }

    create_repo(template_repo_path, template_files, commit_all=True)
    create_repo(project_repo_path, project_files, commit_all=False)

    completed_process = run_sync_template(project_repo_path, template_repo_path)

    assert completed_process.returncode == 0, completed_process.stderr
    assert "Found 2 changed + 0 new entry/entries." in completed_process.stdout
    assert "CHANGED\tscripts/tool.sh" in completed_process.stdout
    assert "CHANGED\tfrontend/src/App.tsx" in completed_process.stdout
    assert "src/backend/api/api.py" not in completed_process.stdout


def test_sync_template_project_skip_paths_can_be_overridden_by_env(
    tmp_path: Path,
) -> None:
    """Environment overrides should support one-off project skip changes."""

    template_repo_path = tmp_path / "template-repo"
    project_repo_path = tmp_path / "project-repo"

    template_files = {
        "scripts/tool.sh": "echo template\n",
        "src/backend/api/api.py": "print('template backend')\n",
        "frontend/src/App.tsx": "export const App = 'template';\n",
    }
    project_files = {
        "config.toml": '[template_sync]\nproject_skip_paths = ["src/backend/"]\n',
        "scripts/tool.sh": "echo project\n",
        "src/backend/api/api.py": "print('project backend')\n",
        "frontend/src/App.tsx": "export const App = 'project';\n",
    }

    create_repo(template_repo_path, template_files, commit_all=True)
    create_repo(project_repo_path, project_files, commit_all=False)

    completed_process = run_sync_template(
        project_repo_path,
        template_repo_path,
        extra_env={"SYNC_TEMPLATE_PROJECT_SKIP_PATHS": "frontend/"},
    )

    assert completed_process.returncode == 0, completed_process.stderr
    assert "Found 2 changed + 0 new entry/entries." in completed_process.stdout
    assert "CHANGED\tscripts/tool.sh" in completed_process.stdout
    assert "CHANGED\tsrc/backend/api/api.py" in completed_process.stdout
    assert "frontend/src/App.tsx" not in completed_process.stdout
