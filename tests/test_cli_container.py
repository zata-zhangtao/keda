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
