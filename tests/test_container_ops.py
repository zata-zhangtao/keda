"""Tests for ``backend.engines.agent_runner.container_ops``.

覆盖：
- 包内资产定位：从 importlib.resources 解析到 docker-compose.runner.yml 绝对路径
- ``build_container_up_argv`` / ``build_container_down_argv`` / ``build_container_logs_argv`` 构造正确的 argv
- ``build_container_up_env`` 注入 ``REPO_PATH`` / ``RUNNER_UID`` / ``RUNNER_GID`` / ``GH_TOKEN``
- ``run_container_up`` 用 dry-run 模式不调 docker；非 dry-run 模式调用注入的 runner
- docker CLI 不存在时抛 ``FileNotFoundError``
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.engines.agent_runner.container_ops import (
    COMPOSE_FILE_NAME,
    ContainerUpRequest,
    build_container_down_argv,
    build_container_logs_argv,
    build_container_up_argv,
    build_container_up_env,
    plan_container_up,
    resolve_packaged_runner_assets,
    run_container_down,
    run_container_logs,
    run_container_up,
)


def _fake_docker(which_returns: str | None = "/usr/bin/docker") -> None:
    """Patch ``shutil.which`` to pretend docker is on PATH (or not)."""
    return patch("shutil.which", lambda _: which_returns)


def test_resolve_packaged_runner_assets_finds_compose() -> None:
    """包内 ``runner_container/docker-compose.runner.yml`` 必须能被定位。"""
    assets = resolve_packaged_runner_assets()
    assert assets.compose_file.is_absolute()
    assert assets.compose_file.name == COMPOSE_FILE_NAME
    assert assets.compose_file.is_file()
    # .env.example 不强制存在（生产可省略），但模板目录必须存在
    assert assets.template_dir.is_dir()


def test_resolve_packaged_runner_assets_missing_raises(tmp_path: Path) -> None:
    """包内资源不存在时抛 ``FileNotFoundError``。

    通过把 ``container_ops.files`` 替换为返回单例 MagicMock 的 stub，
    让 ``compose_resource.is_file()`` 返回 False，从而验证 ``raise FileNotFoundError`` 路径。
    """
    from unittest.mock import MagicMock

    fake_compose = MagicMock()
    # 关键：把 fake_compose.joinpath 也设成返回自身；否则 ``template_root = files(...).joinpath(...)``
    # 返回 fake_compose 后，后续 ``fake_compose.joinpath(...)`` 走 auto-mock，返回
    # 一个新的子 mock，其 ``is_file()`` 是 truthy —— 不会触发 raise。
    fake_compose.joinpath = MagicMock(return_value=fake_compose)  # type: ignore[method-assign]
    fake_compose.is_file = MagicMock(return_value=False)  # type: ignore[method-assign]
    fake_template_root = MagicMock()
    fake_template_root.joinpath = MagicMock(return_value=fake_compose)  # type: ignore[method-assign]

    import backend.engines.agent_runner.container_ops as container_ops_module

    original_files = container_ops_module.files
    try:
        container_ops_module.files = lambda _pkg: fake_template_root
        with pytest.raises(FileNotFoundError):
            resolve_packaged_runner_assets()
    finally:
        container_ops_module.files = original_files


def test_build_container_up_argv_includes_compose_and_up() -> None:
    """``build_container_up_argv`` 含 docker / compose / -f / up / -d。"""
    compose_file = Path("/tmp/whatever/docker-compose.runner.yml")
    options = ContainerUpRequest(repo_path=Path("/tmp/repo"), build=False, dry_run=False)
    argv = build_container_up_argv(compose_file, options)
    assert argv[0:2] == ["docker", "compose"]
    assert argv[2:4] == ["-f", str(compose_file)]
    assert "up" in argv
    assert "-d" in argv
    assert "--build" not in argv


def test_build_container_up_argv_with_build_flag() -> None:
    """``build=True`` 时附加 ``--build``。"""
    compose_file = Path("/tmp/x.yml")
    options = ContainerUpRequest(repo_path=Path("/tmp/r"), build=True, dry_run=False)
    argv = build_container_up_argv(compose_file, options)
    assert "--build" in argv


def test_build_container_down_argv_uses_down() -> None:
    """``build_container_down_argv`` 含 ``down`` 子命令。"""
    argv = build_container_down_argv(Path("/tmp/x.yml"))
    assert argv[0:2] == ["docker", "compose"]
    assert "down" in argv


def test_build_container_logs_argv_default_follow() -> None:
    """``build_container_logs_argv`` 默认 ``--follow``。"""
    argv = build_container_logs_argv(Path("/tmp/x.yml"))
    assert "logs" in argv
    assert "--follow" in argv
    assert argv[-1] == "--follow"


def test_build_container_logs_argv_no_follow() -> None:
    """``follow=False`` 时不含 ``--follow`` 跟随标志。"""
    argv = build_container_logs_argv(Path("/tmp/x.yml"), follow=False)
    assert "logs" in argv
    # -f 是 docker compose 的 compose-file 短选项，永远存在；真正判断
    # follow 的标志是 --follow，应只在 follow=True 时出现
    assert "--follow" not in argv


def test_build_container_up_env_required_keys() -> None:
    """env 必须含 ``REPO_PATH`` / ``RUNNER_UID`` / ``RUNNER_GID``。"""
    options = ContainerUpRequest(repo_path=Path("/Users/me/repo"))
    env = build_container_up_env(options)
    assert env["REPO_PATH"] == "/Users/me/repo"
    assert env["RUNNER_UID"].isdigit()
    assert env["RUNNER_GID"].isdigit()
    # 未传入时不假造 token；键可能不存在（如果没传入 gh_token）
    assert env.get("GH_TOKEN", "") == ""


def test_build_container_up_env_includes_gh_token_and_repo_id() -> None:
    """``GH_TOKEN`` / ``IAR_REPO_ID`` 在传入时出现，未传入时不出现。"""
    options = ContainerUpRequest(
        repo_path=Path("/Users/me/repo"),
        gh_token="ghp_xxx",
        repo_id="keda",
    )
    env = build_container_up_env(options)
    assert env["GH_TOKEN"] == "ghp_xxx"
    assert env["IAR_REPO_ID"] == "keda"


def test_build_container_up_env_explicit_uid_gid() -> None:
    """显式传入 ``uid``/``gid`` 时覆盖默认 ``os.getuid()``/``os.getgid()``。"""
    options = ContainerUpRequest(repo_path=Path("/tmp"), uid=1234, gid=5678)
    env = build_container_up_env(options)
    assert env["RUNNER_UID"] == "1234"
    assert env["RUNNER_GID"] == "5678"


def test_build_container_up_env_extra_env_overrides() -> None:
    """``extra_env`` 覆盖默认项，便于扩展字段。"""
    options = ContainerUpRequest(
        repo_path=Path("/tmp"),
        extra_env={"REPO_PATH": "/override", "CUSTOM": "value"},
    )
    env = build_container_up_env(options)
    assert env["REPO_PATH"] == "/override"
    assert env["CUSTOM"] == "value"


def test_plan_container_up_returns_plan_without_invoking_docker() -> None:
    """``plan_container_up`` 不调 docker，只返回计划。"""
    options = ContainerUpRequest(repo_path=Path("/tmp/r"))
    plan = plan_container_up(Path("/tmp/compose.yml"), options)
    assert plan.compose_file == Path("/tmp/compose.yml")
    # plan.argv 是 tuple（immutable）；前 4 个元素应为 docker compose -f <file>
    assert tuple(plan.argv[0:4]) == ("docker", "compose", "-f", "/tmp/compose.yml")
    assert "up" in plan.argv
    assert plan.env["REPO_PATH"] == "/tmp/r"


def test_run_container_up_dry_run_skips_runner() -> None:
    """``dry_run=True`` 时不调注入的 runner。"""
    captured: list[tuple] = []

    def fake_runner(argv, *, env, cwd, check):
        captured.append((argv, env, cwd, check))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    options = ContainerUpRequest(repo_path=Path("/tmp/r"), dry_run=True, build=True)
    run_container_up(Path("/tmp/compose.yml"), options, runner=fake_runner)
    assert captured == []  # 没调 docker


def test_run_container_up_invokes_runner_with_correct_env() -> None:
    """非 dry-run 模式调用注入的 runner 并传正确 env。"""
    captured: list[tuple] = []

    def fake_runner(argv, *, env, cwd, check):
        captured.append((argv, dict(env), cwd, check))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    options = ContainerUpRequest(
        repo_path=Path("/Users/me/repo"),
        gh_token="ghp_xxx",
        repo_id="keda",
        uid=501,
        gid=20,
    )
    run_container_up(Path("/tmp/compose.yml"), options, runner=fake_runner)

    assert len(captured) == 1
    argv, env, cwd, check = captured[0]
    assert argv[0] == "docker"
    assert "up" in argv
    assert env["REPO_PATH"] == "/Users/me/repo"
    assert env["RUNNER_UID"] == "501"
    assert env["RUNNER_GID"] == "20"
    assert env["GH_TOKEN"] == "ghp_xxx"
    assert env["IAR_REPO_ID"] == "keda"
    # 系统 PATH 等被合并到 env（不被替换）
    assert "PATH" in env
    assert check is True


def test_run_container_up_docker_missing_raises(tmp_path: Path) -> None:
    """PATH 上没有 docker 时抛 ``FileNotFoundError``。"""
    options = ContainerUpRequest(repo_path=tmp_path / "repo")
    compose = tmp_path / "compose.yml"
    compose.write_text("", encoding="utf-8")

    with _fake_docker(which_returns=None):
        with pytest.raises(FileNotFoundError, match="docker CLI not found"):
            run_container_up(compose, options)


def test_run_container_down_invokes_runner() -> None:
    """``run_container_down`` 调注入的 runner，argv 含 ``down``。"""
    captured: list[tuple] = []

    def fake_runner(argv, *, env, cwd, check):
        captured.append((argv, env, cwd, check))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    argv = run_container_down(Path("/tmp/compose.yml"), runner=fake_runner)
    assert "down" in argv
    assert len(captured) == 1
    assert captured[0][0] == argv


def test_run_container_logs_invokes_runner() -> None:
    """``run_container_logs`` 调注入的 runner，argv 含 ``logs --follow``。"""
    captured: list[tuple] = []

    def fake_runner(argv, *, env, cwd, check):
        captured.append((argv, env, cwd, check))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    argv = run_container_logs(Path("/tmp/compose.yml"), runner=fake_runner)
    assert "logs" in argv
    assert "--follow" in argv
    assert captured[0][0] == argv


def test_run_container_logs_no_follow() -> None:
    """``follow=False`` 时 argv 不含 ``--follow`` 跟随标志。"""
    captured: list[tuple] = []

    def fake_runner(argv, *, env, cwd, check):
        captured.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    argv = run_container_logs(Path("/tmp/compose.yml"), follow=False, runner=fake_runner)
    assert "--follow" not in argv
