"""Tests for ``iar workflow install`` engine and CLI surface."""

from __future__ import annotations

import stat
from importlib.resources import files
from pathlib import Path

import pytest

from backend.engines.agent_runner.repository_local import (
    IARRepositoryNotInitializedError,
)
from backend.engines.agent_runner.workflow_install import (
    ExistingFileRefusedError,
    TEMPLATE_PACKAGE_NAME,
    UnknownWorkflowError,
    WorkflowInstallOptions,
    WorkflowInstallResult,
    install_workflow,
    render_preview_placeholder_section,
)
from backend.infrastructure.config.settings import PreviewSettings
from backend.infrastructure.process_runner import CommandResult
from tests.conftest import FakeProcessRunner


_EXPECTED_FILES = (
    ".github/workflows/deploy-preview.yml",
    "deploy/vps-traefik/README.md",
    "deploy/vps-traefik/deploy-preview.sh",
    "deploy/vps-traefik/docker-compose.preview.yml",
    "deploy/vps-traefik/preview.env.example",
    "scripts/preview_env.py",
    "scripts/provision_preview_server.py",
)


def _init_iar(repo_root: Path) -> None:
    """Write a minimal valid ``.iar.toml`` + ``config.toml`` into ``repo_root``."""
    iar_path = repo_root / ".iar.toml"
    iar_path.write_text(
        "[agent_runner.repository]\n" 'id = "sample"\n' "enabled = true\n",
        encoding="utf-8",
    )
    (repo_root / "config.toml").write_text("", encoding="utf-8")


def _make_fake_runner(repo_root: Path) -> FakeProcessRunner:
    """Return a FakeProcessRunner that pretends ``repo_root`` is a git repo."""
    return FakeProcessRunner(
        responses={
            ("git", "rev-parse", "--show-toplevel"): CommandResult(
                command=("git", "rev-parse", "--show-toplevel"),
                return_code=0,
                stdout=str(repo_root),
                stderr="",
            ),
        }
    )


def test_template_package_lists_preview() -> None:
    """The bundled templates package must expose a ``preview`` subdirectory."""
    template_root = files(TEMPLATE_PACKAGE_NAME)
    assert (template_root / "preview").is_dir()


def test_template_files_byte_match_source() -> None:
    """Bundled template files must be byte-identical to the source files."""
    repo_root = Path(__file__).resolve().parents[1]
    template_root = files(TEMPLATE_PACKAGE_NAME) / "preview"
    for relative_posix_path in _EXPECTED_FILES:
        bundled_bytes = (template_root / relative_posix_path).read_bytes()
        source_bytes = (repo_root / relative_posix_path).read_bytes()
        assert bundled_bytes == source_bytes, relative_posix_path


def test_deploy_preview_sh_keeps_executable_bit() -> None:
    """The bundled deploy-preview.sh must keep its 0755 permission."""
    template_root = files(TEMPLATE_PACKAGE_NAME) / "preview"
    script_path = template_root / "deploy/vps-traefik/deploy-preview.sh"
    mode = script_path.stat().st_mode
    assert mode & stat.S_IXUSR
    assert mode & stat.S_IXGRP
    assert mode & stat.S_IXOTH


def test_preview_placeholder_section_uses_settings_fields() -> None:
    """The placeholder section must derive its field set from PreviewSettings."""
    rendered_text = render_preview_placeholder_section()
    for field_name in PreviewSettings.model_fields:
        assert f"{field_name} =" in rendered_text, field_name


def test_preview_placeholder_section_keeps_bool_default() -> None:
    """The bool field ``enabled`` must keep its schema default (a bool, not "<set-me>")."""
    rendered_text = render_preview_placeholder_section()
    assert "enabled = true" in rendered_text or "enabled = false" in rendered_text
    assert 'enabled = "<set-me>"' not in rendered_text


def test_install_workflow_writes_seven_files(tmp_path: Path) -> None:
    """``install_workflow`` must write all seven template files byte-identically."""
    _init_iar(tmp_path)
    fake_runner = _make_fake_runner(tmp_path)

    result = install_workflow(
        WorkflowInstallOptions(cwd=tmp_path, name="preview"),
        process_runner=fake_runner,
    )

    assert isinstance(result, WorkflowInstallResult)
    assert not result.dry_run
    repo_root = Path(result.repo_root_path)
    for relative_posix_path in _EXPECTED_FILES:
        target = repo_root / relative_posix_path
        assert target.is_file(), relative_posix_path
        assert (
            target.read_bytes()
            == (
                files(TEMPLATE_PACKAGE_NAME) / "preview" / relative_posix_path
            ).read_bytes()
        )
    assert result.config_toml_plan is not None
    assert result.config_toml_plan.will_write_new_section


def test_install_workflow_preserves_executable_bit(tmp_path: Path) -> None:
    """deploy-preview.sh must keep 0755 after install."""
    _init_iar(tmp_path)
    fake_runner = _make_fake_runner(tmp_path)
    install_workflow(
        WorkflowInstallOptions(cwd=tmp_path, name="preview"),
        process_runner=fake_runner,
    )
    script_path = tmp_path / "deploy" / "vps-traefik" / "deploy-preview.sh"
    mode = script_path.stat().st_mode
    assert mode & stat.S_IXUSR
    assert mode & stat.S_IXGRP
    assert mode & stat.S_IXOTH
    assert oct(mode & 0o777) == oct(0o755)


