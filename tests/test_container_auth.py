"""Tests for ``backend.engines.agent_runner.container_auth``.

覆盖：
- 白名单复制：只复制 settings.json / skills 等列入 ``include_top_level`` 的条目
- 排除运行时状态：history / sessions / cache / paste-cache 等不被复制
- 缺失源目录时跳过该 agent 并返回 ``skipped=True``
- 目标目录权限 0700
- 容器认证根目录不存在时自动创建
- ``container-auth`` 路径写入 ``~/.iar/info/exclude``（在 iar 全局目录是 git 仓库时）
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from backend.engines.agent_runner.container_auth import (
    CONTAINER_AUTH_PERMISSIONS,
    AgentImportSpec,
    ContainerAuthImportResult,
    SUPPORTED_AGENT_SPECS,
    container_auth_dir,
    ensure_container_auth_root,
    import_agent_auth,
    import_container_auth,
)


def _seed_claude_source(source_dir: Path) -> None:
    """在 ``source_dir`` 下生成一个最小的 claude 样例，含 settings/skills/运行时。"""
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "settings.json").write_text(
        '{"env": {"ANTHROPIC_AUTH_TOKEN": "secret"}}', encoding="utf-8"
    )
    skills_dir = source_dir / "skills"
    skills_dir.mkdir()
    (skills_dir / "code-reviewer").mkdir()
    (skills_dir / "code-reviewer" / "SKILL.md").write_text("# code-reviewer", encoding="utf-8")
    (skills_dir / "idea-inbox").mkdir()
    (skills_dir / "prd").mkdir()
    # 运行时状态——必须不被复制
    (source_dir / "history.jsonl").write_text("history", encoding="utf-8")
    (source_dir / "file-history").mkdir()
    (source_dir / "paste-cache").mkdir()
    (source_dir / "cache").mkdir()
    (source_dir / "backups").mkdir()


def _seed_codex_source(source_dir: Path) -> None:
    """在 ``source_dir`` 下生成一个最小的 codex 样例。"""
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "auth.json").write_text('{"OPENAI_API_KEY": "sk-xxx"}', encoding="utf-8")
    skills_dir = source_dir / "skills"
    skills_dir.mkdir()
    (skills_dir / "code-reviewer").mkdir()
    # 运行时状态
    (source_dir / "sessions").mkdir()
    (source_dir / "cache").mkdir()
    (source_dir / ".codex-global-state.json").write_text("{}", encoding="utf-8")


def _seed_kimi_source(source_dir: Path) -> None:
    """在 ``source_dir`` 下生成一个最小的 kimi 样例。"""
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "config.toml").write_text("[kimi]\n", encoding="utf-8")
    (source_dir / "credentials").mkdir()
    (source_dir / "oauth").mkdir()
    (source_dir / "device_id").write_text("device-123", encoding="utf-8")
    skills_dir = source_dir / "skills"
    skills_dir.mkdir()
    # 运行时状态
    (source_dir / "sessions").mkdir()
    (source_dir / "logs").mkdir()
    (source_dir / "session_index.jsonl").write_text("", encoding="utf-8")


def test_supported_agent_specs_have_three_agents() -> None:
    """规格表恰好覆盖 claude/codex/kimi 三个 agent。"""
    names = [spec.agent_name for spec in SUPPORTED_AGENT_SPECS]
    assert names == ["claude", "codex", "kimi"]


def test_container_auth_dir_under_global_iar() -> None:
    """``container_auth_dir`` 落在全局 iar 目录下。"""
    global_iar = Path("/tmp/iar-fake-home")
    assert container_auth_dir(global_iar) == global_iar / "container-auth"


def test_ensure_container_auth_root_creates_with_0700(tmp_path: Path) -> None:
    """目标根目录自动创建并权限为 0700。"""
    target = tmp_path / "container-auth"
    ensure_container_auth_root(target)
    assert target.is_dir()
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == CONTAINER_AUTH_PERMISSIONS


def test_import_agent_auth_copies_whitelist_only(tmp_path: Path) -> None:
    """白名单内条目被复制；运行时状态被排除。"""
    source = tmp_path / "claude"
    _seed_claude_source(source)

    target_root = tmp_path / "container-auth"
    spec = AgentImportSpec(
        agent_name="claude",
        source_dir=source,
        target_subdir="claude",
        include_top_level=("settings.json", "skills"),
        exclude_subpaths=frozenset({"history.jsonl", "file-history", "paste-cache"}),
    )

    result = import_agent_auth(spec, target_root)

    target = target_root / "claude"
    assert result.skipped is False
    assert set(result.copied_entries) == {"settings.json", "skills"}
    assert (target / "settings.json").is_file()
    skills_dir = target / "skills"
    assert skills_dir.is_dir()
    assert (skills_dir / "code-reviewer" / "SKILL.md").is_file()
    # 运行时状态必须不存在
    assert not (target / "history.jsonl").exists()
    assert not (target / "file-history").exists()
    assert not (target / "paste-cache").exists()
    assert not (target / "cache").exists()
    assert not (target / "backups").exists()


def test_import_agent_auth_skips_missing_source(tmp_path: Path) -> None:
    """源目录不存在时跳过并 WARN；不 raise。"""
    target_root = tmp_path / "container-auth"
    spec = AgentImportSpec(
        agent_name="missing-agent",
        source_dir=tmp_path / "nonexistent",
        target_subdir="missing-agent",
        include_top_level=("settings.json",),
    )

    result = import_agent_auth(spec, target_root)

    assert result.skipped is True
    assert result.copied_entries == ()
    assert "source not found" in (result.skip_reason or "")


def test_import_container_auth_full_flow(tmp_path: Path, monkeypatch) -> None:
    """``import_container_auth`` 编排：每个 agent 都跑，结果聚合。"""
    # 给三个 agent 各构造一个临时源目录
    claude_src = tmp_path / "host-claude"
    codex_src = tmp_path / "host-codex"
    kimi_src = tmp_path / "host-kimi"
    _seed_claude_source(claude_src)
    _seed_codex_source(codex_src)
    _seed_kimi_source(kimi_src)

    # 用临时全局 iar 目录替换 Path.home()
    fake_home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # 用 monkeypatch 把各 spec 的 source_dir 指向临时源
    from backend.engines.agent_runner import container_auth as ca_module

    specs = []
    for original, new_src in zip(
        ca_module.SUPPORTED_AGENT_SPECS, (claude_src, codex_src, kimi_src)
    ):
        specs.append(
            AgentImportSpec(
                agent_name=original.agent_name,
                source_dir=new_src,
                target_subdir=original.target_subdir,
                include_top_level=original.include_top_level,
                exclude_subpaths=original.exclude_subpaths,
            )
        )

    result: ContainerAuthImportResult = import_container_auth(
        global_iar_dir=fake_home / ".iar", specs=tuple(specs)
    )

    target_root = fake_home / ".iar" / "container-auth"
    assert result.container_auth_dir == target_root
    # 根目录权限 0700
    assert stat.S_IMODE(target_root.stat().st_mode) == CONTAINER_AUTH_PERMISSIONS

    by_agent = {r.agent_name: r for r in result.agent_results}
    assert by_agent["claude"].skipped is False
    assert "settings.json" in by_agent["claude"].copied_entries
    assert "skills" in by_agent["claude"].copied_entries
    assert by_agent["codex"].skipped is False
    assert "auth.json" in by_agent["codex"].copied_entries
    assert by_agent["kimi"].skipped is False
    assert "config.toml" in by_agent["kimi"].copied_entries


def test_import_container_auth_skips_missing_agent_without_failing(
    tmp_path: Path, monkeypatch
) -> None:
    """三个 agent 中部分源缺失时，其它仍正常导入；命令级不 fail。"""
    claude_src = tmp_path / "host-claude"
    _seed_claude_source(claude_src)

    fake_home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    specs = [
        AgentImportSpec(
            agent_name="claude",
            source_dir=claude_src,
            target_subdir="claude",
            include_top_level=("settings.json", "skills"),
        ),
        # codex 不存在源目录
        AgentImportSpec(
            agent_name="codex",
            source_dir=tmp_path / "missing-codex",
            target_subdir="codex",
            include_top_level=("auth.json",),
        ),
    ]

    result = import_container_auth(global_iar_dir=fake_home / ".iar", specs=tuple(specs))

    by_agent = {r.agent_name: r for r in result.agent_results}
    assert by_agent["claude"].skipped is False
    assert by_agent["codex"].skipped is True
    # claude 仍被复制
    assert (fake_home / ".iar" / "container-auth" / "claude" / "settings.json").is_file()
    # codex 子目录不应该被生成
    assert not (fake_home / ".iar" / "container-auth" / "codex").exists()


def test_container_auth_gitignore_when_global_iar_is_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """当 ``~/.iar`` 本身是 git 仓库时，``container-auth`` 路径被写入 info/exclude。"""
    fake_home = tmp_path / "fake-home"
    iar_dir = fake_home / ".iar"
    iar_dir.mkdir(parents=True)
    (iar_dir / ".git").mkdir()  # 标记为 git 仓库（仅 .git 存在即可触发判定）
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    specs = (
        AgentImportSpec(
            agent_name="dummy",
            source_dir=tmp_path / "missing",
            target_subdir="dummy",
            include_top_level=(),
        ),
    )
    result = import_container_auth(global_iar_dir=iar_dir, specs=specs)

    exclude_file = iar_dir / "info" / "exclude"
    assert exclude_file.is_file()
    content = exclude_file.read_text(encoding="utf-8")
    assert "/container-auth" in content
    assert result.gitignore_protected is True


def test_container_auth_gitignore_skipped_when_not_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``~/.iar`` 不是 git 仓库时，gitignore 步骤跳过但命令仍 exit 0。"""
    fake_home = tmp_path / "fake-home"
    iar_dir = fake_home / ".iar"
    iar_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    specs = (
        AgentImportSpec(
            agent_name="dummy",
            source_dir=tmp_path / "missing",
            target_subdir="dummy",
            include_top_level=(),
        ),
    )
    result = import_container_auth(global_iar_dir=iar_dir, specs=specs)

    # 不会创建 info/exclude；状态返回 False（best-effort）
    assert not (iar_dir / "info" / "exclude").exists()
    assert result.gitignore_protected is False


@pytest.mark.parametrize(
    "agent_name,include_top_level",
    [
        ("claude", ("settings.json", "skills")),
        ("codex", ("auth.json", "skills")),
        ("kimi", ("config.toml", "credentials", "oauth", "device_id", "skills")),
    ],
)
def test_each_agent_whitelist_contract(
    tmp_path: Path, agent_name: str, include_top_level: tuple[str, ...]
) -> None:
    """每个 agent 的 ``include_top_level`` 至少含 auth + skills（kimi 多含 device_id/oauth）。"""
    matching = [spec for spec in SUPPORTED_AGENT_SPECS if spec.agent_name == agent_name]
    assert len(matching) == 1
    spec = matching[0]
    assert spec.include_top_level == include_top_level
    # 必须有 exclude 子路径
    assert spec.exclude_subpaths
    # 排除集必须含典型运行时条目
    if agent_name == "claude":
        assert "history.jsonl" in spec.exclude_subpaths
    if agent_name == "codex":
        assert "sessions" in spec.exclude_subpaths
    if agent_name == "kimi":
        assert "session_index.jsonl" in spec.exclude_subpaths
