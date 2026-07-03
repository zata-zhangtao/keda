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
    GITIGNORE_BLOCK_FOOTER,
    GITIGNORE_BLOCK_HEADER,
    IAR_GITIGNORE_SECTIONS,
    GitignoreSyncOptions,
    GitignoreSyncResult,
    RepositoryInitOptions,
    RepositoryInitResult,
    ensure_gitignore_entries,
    initialize_repository_local_config,
)
from backend.engines.agent_runner.takeover import upsert_repository

if TYPE_CHECKING:
    from backend.core.shared.interfaces.agent_runner import IProcessRunner


def _run_init_command(parsed: argparse.Namespace, process_runner: IProcessRunner) -> int:
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
    update_gitignore = not getattr(parsed, "no_update_gitignore", False)
    gitignore_result = ensure_gitignore_entries(
        GitignoreSyncOptions(
            repo_root_path=init_result.repo_root_path,
            dry_run=parsed.dry_run,
            skip=not update_gitignore,
        )
    )
    if parsed.dry_run:
        print(init_result.config_text, end="")
        console.print(
            f"[cyan]{format_skill_copy_plan(init_result.repo_root_path)}[/]",
            markup=False,
        )
        _print_gitignore_plan(gitignore_result)
        return 0
    if init_result.wrote_file:
        console.print(f"[green]Wrote IAR local config:[/] {init_result.config_path}")
    else:
        console.print(f"[dim]IAR local config already up to date:[/] {init_result.config_path}")
    _print_gitignore_summary(gitignore_result)
    if init_result.repo_id:
        try:
            registry_result = upsert_repository(
                repo_id=init_result.repo_id,
                repo_path=init_result.repo_root_path,
                display_name=init_result.display_name or init_result.repo_id,
                editor=create_registry_editor(),
            )
            if registry_result.action == "added":
                console.print(f"[green]Registered repository:[/] {registry_result.repo_id}")
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
            github_client=create_github_client(init_result.repo_root_path, process_runner),
        )
        console.print(f"[green]Labels synced for:[/] {init_result.repo_root_path}")
    except Exception as exc:  # noqa: BLE001
        error_console.print(f"[yellow]Label sync failed:[/] {exc}")
    return 0


def _print_gitignore_plan(result: GitignoreSyncResult) -> None:
    """Print the ``.gitignore`` plan in dry-run mode.

    Args:
        result: ``ensure_gitignore_entries`` 的 dry-run 结果。
    """
    if result.skipped:
        console.print(
            f"[cyan]Would skip .gitignore update (--no-update-gitignore):[/] "
            f"{result.gitignore_path}",
            markup=False,
        )
        _print_info_exclude_hint_if_needed(result)
        return
    if result.block_inserted:
        joined = ", ".join(result.entries_added)
        console.print(
            f"[cyan]Would add IAR patterns to .gitignore:[/] {joined}",
            markup=False,
        )
        for line in _format_block_preview():
            console.print(f"  [cyan]+ {line}[/]", markup=False)
    elif result.block_updated:
        joined_added = ", ".join(result.entries_added) or "(none)"
        joined_skipped = ", ".join(result.entries_skipped_external) or "(none)"
        console.print(
            f"[cyan]Would update IAR .gitignore block:[/] "
            f"added={joined_added}; skipped_external={joined_skipped}",
            markup=False,
        )
    elif not result.entries_added and not result.entries_skipped_external:
        console.print(
            f"[cyan]Would leave .gitignore unchanged (no IAR patterns needed):[/] "
            f"{result.gitignore_path}",
            markup=False,
        )
    else:
        joined_skipped = ", ".join(result.entries_skipped_external) or "(none)"
        console.print(
            f"[cyan]Would leave .gitignore block unchanged:[/] "
            f"skipped_external={joined_skipped}",
            markup=False,
        )
    _print_info_exclude_hint_if_needed(result)


def _print_gitignore_summary(result: GitignoreSyncResult) -> None:
    """Print the ``.gitignore`` sync summary in non-dry-run mode.

    Args:
        result: ``ensure_gitignore_entries`` 的执行结果。
    """
    if result.skipped:
        console.print(
            f"[dim]Skipped .gitignore update (--no-update-gitignore):[/] "
            f"{result.gitignore_path}"
        )
        _print_info_exclude_hint_if_needed(result)
        return
    if result.block_inserted:
        joined = ", ".join(result.entries_added)
        console.print(
            f"[green]Updated .gitignore with IAR patterns:[/] {joined} "
            f"-> {result.gitignore_path}"
        )
    elif result.block_updated:
        joined_added = ", ".join(result.entries_added) or "(none)"
        joined_skipped = ", ".join(result.entries_skipped_external) or "(none)"
        console.print(
            f"[yellow]Updated IAR .gitignore block:[/] "
            f"added={joined_added}; skipped_external={joined_skipped} "
            f"-> {result.gitignore_path}"
        )
    elif result.entries_added or result.entries_skipped_external:
        joined_skipped = ", ".join(result.entries_skipped_external) or "(none)"
        console.print(
            f"[dim]IAR .gitignore block already up to date:[/] "
            f"skipped_external={joined_skipped} -> {result.gitignore_path}"
        )
    else:
        console.print(f"[dim]IAR .gitignore block already up to date:[/] {result.gitignore_path}")
    _print_info_exclude_hint_if_needed(result)


def _print_info_exclude_hint_if_needed(result: GitignoreSyncResult) -> None:
    """当 ``.git/info/exclude`` 含历史 iar 条目时,提示用户清理。"""
    if not result.info_exclude_hint:
        return
    error_console.print(
        "[yellow]Hint:[/] .git/info/exclude contains legacy iar entries "
        "(e.g. /.iar/evidence/, /.iar-worktrees/) that are now covered by "
        "the managed .gitignore block. You may delete them manually.",
        markup=False,
    )


def _format_block_preview() -> list[str]:
    """Return the would-be-written iar block as a list of display lines.

    Used by dry-run output to show the user exactly what will be appended.
    """
    lines = [GITIGNORE_BLOCK_HEADER]
    first = True
    for comment, patterns in IAR_GITIGNORE_SECTIONS:
        if not first:
            lines.append("")
        first = False
        lines.append(comment)
        lines.extend(patterns)
    lines.append(GITIGNORE_BLOCK_FOOTER)
    return lines


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