def test_install_workflow_appends_preview_section(tmp_path: Path) -> None:
    """After install, config.toml must end with the ``[preview]`` placeholder."""
    _init_iar(tmp_path)
    fake_runner = _make_fake_runner(tmp_path)
    install_workflow(
        WorkflowInstallOptions(cwd=tmp_path, name="preview"),
        process_runner=fake_runner,
    )
    config_text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "[preview]" in config_text
    for field_name in PreviewSettings.model_fields:
        assert f"{field_name} = " in config_text, field_name


def test_install_workflow_dry_run_writes_nothing(tmp_path: Path) -> None:
    """``--dry-run`` must not touch the filesystem but must return plans."""
    _init_iar(tmp_path)
    config_text_before = tmp_path / "config.toml"
    config_text_before.write_text("", encoding="utf-8")
    config_hash_before = config_text_before.read_bytes()
    fake_runner = _make_fake_runner(tmp_path)

    result = install_workflow(
        WorkflowInstallOptions(cwd=tmp_path, name="preview", dry_run=True),
        process_runner=fake_runner,
    )

    assert result.dry_run
    for relative_posix_path in _EXPECTED_FILES:
        assert not (tmp_path / relative_posix_path).exists()
    assert config_text_before.read_bytes() == config_hash_before
    assert {plan.source_relative_path for plan in result.template_file_plans} == set(
        _EXPECTED_FILES
    )


def test_install_workflow_refuses_existing_files(tmp_path: Path) -> None:
    """Existing target files must be refused unless ``force`` is set."""
    _init_iar(tmp_path)
    fake_runner = _make_fake_runner(tmp_path)
    install_workflow(
        WorkflowInstallOptions(cwd=tmp_path, name="preview"),
        process_runner=fake_runner,
    )

    with pytest.raises(ExistingFileRefusedError) as exc_info:
        install_workflow(
            WorkflowInstallOptions(cwd=tmp_path, name="preview"),
            process_runner=fake_runner,
        )
    assert len(exc_info.value.refused_paths) == len(_EXPECTED_FILES)


def test_install_workflow_force_overwrites(tmp_path: Path) -> None:
    """``--force`` must overwrite existing template files and ``[preview]``."""
    _init_iar(tmp_path)
    fake_runner = _make_fake_runner(tmp_path)
    install_workflow(
        WorkflowInstallOptions(cwd=tmp_path, name="preview"),
        process_runner=fake_runner,
    )

    readme_path = tmp_path / "deploy" / "vps-traefik" / "README.md"
    readme_path.write_text("stale marker", encoding="utf-8")

    result = install_workflow(
        WorkflowInstallOptions(cwd=tmp_path, name="preview", force=True),
        process_runner=fake_runner,
    )

    assert not readme_path.read_text(encoding="utf-8").startswith("stale marker")
    assert result.config_toml_plan is not None
    assert result.config_toml_plan.will_overwrite_preview_section


def test_install_workflow_unknown_name_raises(tmp_path: Path) -> None:
    """Unknown workflow names must raise ``UnknownWorkflowError``."""
    _init_iar(tmp_path)
    fake_runner = _make_fake_runner(tmp_path)
    with pytest.raises(UnknownWorkflowError):
        install_workflow(
            WorkflowInstallOptions(cwd=tmp_path, name="does-not-exist"),
            process_runner=fake_runner,
        )


def test_install_workflow_requires_iar_init(tmp_path: Path) -> None:
    """Missing ``.iar.toml`` must raise ``IARRepositoryNotInitializedError``."""
    fake_runner = _make_fake_runner(tmp_path)
    with pytest.raises(IARRepositoryNotInitializedError):
        install_workflow(
            WorkflowInstallOptions(cwd=tmp_path, name="preview"),
            process_runner=fake_runner,
        )


def test_install_workflow_toml_parse_failure_does_not_block_files(
    tmp_path: Path, caplog
) -> None:
    """A broken config.toml must not prevent the seven files from being written."""
    _init_iar(tmp_path)
    fake_runner = _make_fake_runner(tmp_path)
    (tmp_path / "config.toml").write_text("this is not [valid\n", encoding="utf-8")

    import logging

    with caplog.at_level(logging.WARNING):
        result = install_workflow(
            WorkflowInstallOptions(cwd=tmp_path, name="preview"),
            process_runner=fake_runner,
        )

    for relative_posix_path in _EXPECTED_FILES:
        assert (tmp_path / relative_posix_path).is_file()
    assert result.config_toml_plan is not None
    assert result.config_toml_plan.parse_failed
    assert any("config.toml 解析失败" in record.message for record in caplog.records)


def test_install_workflow_skips_existing_preview_section(
    tmp_path: Path,
) -> None:
    """An existing ``[preview]`` section must be left alone without ``--force``."""
    _init_iar(tmp_path)
    fake_runner = _make_fake_runner(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[preview]\nenabled = true\nbase_domain = "existing.example"\n',
        encoding="utf-8",
    )

    result = install_workflow(
        WorkflowInstallOptions(cwd=tmp_path, name="preview"),
        process_runner=fake_runner,
    )

    config_text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'base_domain = "existing.example"' in config_text
    assert result.config_toml_plan is not None
    assert result.config_toml_plan.present_existing
    assert not result.config_toml_plan.will_overwrite_preview_section
    assert not result.config_toml_plan.will_write_new_section
