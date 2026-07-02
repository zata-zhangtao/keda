"""Implementation of the ``iar init`` CLI command."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from backend.api.cli_console import console, error_console
from backend.core.shared.models.agent_runner import LabelConfig
from backend.core.use_cases.sync_labels import sync_labels
from backend.engines.agent_runner.factory import (
    create_github_client,
    create_registry_editor,
    logger,
)
from backend.engines.agent_runner.init_flow import (
    BundledSkillCopyOptions,
    DEFAULT_BUNDLED_SKILL_NAMES,
    copy_bundled_skills,
    format_skill_copy_plan,
    format_skill_copy_summary,
)
from backend.engines.agent_runner.repository_local import (
    RepositoryInitOptions,
    RepositoryInitResult,
    initialize_repository_local_config,
)
from backend.engines.agent_runner.takeover import upsert_repository

if TYPE_CHECKING:
    from backend.core.shared.interfaces.agent_runner import IProcessRunner


def _run_init_command(
    parsed: argparse.Namespace, process_runner: IProcessRunner
) -> int:
    """Render / write the local config, copy bundled skills, sync labels."""
    try:
        init_result = initialize_repository_local_config(
            RepositoryInitOptions(
                cwd=Path.cwd(),
                repo_id_override=parsed.repository_id,
                display_name_override=parsed.display_name,
                remote_override=parsed.remote,
                base_branch_override=parsed.base_branch,
                dry_run=parsed.dry_run,
                force=parsed.force,
            ),
            process_runner,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
        logger.error("iar init failed: %s", exc)
        return 1
    _print_verification_commands_review_hint(init_result)
    if parsed.dry_run:
        print(init_result.config_text, end="")
        console.print(
            f"[cyan]{format_skill_copy_plan(init_result.repo_root_path)}[/]",
            markup=False,
        )
        return 0
    if init_result.wrote_file:
        console.print(f"[green]Wrote IAR local config:[/] {init_result.config_path}")
    else:
        console.print(
            f"[dim]IAR local config already up to date:[/] {init_result.config_path}"
        )
    if init_result.repo_id:
        try:
            registry_result = upsert_repository(
                repo_id=init_result.repo_id,
                repo_path=init_result.repo_root_path,
                display_name=init_result.display_name or init_result.repo_id,
                editor=create_registry_editor(),
            )
            if registry_result.action == "added":
                console.print(
                    f"[green]Registered repository:[/] {registry_result.repo_id}"
                )
            elif registry_result.action == "updated":
                console.print(
                    f"[yellow]Updated registry path for {registry_result.repo_id}:[/] "
                    f"{registry_result.previous_path} -> {registry_result.path}"
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to register repository in global registry: %s", exc)
            return 1
    copy_skills_flag = (
        False
        if getattr(parsed, "skip_skills", False)
        else str(getattr(parsed, "copy_skills", "true")).lower() != "false"
    )
    try:
        skill_result = copy_bundled_skills(
            BundledSkillCopyOptions(
                repo_root_path=init_result.repo_root_path,
                force=parsed.force,
                dry_run=False,
                skip=not copy_skills_flag,
                skill_names=DEFAULT_BUNDLED_SKILL_NAMES,
            )
        )
    except Exception as exc:  # noqa: BLE001
        error_console.print(f"[yellow]Bundled skill copy failed:[/] {exc}")
    else:
        for line in format_skill_copy_summary(skill_result):
            if line.startswith("Copied skill"):
                console.print(f"[green]{line}[/]")
            elif line.startswith(("Overwrote skill", "Skill diverged")):
                console.print(f"[yellow]{line}[/]")
            elif line.startswith("Skill already up to date"):
                console.print(f"[dim]{line}[/]")
            else:
                console.print(line)
    try:
        sync_labels(
            labels_config=LabelConfig(),
            github_client=create_github_client(
                init_result.repo_root_path, process_runner
            ),
        )
        console.print(f"[green]Labels synced for:[/] {init_result.repo_root_path}")
    except Exception as exc:  # noqa: BLE001
        error_console.print(f"[yellow]Label sync failed:[/] {exc}")
    return 0


def _print_verification_commands_review_hint(
    init_result: RepositoryInitResult,
) -> None:
    """Print a fixed bilingual reminder to review verification_commands."""
    commands_repr = repr(init_result.verification_commands)
    message = (
        f"⚠️  请检查 {init_result.config_path} 中的 verification_commands / "
        f"Please review verification_commands in {init_result.config_path}:\n"
        f"    {commands_repr}\n"
        "    这些命令由 iar init 自动探测生成，建议根据项目实际 test/lint/build 命令复核并调整。\n"
        "    These commands are auto-detected by iar init; please review against your actual test/lint/build setup."
    )
    error_console.print(message, style="yellow", markup=False)


def _print_workflow_config_plan(config_plan, *, dry_run: bool) -> None:
    """Print the [preview] section status after workflow install."""
    if config_plan is None:
        console.print(
            "config.toml not found at repo root; skipping [preview] section write",
            markup=False,
            style="dim",
        )
        return
    if config_plan.parse_failed:
        console.print(
            "config.toml 解析失败，跳过 [preview] 段写入",
            markup=False,
            style="yellow",
        )
        return
    if config_plan.will_overwrite_preview_section:
        label = "Would overwrite" if dry_run else "Overwrote"
        console.print(
            f"{label} existing [preview] section",
            markup=False,
            style="yellow",
        )
        return
    if config_plan.will_write_new_section:
        label = "Would append" if dry_run else "Appended"
        console.print(
            f"{label} [preview] section",
            markup=False,
            style="green",
        )
        return
    suffix = "use --force to overwrite" if dry_run else "pass --force to overwrite"
    console.print(
        f"[preview] section already exists; {suffix}",
        markup=False,
        style="dim",
    )
