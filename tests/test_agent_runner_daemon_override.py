"""Tests for repository-local daemon override (TTL reclaim round-trip)."""

from __future__ import annotations

from pathlib import Path

from backend.engines.agent_runner.repository_local import (
    settings_to_toml_string,
)
from backend.infrastructure.config.settings import (
    AgentRunnerLocalSettings as LiveAgentRunnerLocalSettings,
    AgentRunnerRepositoryMetadataSettings,
    load_agent_runner_local_settings,
)


def test_local_settings_round_trip_daemon_override(tmp_path: Path) -> None:
    """写入 .iar.toml 含 [agent_runner.daemon] 字段 → load 出来字段值正确。

    验证:用户取消 _IAR_DAEMON_EXAMPLE 注释修改 reclaim_ttl_seconds 后,
    load_agent_runner_local_settings 能正确读出并保持其他字段默认。
    """
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    iar_toml = repo / ".iar.toml"
    iar_toml.write_text(
        "[agent_runner.repository]\n"
        'id = "fake-repo"\n'
        "[agent_runner.daemon]\n"
        "reclaim_ttl_seconds = 600\n"
        "reclaim_stale_running = false\n",
        encoding="utf-8",
    )

    loaded = load_agent_runner_local_settings(repo)
    assert loaded is not None
    assert loaded.daemon is not None
    assert loaded.daemon.reclaim_ttl_seconds == 600
    assert loaded.daemon.reclaim_stale_running is False
    # 未在 .iar.toml 写出的字段 → 继承全局默认(来自 AgentRunnerDaemonSettings)
    assert loaded.daemon.review_interval_seconds == 120


def test_local_settings_without_daemon_override(tmp_path: Path) -> None:
    """不写 [agent_runner.daemon] → loaded.daemon is None(由 merge 层兜底全局默认)。"""
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    iar_toml = repo / ".iar.toml"
    iar_toml.write_text(
        "[agent_runner.repository]\n" 'id = "fake-repo"\n',
        encoding="utf-8",
    )

    loaded = load_agent_runner_local_settings(repo)
    assert loaded is not None
    assert loaded.daemon is None


def test_init_renders_daemon_example_block() -> None:
    """init 模板渲染结果包含 _IAR_DAEMON_EXAMPLE 注释块。

    验证:用户 ``iar init`` 后能在 .iar.toml 看到 [agent_runner.daemon] 注释示例,
    取消注释即可改 reclaim_ttl_seconds 等字段。
    """
    settings = LiveAgentRunnerLocalSettings(
        repository=AgentRunnerRepositoryMetadataSettings(id="fake-repo", enabled=True)
    )
    rendered = settings_to_toml_string(settings)

    assert "[agent_runner.daemon]" in rendered
    assert "reclaim_ttl_seconds" in rendered
    assert "reclaim_stale_running" in rendered
    # 全 5 个 daemon 字段都在示例里
    assert "review_interval_seconds" in rendered
    assert "run_interval_seconds" in rendered
    assert "max_deliberation_issues" in rendered
    # 示例默认 3 小时(10800)出现在注释里
    assert "10800" in rendered
    # daemon section 注释存在
    assert "Daemon 轮询与 reclaim 配置" in rendered


def test_global_daemon_default_is_3_hours() -> None:
    """默认 reclaim_ttl_seconds = 10800(3 小时)"""
    from backend.infrastructure.config.settings import AgentRunnerDaemonSettings

    settings = AgentRunnerDaemonSettings()
    assert settings.reclaim_ttl_seconds == 10800
