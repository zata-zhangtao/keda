"""``iar container ...`` 命令的 core 命令入口（dispatch handler）。

每个 handler 接收 :class:`ParsedCommandContext`，构造 engines 实现并注入到
:mod:`backend.core.use_cases.agent_runner_container` 中的 facade；本模块自身
不写任何 docker / 文件复制实现，仅做参数校验、日志格式化、退出码返回。

依赖方向：api → core（facade）；api → engines（构造实现并注入）。
本模块同时 import engines 实现类与 core facade，是合法的 ``api`` 层职责。
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from backend.api.cli_console import console
from backend.api.cli_parsed_context import ParsedCommandContext
from backend.core.use_cases.agent_runner_container import (
    StartRunnerContainerOptions,
    import_container_auth,
    start_runner_container,
    stop_runner_container,
    stream_runner_container_logs,
)
from backend.core.use_cases.daemon_single_instance import DaemonAlreadyRunningError
from backend.engines.agent_runner.container_auth import ContainerAuthController
from backend.engines.agent_runner.container_ops import ContainerOpsController


def _resolve_repo_path(parsed) -> Path | None:
    """从 ``--repo`` / ``REPO_PATH`` 解析目标仓库绝对路径；优先用 CLI 显式值。"""
    raw = getattr(parsed, "repo", None)
    if raw:
        return Path(raw).expanduser().resolve()
    env_value = os.environ.get("REPO_PATH")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return None


def _resolve_repo_id(parsed) -> str:
    """从 ``--repo-id`` / ``IAR_REPO_ID`` 解析仓库 id。"""
    raw = getattr(parsed, "repo_id", None)
    if raw:
        return raw
    env_value = os.environ.get("IAR_REPO_ID")
    return env_value or ""


def _resolve_gh_token(parsed) -> str:
    """从 ``--gh-token`` / ``GH_TOKEN`` 解析 GitHub Token。"""
    raw = getattr(parsed, "gh_token", None)
    if raw:
        return raw
    return os.environ.get("GH_TOKEN", "")


def _format_env_preview(env: dict[str, str]) -> str:
    """序列化 env 给 ``--dry-run`` 输出，密钥用 ``<set>`` 标识避免泄露。"""
    secret_keys = {"GH_TOKEN"}
    parts: list[str] = []
    for key in sorted(env):
        value = env[key]
        rendered = "<set>" if key in secret_keys and value else value
        parts.append(f"{key}={rendered}")
    return " ".join(parts)


def run_container_auth_import_command(ctx: ParsedCommandContext) -> int:
    """``iar container auth import``：把本机认证快照到容器专用目录。"""
    if ctx.repo_id is not None or ctx.repo_override is not None or ctx.parsed.config is not None:
        console.print(
            "[red]iar container auth import uses the host's current profile; "
            "omit --repo/--repo-id/--config.[/]"
        )
        return 1

    importer = ContainerAuthController()
    result = import_container_auth(importer)
    console.print(f"[green]Container auth root:[/] {result.container_auth_dir}")
    for agent_result in result.agent_results:
        if agent_result.skipped:
            console.print(
                f"  [yellow]{agent_result.agent_name}: skipped[/] ({agent_result.skip_reason})"
            )
            continue
        entries = ", ".join(agent_result.copied_entries)
        console.print(
            f"  [green]{agent_result.agent_name}: copied[/] {entries} -> {agent_result.target_dir}"
        )
    if not result.gitignore_protected:
        console.print(
            "[yellow]Note: ~/.iar is not a git repo; container-auth is still protected "
            "by filesystem permissions (0700) but cannot be marked via gitignore.[/]"
        )
    return 0


def run_container_up_command(ctx: ParsedCommandContext) -> int:
    """``iar container up``：启动 runner 容器。"""
    parsed = ctx.parsed
    repo_path = _resolve_repo_path(parsed)
    if repo_path is None:
        console.print(
            "[red]Missing --repo (or REPO_PATH env). "
            "Pass the absolute path of the Git repository to mount.[/]"
        )
        return 1

    repo_id = _resolve_repo_id(parsed)
    gh_token = _resolve_gh_token(parsed)
    dry_run = bool(getattr(parsed, "dry_run", False))
    build = bool(getattr(parsed, "build", False))

    process_registry_path = ctx.runner_settings.console.process_registry_path

    options = StartRunnerContainerOptions(
        repo_path=repo_path,
        repo_id=repo_id,
        gh_token=gh_token,
        process_registry_path=process_registry_path,
        dry_run=dry_run,
        build=build,
    )

    controller = ContainerOpsController()
    try:
        result = start_runner_container(controller, options, runner=_dry_run_runner(dry_run))
    except DaemonAlreadyRunningError as exc:
        console.print(
            f"[red]{exc}[/] Stop the host daemon first "
            "(`iar daemon stop --repo-id <id>`) before starting a container."
        )
        return 2
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    plan = result.plan
    argv_text = " ".join(shlex.quote(part) for part in plan.argv)
    console.print(f"[green]Container compose file:[/] {plan.compose_file}")
    console.print(f"[green]Planned argv:[/] {argv_text}")
    console.print(f"[green]Env overrides:[/] {_format_env_preview(plan.env)}")
    if result.daemon_lock_check_skipped:
        console.print(
            "[yellow]Note: no --repo-id given, skipped host daemon lock check. "
            "Pass --repo-id to enable mutual exclusion.[/]"
        )
    if dry_run:
        console.print("[cyan]Dry-run: docker compose not invoked.[/]")
        return 0
    console.print("[green]Container started.[/]")
    return 0


def run_container_down_command(ctx: ParsedCommandContext) -> int:
    """``iar container down``：停止容器。"""
    parsed = ctx.parsed
    dry_run = bool(getattr(parsed, "dry_run", False))
    controller = ContainerOpsController()
    assets = controller.resolve_packaged_runner_assets()
    argv = stop_runner_container(controller, assets.compose_file, runner=_dry_run_runner(dry_run))
    argv_text = " ".join(shlex.quote(part) for part in argv)
    if dry_run:
        console.print(f"[cyan]Dry-run:[/] {argv_text}")
    else:
        console.print(f"[green]Container stopped:[/] {argv_text}")
    return 0


def run_container_logs_command(ctx: ParsedCommandContext) -> int:
    """``iar container logs``：streaming 容器日志。"""
    parsed = ctx.parsed
    follow = bool(getattr(parsed, "follow", True))
    controller = ContainerOpsController()
    assets = controller.resolve_packaged_runner_assets()
    argv = stream_runner_container_logs(controller, assets.compose_file, follow=follow)
    console.print(f"[green]Tailing logs:[/] {' '.join(shlex.quote(p) for p in argv)}")
    return 0


def _dry_run_runner(dry_run: bool):
    """``--dry-run`` 时屏蔽实际 docker 调用，仅打印 argv。"""
    if not dry_run:
        return None

    def fake_runner(argv, *, env, cwd, check):  # noqa: ARG001 - dry-run noop.
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout="dry-run",
            stderr="",
        )

    return fake_runner


__all__ = [
    "run_container_auth_import_command",
    "run_container_down_command",
    "run_container_logs_command",
    "run_container_up_command",
]
