"""Workflow template installation for issue-agent-runner.

This module provides the engine-layer implementation of
``iar workflow install <name>``. Templates are bundled with the Python
package as data files and copied into the current Git repository at install
time. ``config.toml`` is appended with a ``[preview]`` placeholder section
whose field names are derived from
``backend.infrastructure.config.settings.PreviewSettings.model_fields``
to avoid duplicating the field list in this module.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Iterable

import tomlkit
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from backend.infrastructure.config.settings import PreviewSettings
from backend.infrastructure.logging.logger import Logger
from backend.infrastructure.process_runner import SubprocessRunner

from .repository_local import (
    detect_git_repository_root,
    require_iar_repository_initialized,
)

logger = Logger()


TEMPLATE_PACKAGE_NAME = "backend.engines.agent_runner.templates"
"""Package holding bundled workflow templates; see ``pyproject.toml``."""

_PREVIEW_SECTION_HEADER = (
    "Preview deployment (placeholder values — see deploy/vps-traefik/README.md)"
)

_EXECUTABLE_RELATIVE_PATHS: frozenset[str] = frozenset({"deploy/vps-traefik/deploy-preview.sh"})


@dataclass(frozen=True)
class WorkflowInstallOptions:
    """Options for installing a bundled workflow template.

    Attributes:
        cwd: Working directory used as the Git root probe seed.
        name: Workflow template name (e.g. ``"preview"``); must match a
            directory under ``backend.engines.agent_runner.templates``.
        force: Overwrite existing template files and replace the existing
            ``[preview]`` section in ``config.toml``.
        dry_run: Print the would-write plan without touching the filesystem.
    """

    cwd: Path
    name: str
    force: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class TemplateFilePlan:
    """Per-file write plan emitted by the install engine."""

    source_relative_path: str
    target_path: Path
    bytes_to_write: int
    exists_on_disk: bool


@dataclass(frozen=True)
class ConfigTomlPlan:
    """Plan describing the ``config.toml`` mutation."""

    config_toml_path: Path
    present_existing: bool
    parse_failed: bool
    will_overwrite_preview_section: bool
    will_write_new_section: bool


@dataclass(frozen=True)
class WorkflowInstallResult:
    """Aggregate result of ``install_workflow``."""

    repo_root_path: Path
    name: str
    dry_run: bool
    template_file_plans: tuple[TemplateFilePlan, ...]
    config_toml_plan: ConfigTomlPlan | None
    refused_template_paths: tuple[Path, ...] = field(default_factory=tuple)


class UnknownWorkflowError(ValueError):
    """Raised when the requested workflow name has no bundled template."""


class ExistingFileRefusedError(RuntimeError):
    """Raised when template files exist and ``--force`` was not passed."""

    def __init__(self, refused_paths: list[Path]) -> None:
        self.refused_paths = refused_paths
        joined = ", ".join(str(path) for path in refused_paths)
        super().__init__(
            f"Refusing to overwrite existing files (use --force to override): {joined}"
        )


def install_workflow(
    options: WorkflowInstallOptions,
    process_runner: SubprocessRunner | None = None,
) -> WorkflowInstallResult:
    """Install a bundled workflow template into the current Git repository.

    Args:
        options: Install options (cwd, name, force, dry_run).
        process_runner: Optional subprocess runner (mostly used for the
            ``git rev-parse --show-toplevel`` call inside the repository
            guard).

    Returns:
        ``WorkflowInstallResult`` describing every file write plan and the
        ``config.toml`` mutation plan.

    Raises:
        UnknownWorkflowError: When no bundled template matches ``options.name``.
        ExistingFileRefusedError: When existing files would be overwritten
            and ``options.force`` is ``False``.
        IARRepositoryNotInitializedError: When ``.iar.toml`` is missing.
        ValueError: When the cwd is not inside a Git repository.
    """
    repo_root_path = detect_git_repository_root(options.cwd, process_runner)
    require_iar_repository_initialized(repo_root_path, process_runner)

    template_root = _resolve_template_root(options.name)
    template_file_plans = _plan_template_writes(template_root, repo_root_path)

    refused_paths = [
        plan.target_path
        for plan in template_file_plans
        if plan.exists_on_disk and not options.force
    ]
    if refused_paths and not options.dry_run:
        raise ExistingFileRefusedError(refused_paths)

    config_toml_plan = _plan_config_toml_update(
        repo_root_path=repo_root_path,
        force=options.force,
        dry_run=options.dry_run,
    )

    if options.dry_run:
        return WorkflowInstallResult(
            repo_root_path=repo_root_path,
            name=options.name,
            dry_run=True,
            template_file_plans=template_file_plans,
            config_toml_plan=config_toml_plan,
            refused_template_paths=tuple(refused_paths),
        )

    _apply_template_writes(
        template_root=template_root,
        template_file_plans=template_file_plans,
        force=options.force,
    )
    _apply_config_toml_update(
        config_toml_plan=config_toml_plan,
        force=options.force,
    )

    return WorkflowInstallResult(
        repo_root_path=repo_root_path,
        name=options.name,
        dry_run=False,
        template_file_plans=template_file_plans,
        config_toml_plan=config_toml_plan,
    )


def render_preview_placeholder_section() -> str:
    """Render the ``[preview]`` placeholder section using the current
    ``PreviewSettings`` field set.

    Field names are pulled directly from ``PreviewSettings.model_fields`` so
    adding a new field to the settings class automatically updates the
    placeholder output without any code change here.
    """
    document = tomlkit.document()
    document.add(_tomlkit_comment(_PREVIEW_SECTION_HEADER))
    document.add(tomlkit.nl())
    preview_table = _build_preview_table()
    document.append("preview", preview_table)
    return tomlkit.dumps(document)


def _resolve_template_root(name: str):
    """Return the importlib Traversable for the requested workflow name."""
    template_root = files(TEMPLATE_PACKAGE_NAME).joinpath(name)
    if not template_root.is_dir():
        raise UnknownWorkflowError(f"Unknown workflow: {name}")
    return template_root


def _iter_template_files(template_root) -> Iterable[tuple[str, object]]:
    """Yield ``(relative_posix_path, traversable)`` for every template file."""
    for entry in sorted(template_root.rglob("*"), key=lambda p: str(p)):
        if not entry.is_file():
            continue
        relative_path = entry.relative_to(template_root).as_posix()
        yield relative_path, entry


def _plan_template_writes(template_root, repo_root_path: Path) -> tuple[TemplateFilePlan, ...]:
    """Build per-file write plans for a template root."""
    plans: list[TemplateFilePlan] = []
    for relative_posix_path, traversable in _iter_template_files(template_root):
        template_text = traversable.read_text(encoding="utf-8")
        target_path = repo_root_path / relative_posix_path
        plans.append(
            TemplateFilePlan(
                source_relative_path=relative_posix_path,
                target_path=target_path,
                bytes_to_write=len(template_text.encode("utf-8")),
                exists_on_disk=target_path.exists(),
            )
        )
    return tuple(plans)


def _plan_config_toml_update(
    *,
    repo_root_path: Path,
    force: bool,
    dry_run: bool,
) -> ConfigTomlPlan | None:
    """Describe the ``config.toml`` ``[preview]`` section mutation."""
    config_toml_path = repo_root_path / "config.toml"
    if not config_toml_path.is_file():
        return None

    raw_text = config_toml_path.read_text(encoding="utf-8")
    try:
        parsed_document: TOMLDocument = tomlkit.loads(raw_text)
    except Exception as exc:  # noqa: BLE001 - FR-11: best-effort, never fail.
        logger.warning(
            "config.toml 解析失败，跳过 [preview] 段写入: %s",
            exc,
        )
        return ConfigTomlPlan(
            config_toml_path=config_toml_path,
            present_existing=False,
            parse_failed=True,
            will_overwrite_preview_section=False,
            will_write_new_section=False,
        )

    present_existing = "preview" in parsed_document and isinstance(
        parsed_document.get("preview"), Table
    )
    will_overwrite = force and present_existing
    will_write_new = not present_existing
    return ConfigTomlPlan(
        config_toml_path=config_toml_path,
        present_existing=present_existing,
        parse_failed=False,
        will_overwrite_preview_section=will_overwrite,
        will_write_new_section=will_write_new,
    )


def _apply_template_writes(
    *,
    template_root,
    template_file_plans: tuple[TemplateFilePlan, ...],
    force: bool,
) -> None:
    """Write template files to disk and restore executable bits."""
    plans_by_path = {plan.source_relative_path: plan for plan in template_file_plans}
    for relative_posix_path, traversable in _iter_template_files(template_root):
        plan = plans_by_path[relative_posix_path]
        if plan.exists_on_disk and not force:
            continue
        template_text = traversable.read_text(encoding="utf-8")
        plan.target_path.parent.mkdir(parents=True, exist_ok=True)
        plan.target_path.write_text(template_text, encoding="utf-8")
        if relative_posix_path in _EXECUTABLE_RELATIVE_PATHS:
            _set_executable_bit(plan.target_path)


def _set_executable_bit(target_path: Path) -> None:
    """Apply ``0755`` to ``target_path`` while preserving any existing bits."""
    current_mode = target_path.stat().st_mode
    target_mode = current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    target_mode = (target_mode & ~stat.S_IRWXO) | 0o755 & stat.S_IRWXO
    os.chmod(target_path, target_mode)


def _apply_config_toml_update(
    *,
    config_toml_plan: ConfigTomlPlan | None,
    force: bool,
) -> None:
    """Apply the ``config.toml`` ``[preview]`` section mutation."""
    if config_toml_plan is None or config_toml_plan.parse_failed:
        return
    if config_toml_plan.present_existing and not force:
        return

    raw_text = config_toml_plan.config_toml_path.read_text(encoding="utf-8")
    document: TOMLDocument = tomlkit.loads(raw_text)

    preview_table = _build_preview_table()
    if "preview" in document and isinstance(document.get("preview"), Table):
        document["preview"] = preview_table  # type: ignore[index]
    else:
        if len(document) and not _ends_with_blank_line(raw_text):
            document.add(tomlkit.nl())
            document.add(tomlkit.nl())
        elif len(document):
            document.add(tomlkit.nl())
        document.add(_tomlkit_comment(_PREVIEW_SECTION_HEADER))
        document.add(tomlkit.nl())
        document.append("preview", preview_table)

    rendered_text = tomlkit.dumps(document)
    config_toml_plan.config_toml_path.write_text(rendered_text, encoding="utf-8")


def _build_preview_table() -> Table:
    """Build a ``[preview]`` table whose keys come from ``PreviewSettings``.

    String-typed fields are written with the ``<set-me>`` sentinel so the
    operator can grep for them. Non-string fields (currently just
    ``enabled: bool``) keep the schema default so ``config.toml`` stays
    parseable by ``pydantic-settings`` until the operator edits the
    placeholders.
    """
    default_settings = PreviewSettings()
    table = tomlkit.table()
    for field_name, model_field in PreviewSettings.model_fields.items():
        annotation = model_field.annotation
        if annotation is bool:
            placeholder_value = getattr(default_settings, field_name)
        else:
            placeholder_value = "<set-me>"
        table.add(field_name, placeholder_value)
    return table


def _ends_with_blank_line(raw_text: str) -> bool:
    """Return True when ``raw_text`` already ends with at least one blank line."""
    return raw_text.endswith("\n\n") or raw_text.endswith("\r\n\r\n")


def _tomlkit_comment(text: str):
    """Wrap ``text`` as a tomlkit comment item suitable for ``document.add``."""
    comment = tomlkit.comment(text)
    return comment


__all__ = [
    "ExistingFileRefusedError",
    "UnknownWorkflowError",
    "WorkflowInstallOptions",
    "WorkflowInstallResult",
    "ConfigTomlPlan",
    "TemplateFilePlan",
    "install_workflow",
    "render_preview_placeholder_section",
]
