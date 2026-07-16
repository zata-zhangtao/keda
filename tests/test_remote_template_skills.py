"""远程模板用户级 Skill 同步的测试。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import CommandResult
from backend.engines.agent_runner.remote_template_skills import (
    REMOTE_TEMPLATE_SKILL_NAMES,
    REMOTE_TEMPLATE_SKILLS_REPOSITORY_URL,
    RemoteTemplateSkillInstallError,
    RemoteTemplateSkillInstallOptions,
    install_remote_template_skills,
    resolve_user_skill_install_root,
)


class FakeRemoteTemplateProcessRunner:
    """模拟 sparse checkout，并在克隆目录写入受控的远程 Skill 内容。"""

    def __init__(
        self, *, include_skill_names: tuple[str, ...] = REMOTE_TEMPLATE_SKILL_NAMES
    ) -> None:
        self.include_skill_names = include_skill_names
        self.command_tuples: list[tuple[str, ...]] = []

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        **_command_options: object,
    ) -> CommandResult:
        command_tuple = tuple(command)
        self.command_tuples.append(command_tuple)
        if command_tuple[:2] == ("git", "clone"):
            checkout_path = Path(command_tuple[-1])
            for skill_name in self.include_skill_names:
                source_skill_path = checkout_path / "skills" / skill_name
                source_skill_path.mkdir(parents=True)
                (source_skill_path / "SKILL.md").write_text(
                    f"remote {skill_name}", encoding="utf-8"
                )
                (source_skill_path / "reference.md").write_text(
                    f"reference {skill_name}", encoding="utf-8"
                )
        return CommandResult(
            command=command_tuple,
            return_code=0,
            stdout="",
            stderr="",
        )


def test_resolve_user_skill_install_root_supports_kimi_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kimi Code 是缺少 cc-switch、Codex 与 Claude 时的用户级安装目标。"""
    user_home_path = tmp_path / "home"
    (user_home_path / ".kimi-code").mkdir(parents=True)
    monkeypatch.delenv("CC_SWITCH_SKILLS_DIR", raising=False)

    assert (
        resolve_user_skill_install_root(user_home_path) == user_home_path / ".kimi-code" / "skills"
    )


