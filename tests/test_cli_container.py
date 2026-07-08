"""Tests for ``iar container`` CLI plumbing.

覆盖：
- argparse 能解析 ``container auth import`` / ``container up`` / ``container down`` / ``container logs``
- Typer 顶层 ``iar container --help`` 展示 ``auth`` 子命令；``iar container auth --help`` 展示 ``import``
- Typer 层不直接 import ``backend.engines.*``（架构守门）

不真跑 docker / 文件复制；只验证 CLI 表面契约与依赖方向。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backend.api.cli_parser import build_parser
from backend.api.cli_typer_app import app, auth_app, container_app


def test_container_subparser_top_level_registered() -> None:
    """argparse 的 ``container`` 顶层子命令存在。"""
    parser = build_parser()
    # argparse 不直接暴露子命令列表；用 parse_args 间接验证
    args = parser.parse_args(["container", "auth", "import"])
    assert args.command == "container auth import"
    assert args.container_command == "auth"
    assert args.container_auth_command == "import"


def test_container_up_parses_arguments() -> None:
    """``container up --repo / --repo-id --gh-token --build --dry-run`` 全部解析正确。"""
    parser = build_parser()
    args = parser.parse_args(
        [
            "container",
            "up",
            "--repo",
            "/Users/me/repo",
            "--repo-id",
            "keda",
            "--gh-token",
            "ghp_xxx",
            "--build",
            "--dry-run",
        ]
    )
    assert args.command == "container up"
    assert args.repo == "/Users/me/repo"
    assert args.repo_id == "keda"
    assert args.gh_token == "ghp_xxx"
    assert args.build is True
    assert args.dry_run is True


def test_container_down_parses_dry_run() -> None:
    """``container down --dry-run`` 解析正确。"""
    parser = build_parser()
    args = parser.parse_args(["container", "down", "--dry-run"])
    assert args.command == "container down"
    assert args.dry_run is True


def test_container_logs_parses_no_follow() -> None:
    """``container logs --no-follow`` 解析正确。"""
    parser = build_parser()
    args = parser.parse_args(["container", "logs", "--no-follow"])
    assert args.command == "container logs"
    assert args.no_follow is True


def test_typer_app_registers_container_subapp() -> None:
    """Typer 主 app 上注册了 ``container`` 与 ``auth`` 子 app。"""
    registered_subapps = {info.typer_instance for info in app.registered_groups}
    assert container_app in registered_subapps

    container_subapps = {info.typer_instance for info in container_app.registered_groups}
    assert auth_app in container_subapps


def test_typer_container_help_lists_auth() -> None:
    """``iar container --help`` 列出 ``auth`` 子命令组。"""
    runner = CliRunner()
    result = runner.invoke(app, ["container", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "auth" in result.stdout


def test_typer_container_auth_help_lists_import() -> None:
    """``iar container auth --help`` 列出 ``import`` 子命令。"""
    runner = CliRunner()
    result = runner.invoke(app, ["container", "auth", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "import" in result.stdout


def test_cli_typer_container_has_no_engines_import() -> None:
    """``cli_typer_container.py`` 不直接 import ``backend.engines.*``，保持 ``api → core`` 方向。"""
    cli_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "backend"
        / "api"
        / "cli_typer_container.py"
    )
    source = cli_path.read_text(encoding="utf-8")
    # 允许 import engines 的注释；匹配实际 import 行
    forbidden = re.compile(r"^\s*from\s+backend\.engines", flags=re.MULTILINE)
    assert forbidden.search(source) is None, (
        "cli_typer_container.py must not directly import backend.engines.* — "
        "route through core facade."
    )


def test_container_up_accepts_repo_path_and_repo_id_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """container up 同时传 --repo (mount path) 与 --repo-id (registry id) 不被互斥校验拦。

    回归: _run_parsed_command 的互斥校验曾把文档主路径
    `iar container up --repo <path> --repo-id <id>` 误判为 exit 1。
    container up 的 --repo 是容器挂载路径、--repo-id 是仓库 registry id，
    二者语义互补，与其它命令里二者互为仓库选择器不同。
    """
    import argparse

    from backend.api import cli as cli_module

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    # 屏蔽真实 dispatch，只验证互斥校验不拦 container up。
    monkeypatch.setattr(cli_module, "dispatch_parsed_command", lambda _ctx: 0)

    namespace = argparse.Namespace(
        command="container up",
        repo=str(repo_path),
        repo_id="keda",
        config=None,
        gh_token=None,
        build=False,
        dry_run=True,
    )
    assert cli_module._run_parsed_command(namespace) == 0


def test_non_container_command_still_enforces_repo_selector_mutex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 container up 命令仍受 --repo/--repo-id 互斥校验约束。

    确保 container up 的豁免是外科手术式的，不放松其它命令的选择器互斥。
    """
    import argparse

    from backend.api import cli as cli_module

    monkeypatch.setattr(cli_module, "dispatch_parsed_command", lambda _ctx: 0)

    namespace = argparse.Namespace(
        command="run",
        repo=str(tmp_path),
        repo_id="keda",
        config=None,
    )
    # 互斥校验在 dispatch 之前，即便 dispatch 被模拟为 0 也应先返回 1。
    assert cli_module._run_parsed_command(namespace) == 1
