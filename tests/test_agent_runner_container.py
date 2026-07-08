"""Tests for ``backend.core.use_cases.agent_runner_container``.

覆盖：
- ``start_runner_container``：daemon lock 互斥、env 注入、目标仓库缺失报错
- ``start_runner_container --dry-run`` 不调 docker runner
- ``stop_runner_container`` / ``stream_runner_container_logs`` 透传到 engines
- core facade 不直接 import engines（架构守门）
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from backend.core.use_cases.agent_runner_container import (
    StartRunnerContainerOptions,
    import_container_auth,
    start_runner_container,
    stop_runner_container,
    stream_runner_container_logs,
)
from backend.core.use_cases.daemon_single_instance import DaemonAlreadyRunningError
from backend.engines.agent_runner.container_auth import (
    AgentImportSpec,
    ContainerAuthController,
)
from backend.engines.agent_runner.container_ops import ContainerOpsController


@pytest.fixture
def fake_global_iar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 ``Path.home()`` 指向临时目录，避免污染本机 ``~/.iar``。"""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home


def test_container_ops_controller_resolves_assets() -> None:
    """``ContainerOpsController`` 能定位包内 compose 文件。"""
    controller = ContainerOpsController()
    assets = controller.resolve_packaged_runner_assets()
    assert assets.compose_file.is_absolute()
    assert assets.compose_file.name == "docker-compose.runner.yml"