def test_resolve_user_skill_install_root_prefers_configured_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC_SWITCH_SKILLS_DIR 必须覆盖所有自动探测到的用户目录。"""
    configured_skills_path = tmp_path / "configured-skills"
    monkeypatch.setenv("CC_SWITCH_SKILLS_DIR", str(configured_skills_path))

    assert resolve_user_skill_install_root(tmp_path / "home") == configured_skills_path


def test_install_remote_template_skills_downloads_only_required_user_skills(tmp_path: Path) -> None:
    """远程 sparse checkout 只把 prd 与 code-reviewer 写入用户级 Kimi Code 目录。"""
    user_home_path = tmp_path / "home"
    (user_home_path / ".kimi-code").mkdir(parents=True)
    fake_process_runner = FakeRemoteTemplateProcessRunner()

    install_result = install_remote_template_skills(
        RemoteTemplateSkillInstallOptions(
            process_runner=fake_process_runner,
            user_home_path=user_home_path,
        )
    )

    target_skills_root = user_home_path / ".kimi-code" / "skills"
    assert install_result.target_skills_root == target_skills_root
    assert install_result.installed_skill_names == REMOTE_TEMPLATE_SKILL_NAMES
    assert not install_result.dry_run
    for skill_name in REMOTE_TEMPLATE_SKILL_NAMES:
        assert (target_skills_root / skill_name / "SKILL.md").read_text(encoding="utf-8") == (
            f"remote {skill_name}"
        )
    assert not (target_skills_root / "unrelated-skill").exists()
    assert fake_process_runner.command_tuples[0][-2] == REMOTE_TEMPLATE_SKILLS_REPOSITORY_URL
    assert fake_process_runner.command_tuples[1][-2:] == (
        "skills/prd",
        "skills/code-reviewer",
    )
    assert not (target_skills_root / "unrelated-skill").exists()
    assert fake_process_runner.command_tuples[0][-2] == REMOTE_TEMPLATE_SKILLS_REPOSITORY_URL
    assert fake_process_runner.command_tuples[1][-2:] == (
        "skills/prd",
        "skills/code-reviewer",
    )
    stale_skill_file = target_skills_root / "prd" / "stale.md"
    stale_skill_file.write_text("stale", encoding="utf-8")

    second_install_result = install_remote_template_skills(
        RemoteTemplateSkillInstallOptions(
            process_runner=fake_process_runner,
            user_home_path=user_home_path,
        )
    )

    # ``iar init`` 现在对比 SKILL.md 字节:内容一致就跳过,不再无差别
    # 清空整个目录,因此 stale.md 这种 iar 不拥有的副产物应原样保留。
    assert stale_skill_file.exists()
    assert set(second_install_result.skipped_skill_names) == {"prd", "code-reviewer"}


def test_install_remote_template_skills_rejects_missing_remote_skill(tmp_path: Path) -> None:
    """远程模板少了受限白名单中的任一 Skill 时不得写入用户目录。"""
    fake_process_runner = FakeRemoteTemplateProcessRunner(include_skill_names=("prd",))

    with pytest.raises(RemoteTemplateSkillInstallError, match="code-reviewer"):
        install_remote_template_skills(
            RemoteTemplateSkillInstallOptions(
                process_runner=fake_process_runner,
                user_home_path=tmp_path / "home",
            )
        )
    assert not (tmp_path / "home" / ".codex" / "skills").exists()


def _build_process_runner_for_remote_skill_payload(
    remote_skill_payload: dict[str, bytes],
    *,
    include_skill_names: tuple[str, ...] = REMOTE_TEMPLATE_SKILL_NAMES,
) -> FakeRemoteTemplateProcessRunner:
    """构造一个可注入 ``SKILL.md`` 内容与附文件的 fake process runner。"""

    class _CustomizableRemoteTemplateProcessRunner(FakeRemoteTemplateProcessRunner):
        def run(  # type: ignore[override]
            self,
            command: Sequence[str],
            *,
            cwd: Path,
            **_command_options: object,
        ) -> CommandResult:
            self.command_tuples.append(tuple(command))
            if tuple(command)[:2] == ("git", "clone"):
                checkout_path = Path(command[-1])
                for skill_name in include_skill_names:
                    source_skill_path = checkout_path / "skills" / skill_name
                    source_skill_path.mkdir(parents=True)
                    payload_bytes = remote_skill_payload[skill_name]
                    (source_skill_path / "SKILL.md").write_bytes(payload_bytes)
            return CommandResult(
                command=tuple(command),
                return_code=0,
                stdout="",
                stderr="",
            )

    return _CustomizableRemoteTemplateProcessRunner(
        include_skill_names=include_skill_names,
    )


def test_install_remote_template_skills_skips_when_skill_md_matches(tmp_path: Path) -> None:
    """现有 Skill 的 ``SKILL.md`` 与远程完全一致时跳过覆盖、不刷新 ``mtime``。"""
    user_home_path = tmp_path / "home"
    (user_home_path / ".kimi-code").mkdir(parents=True)
    target_skills_root = user_home_path / ".kimi-code" / "skills"
    target_skill_path = target_skills_root / "prd"
    target_skill_path.mkdir(parents=True)
    matching_payload = b"identical remote prd body"
    (target_skill_path / "SKILL.md").write_bytes(matching_payload)
    (target_skill_path / "user-extra-note.md").write_bytes(b"keep this")
    original_mtime_ns = target_skill_path.stat().st_mtime_ns
    fake_process_runner = _build_process_runner_for_remote_skill_payload(
        remote_skill_payload={"prd": matching_payload, "code-reviewer": b"remote code-reviewer"},
    )

    install_result = install_remote_template_skills(
        RemoteTemplateSkillInstallOptions(
            process_runner=fake_process_runner,
            user_home_path=user_home_path,
        )
    )

    assert install_result.skipped_skill_names == ("prd",)
    assert install_result.overwritten_skill_names == ("code-reviewer",)
    assert (target_skill_path / "SKILL.md").read_bytes() == matching_payload
    assert (target_skill_path / "user-extra-note.md").read_bytes() == b"keep this"
    assert target_skill_path.stat().st_mtime_ns == original_mtime_ns


def test_install_remote_template_skills_refuses_overwrite_on_user_owned_skill(
    tmp_path: Path,
) -> None:
    """目标 SKILL.md 与远程不同且未声明 ``force`` 时拒绝,原文件字节不变。"""
    user_home_path = tmp_path / "home"
    (user_home_path / ".kimi-code").mkdir(parents=True)
    target_skills_root = user_home_path / ".kimi-code" / "skills"
    target_skill_path = target_skills_root / "prd"
    target_skill_path.mkdir(parents=True)
    (target_skill_path / "SKILL.md").write_bytes(b"local customized SKILL.md")
    (target_skill_path / "user-extra-note.md").write_bytes(b"keep this too")
    fake_process_runner = _build_process_runner_for_remote_skill_payload(
        remote_skill_payload={"prd": b"remote prd body", "code-reviewer": b"remote cr"},
    )

    with pytest.raises(RemoteTemplateSkillInstallError, match="user-owned skill 'prd'"):
        install_remote_template_skills(
            RemoteTemplateSkillInstallOptions(
                process_runner=fake_process_runner,
                user_home_path=user_home_path,
            )
        )

    assert (target_skill_path / "SKILL.md").read_bytes() == b"local customized SKILL.md"
    assert (target_skill_path / "user-extra-note.md").read_bytes() == b"keep this too"


def test_install_remote_template_skills_overwrites_with_force(tmp_path: Path) -> None:
    """传 ``force=True`` 时即便 SKILL.md 不同也覆盖,记入 ``overwritten_skill_names``。"""
    user_home_path = tmp_path / "home"
    (user_home_path / ".kimi-code").mkdir(parents=True)
    target_skills_root = user_home_path / ".kimi-code" / "skills"
    target_skill_path = target_skills_root / "prd"
    target_skill_path.mkdir(parents=True)
    (target_skill_path / "SKILL.md").write_bytes(b"local customized SKILL.md")
    (target_skill_path / "user-extra-note.md").write_bytes(b"will be replaced")
    new_payload = b"remote prd body"
    fake_process_runner = _build_process_runner_for_remote_skill_payload(
        remote_skill_payload={"prd": new_payload, "code-reviewer": b"remote cr"},
    )

    install_result = install_remote_template_skills(
        RemoteTemplateSkillInstallOptions(
            process_runner=fake_process_runner,
            force=True,
            user_home_path=user_home_path,
        )
    )

    assert install_result.overwritten_skill_names == REMOTE_TEMPLATE_SKILL_NAMES
    assert install_result.skipped_skill_names == ()
    assert (target_skill_path / "SKILL.md").read_bytes() == new_payload


def test_install_remote_template_skills_dry_run_skips_remote_commands(tmp_path: Path) -> None:
    """dry-run 只能显示计划，不能访问远程仓库或创建用户目录。"""
    fake_process_runner = FakeRemoteTemplateProcessRunner()

    install_result = install_remote_template_skills(
        RemoteTemplateSkillInstallOptions(
            process_runner=fake_process_runner,
            dry_run=True,
            user_home_path=tmp_path / "home",
        )
    )

    assert install_result.dry_run
    assert not fake_process_runner.command_tuples
    assert not install_result.target_skills_root.exists()