def test_container_auth_controller_imports_through_facade(
    fake_global_iar: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``import_container_auth`` 经 facade 调用 engines 实现并返回结果。"""
    # 用临时 missing specs 让所有 agent 都 skipped
    from backend.engines.agent_runner import container_auth as ca_module

    missing_specs = tuple(
        AgentImportSpec(
            agent_name=spec.agent_name,
            source_dir=fake_global_iar / "missing",
            target_subdir=spec.target_subdir,
            include_top_level=spec.include_top_level,
            exclude_subpaths=spec.exclude_subpaths,
        )
        for spec in ca_module.SUPPORTED_AGENT_SPECS
    )
    monkeypatch.setattr(ca_module, "SUPPORTED_AGENT_SPECS", missing_specs)

    importer = ContainerAuthController()
    result = import_container_auth(importer)
    assert result.container_auth_dir == fake_global_iar / ".iar" / "container-auth"
    # 由于 SUPPORTED_AGENT_SPECS 被 monkeypatched 但 default 参数在函数定义时已绑定，
    # 这里直接用底层函数验证：
    direct_result = ca_module.import_container_auth(specs=missing_specs)
    assert all(r.skipped for r in direct_result.agent_results)


def test_start_runner_container_missing_repo_path(tmp_path: Path, fake_global_iar: Path) -> None:
    """``start_runner_container`` 在目标仓库路径不存在时抛 ``FileNotFoundError``。"""
    options = StartRunnerContainerOptions(
        repo_path=tmp_path / "does-not-exist",
        repo_id="keda",
        process_registry_path=str(fake_global_iar / ".iar" / "processes.json"),
    )
    controller = ContainerOpsController()
    with pytest.raises(FileNotFoundError):
        start_runner_container(controller, options)


def test_start_runner_container_refuses_when_host_daemon_lock_live(
    tmp_path: Path, fake_global_iar: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """当目标 repo_id 已有活 PID daemon lock 时拒绝启动，且不调 docker runner。"""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    lock_dir = fake_global_iar / ".iar" / "daemon-locks"
    lock_dir.mkdir(parents=True)
    (lock_dir / "keda.lock").write_text("99999\n", encoding="utf-8")

    from backend.core.use_cases import agent_runner_container as facade_module

    monkeypatch.setattr(facade_module, "_is_pid_alive", lambda _pid: True)

    options = StartRunnerContainerOptions(
        repo_path=repo_path,
        repo_id="keda",
        process_registry_path=str(fake_global_iar / ".iar" / "processes.json"),
        dry_run=True,
    )

    called: list[tuple] = []

    def fake_runner(argv, *, env, cwd, check):
        called.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    controller = ContainerOpsController()
    with pytest.raises(DaemonAlreadyRunningError):
        start_runner_container(controller, options, runner=fake_runner)
    assert called == []


def test_start_runner_container_passes_env_to_runner(tmp_path: Path, fake_global_iar: Path) -> None:
    """``start_runner_container`` 把 env 传给注入的 docker runner。"""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    captured: list[dict] = []

    def fake_runner(argv, *, env, cwd, check):
        captured.append(dict(env))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    options = StartRunnerContainerOptions(
        repo_path=repo_path,
        repo_id="keda",
        gh_token="ghp_test",
        uid=501,
        gid=20,
        process_registry_path=str(fake_global_iar / ".iar" / "processes.json"),
    )
    controller = ContainerOpsController()
    result = start_runner_container(controller, options, runner=fake_runner)

    assert len(captured) == 1
    env = captured[0]
    assert env["REPO_PATH"] == str(repo_path)
    assert env["RUNNER_UID"] == "501"
    assert env["RUNNER_GID"] == "20"
    assert env["GH_TOKEN"] == "ghp_test"
    assert env["IAR_REPO_ID"] == "keda"

    assert result.repo_id == "keda"
    assert result.daemon_lock_check_skipped is False
    assert "up" in result.plan.argv


def test_start_runner_container_dry_run_skips_runner(tmp_path: Path, fake_global_iar: Path) -> None:
    """``dry_run=True`` 时不调 docker runner。"""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    called: list[tuple] = []

    def fake_runner(argv, *, env, cwd, check):
        called.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    options = StartRunnerContainerOptions(
        repo_path=repo_path,
        repo_id="",
        process_registry_path=str(fake_global_iar / ".iar" / "processes.json"),
        dry_run=True,
    )
    controller = ContainerOpsController()
    result = start_runner_container(controller, options, runner=fake_runner)

    assert called == []
    assert "up" in result.plan.argv
    assert result.daemon_lock_check_skipped is True


def test_start_runner_container_no_repo_id_skips_lock_check(
    tmp_path: Path, fake_global_iar: Path
) -> None:
    """未传 ``repo_id`` 时跳过 daemon lock 检查（标 ``daemon_lock_check_skipped=True``）。"""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    called: list[tuple] = []

    def fake_runner(argv, *, env, cwd, check):
        called.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    options = StartRunnerContainerOptions(
        repo_path=repo_path,
        repo_id="",
        process_registry_path=str(fake_global_iar / ".iar" / "processes.json"),
    )
    controller = ContainerOpsController()
    result = start_runner_container(controller, options, runner=fake_runner)

    assert result.daemon_lock_check_skipped is True
    assert len(called) == 1


def test_start_runner_container_stale_daemon_lock_is_not_blocking(
    tmp_path: Path, fake_global_iar: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目标 repo_id 的 daemon lock 是 stale PID（已死）时不应阻止启动。"""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    stale_pid = 2_147_483_647
    lock_dir = fake_global_iar / ".iar" / "daemon-locks"
    lock_dir.mkdir(parents=True)
    (lock_dir / "keda.lock").write_text(f"{stale_pid}\n", encoding="utf-8")

    from backend.core.use_cases import agent_runner_container as facade_module

    monkeypatch.setattr(facade_module, "_is_pid_alive", lambda pid: pid != stale_pid)

    called: list[tuple] = []

    def fake_runner(argv, *, env, cwd, check):
        called.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    options = StartRunnerContainerOptions(
        repo_path=repo_path,
        repo_id="keda",
        process_registry_path=str(fake_global_iar / ".iar" / "processes.json"),
    )
    controller = ContainerOpsController()
    start_runner_container(controller, options, runner=fake_runner)
    assert len(called) == 1


def test_stop_runner_container_forwards_to_engines(tmp_path: Path) -> None:
    """``stop_runner_container`` 透传到 engines 实现。"""
    compose = tmp_path / "docker-compose.runner.yml"
    compose.write_text("", encoding="utf-8")

    captured: list[tuple] = []

    def fake_runner(argv, *, env, cwd, check):
        captured.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    controller = ContainerOpsController()
    argv = stop_runner_container(controller, compose, runner=fake_runner)
    assert captured[0] == argv
    assert "down" in argv


def test_stream_runner_container_logs_forwards_to_engines(tmp_path: Path) -> None:
    """``stream_runner_container_logs`` 透传到 engines 实现。"""
    compose = tmp_path / "docker-compose.runner.yml"
    compose.write_text("", encoding="utf-8")

    captured: list[tuple] = []

    def fake_runner(argv, *, env, cwd, check):
        captured.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    controller = ContainerOpsController()
    argv = stream_runner_container_logs(controller, compose, runner=fake_runner)
    assert "logs" in argv
    assert "--follow" in argv
    assert captured[0] == argv


def test_stream_runner_container_logs_no_follow(tmp_path: Path) -> None:
    """``follow=False`` 时不含 ``--follow`` 跟随标志。"""
    compose = tmp_path / "docker-compose.runner.yml"
    compose.write_text("", encoding="utf-8")

    captured: list[tuple] = []

    def fake_runner(argv, *, env, cwd, check):
        captured.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    controller = ContainerOpsController()
    argv = stream_runner_container_logs(controller, compose, follow=False, runner=fake_runner)
    assert "--follow" not in argv


def test_core_facade_does_not_import_engines() -> None:
    """架构守门：core facade 不直接 import ``backend.engines.*``。"""
    facade_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "backend"
        / "core"
        / "use_cases"
        / "agent_runner_container.py"
    )
    source = facade_path.read_text(encoding="utf-8")
    forbidden = re.compile(r"^\s*from\s+backend\.engines", flags=re.MULTILINE)
    assert forbidden.search(source) is None, (
        "agent_runner_container.py (core) must not directly import backend.engines.* — "
        "route through IContainerRunnerController / IContainerAuthImporter ports."
    )
