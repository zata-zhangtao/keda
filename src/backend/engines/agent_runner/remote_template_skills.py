"""从远程模板仓库同步 IAR 所需的用户级 Skills。"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.shared.interfaces.agent_runner import IProcessRunner


REMOTE_TEMPLATE_SKILLS_REPOSITORY_URL = "https://github.com/zata-zhangtao/zata-codes-template.git"
"""用户级 Skill 的唯一远程内容来源。"""

REMOTE_TEMPLATE_SKILLS_REF = "main"
"""每次 ``iar init`` 要获取的模板分支。"""

REMOTE_TEMPLATE_SKILL_NAMES: tuple[str, ...] = ("prd", "code-reviewer")
"""IAR 安装且仅安装的远程模板 Skills。"""

_CC_SWITCH_SKILLS_DIR_ENV_VAR = "CC_SWITCH_SKILLS_DIR"
_USER_SKILLS_ROOT_RELATIVE_PATHS: tuple[Path, ...] = (
    Path(".cc-switch") / "skills",
    Path(".codex") / "skills",
    Path(".claude") / "skills",
    Path(".kimi-code") / "skills",
)
_FALLBACK_USER_SKILLS_ROOT_RELATIVE_PATH = Path(".codex") / "skills"

_REMOTE_SKILL_PROTECTED_FILENAME = "SKILL.md"
"""用于识别用户同名 Skill 是否被改动的最小契约文件。"""


class RemoteTemplateSkillInstallError(RuntimeError):
    """远程模板 Skill 拉取或同步失败时抛出。"""


@dataclass(frozen=True)
class RemoteTemplateSkillInstallOptions:
    """控制远程模板 Skill 同步的选项。

    Attributes:
        process_runner: 执行受限 Git 命令的端口。
        dry_run: 是否只返回用户级目标路径而不访问网络或写入文件。
        user_home_path: 测试或受控运行时指定的用户主目录；为空时使用实际主目录。
        force: 是否允许覆盖用户修改过的同名 Skill。详见
            :func:`install_remote_template_skills`。
    """

    process_runner: IProcessRunner
    dry_run: bool = False
    user_home_path: Path | None = None
    force: bool = False


@dataclass(frozen=True)
class RemoteTemplateSkillInstallResult:
    """远程模板 Skill 同步结果。

    Attributes:
        target_skills_root: 写入 ``prd`` 与 ``code-reviewer`` 的用户级根目录。
        installed_skill_names: 实际或计划写入的 Skill 白名单名称。
        skipped_skill_names: 与远程模板一致因而无需再次覆盖的 Skill 名称,
            可避免重复写入误触更新 ``mtime``。
        overwritten_skill_names: 本次确实替换为远程模板副本的 Skill 名称,
            含 ``force=True`` 下被覆盖的用户自有副本。
        dry_run: 是否没有执行任何网络或文件系统写入。
    """

    target_skills_root: Path
    installed_skill_names: tuple[str, ...]
    skipped_skill_names: tuple[str, ...] = ()
    overwritten_skill_names: tuple[str, ...] = ()
    dry_run: bool = False


def resolve_user_skill_install_root(user_home_path: Path | None = None) -> Path:
    """解析 ``iar init`` 的用户级 Skill 安装目标。

    优先尊重 ``CC_SWITCH_SKILLS_DIR``；否则选择第一个已经存在的
    cc-switch、Codex、Claude 或 Kimi Code 配置目录。全都不存在时回退到
    ``~/.codex/skills``，避免初始化流程依赖交互式选择。

    Args:
        user_home_path: 可选的用户主目录覆盖，主要供测试使用。

    Returns:
        用户级 Skill 根目录；函数本身不创建目录。
    """
    configured_skills_root = os.environ.get(_CC_SWITCH_SKILLS_DIR_ENV_VAR)
    if configured_skills_root:
        return Path(configured_skills_root).expanduser()

    effective_home_path = Path.home() if user_home_path is None else user_home_path
    candidate_skill_roots = tuple(
        effective_home_path / relative_skills_root
        for relative_skills_root in _USER_SKILLS_ROOT_RELATIVE_PATHS
    )
    for candidate_skills_root in candidate_skill_roots:
        if candidate_skills_root.parent.is_dir():
            return candidate_skills_root
    return effective_home_path / _FALLBACK_USER_SKILLS_ROOT_RELATIVE_PATH


def install_remote_template_skills(
    options: RemoteTemplateSkillInstallOptions,
) -> RemoteTemplateSkillInstallResult:
    """从远程模板仓库安装 IAR 需要的两个用户级 Skill。

    仅通过 sparse checkout 下载 ``skills/prd`` 与 ``skills/code-reviewer``，
    不执行远程仓库脚本，也不会读取或写入目标项目的 Skill 目录。

    Args:
        options: Git 执行端口、dry-run 标记和可选用户主目录。

    Returns:
        实际或计划写入的用户级目录与 Skill 名称。

    Raises:
        RemoteTemplateSkillInstallError: 远程仓库缺少所需目录，目录包含符号链接，
            或目标存在 ``SKILL.md`` 与远程不同的同名 Skill 且 ``force`` 为 False。
    """
    target_skills_root = resolve_user_skill_install_root(options.user_home_path)
    if options.dry_run:
        return RemoteTemplateSkillInstallResult(
            target_skills_root=target_skills_root,
            installed_skill_names=REMOTE_TEMPLATE_SKILL_NAMES,
            dry_run=True,
        )

    with tempfile.TemporaryDirectory(prefix="iar-template-skills-") as temporary_directory:
        temporary_root_path = Path(temporary_directory)
        checkout_path = temporary_root_path / "template"
        options.process_runner.run(
            (
                "git",
                "clone",
                "--depth=1",
                "--branch",
                REMOTE_TEMPLATE_SKILLS_REF,
                "--filter=blob:none",
                "--sparse",
                REMOTE_TEMPLATE_SKILLS_REPOSITORY_URL,
                str(checkout_path),
            ),
            cwd=temporary_root_path,
            timeout=120,
            label="IAR remote template skill download",
        )
        options.process_runner.run(
            (
                "git",
                "-C",
                str(checkout_path),
                "sparse-checkout",
                "set",
                *(f"skills/{skill_name}" for skill_name in REMOTE_TEMPLATE_SKILL_NAMES),
            ),
            cwd=checkout_path,
            timeout=60,
            label="IAR remote template skill checkout",
        )
        source_skill_paths = tuple(
            checkout_path / "skills" / skill_name for skill_name in REMOTE_TEMPLATE_SKILL_NAMES
        )
        for skill_name, source_skill_path in zip(
            REMOTE_TEMPLATE_SKILL_NAMES, source_skill_paths, strict=True
        ):
            _validate_remote_skill_directory(source_skill_path, skill_name)
        overwritten_skill_names, skipped_skill_names = _write_remote_skills_into_user_root(
            source_skill_paths=source_skill_paths,
            target_skills_root=target_skills_root,
            force=options.force,
        )

    return RemoteTemplateSkillInstallResult(
        target_skills_root=target_skills_root,
        installed_skill_names=REMOTE_TEMPLATE_SKILL_NAMES,
        skipped_skill_names=skipped_skill_names,
        overwritten_skill_names=overwritten_skill_names,
        dry_run=False,
    )


def _validate_remote_skill_directory(source_skill_path: Path, skill_name: str) -> None:
    if source_skill_path.is_symlink():
        raise RemoteTemplateSkillInstallError(
            f"Remote template skill root must not be a symlink: {source_skill_path}"
        )
    if not (source_skill_path / _REMOTE_SKILL_PROTECTED_FILENAME).is_file():
        raise RemoteTemplateSkillInstallError(
            f"Remote template is missing skills/{skill_name}/{_REMOTE_SKILL_PROTECTED_FILENAME}"
        )
    if any(remote_skill_path.is_symlink() for remote_skill_path in source_skill_path.rglob("*")):
        raise RemoteTemplateSkillInstallError(
            f"Remote template skill contains unsupported symlink: {source_skill_path}"
        )


def _read_remote_skill_contract(target_skill_path: Path) -> bytes | None:
    """读取现有 Skill 的 ``SKILL.md`` 字节;不存在或为目录时返回 ``None``。"""
    contract_path = target_skill_path / _REMOTE_SKILL_PROTECTED_FILENAME
    if not contract_path.is_file():
        return None
    return contract_path.read_bytes()


def _write_remote_skills_into_user_root(
    *,
    source_skill_paths: tuple[Path, ...],
    target_skills_root: Path,
    force: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """将远程 Skill 同步到用户级目录,处理用户同名 Skill 的保护。

    当目标 Skill 的 ``SKILL.md`` 与远程逐字节一致时视为同一份,跳过覆盖
    以避免无意义地刷新 ``mtime``;只有内容被用户改动过且未传 ``force``
    时才抛错拒绝,以防静默丢失用户私有定制。其余附属文件 (``scripts/`` 等)
    与本地 ``.git/`` 生成的索引副产物不参与比对,允许共存。

    Returns:
        ``(overwritten_skill_names, skipped_skill_names)``。

    Raises:
        RemoteTemplateSkillInstallError: 目标已是 symlink,或目标 ``SKILL.md``
            与远程不同但当前调用未声明 ``force``。
    """
    overwritten_skill_names: list[str] = []
    skipped_skill_names: list[str] = []
    for skill_name, source_skill_path in zip(
        REMOTE_TEMPLATE_SKILL_NAMES, source_skill_paths, strict=True
    ):
        target_skill_path = target_skills_root / skill_name
        if target_skill_path.is_symlink():
            raise RemoteTemplateSkillInstallError(
                f"Refusing to write remote skill through symlink: {target_skill_path}"
            )
        if target_skill_path.exists():
            remote_contract_bytes = (
                source_skill_path / _REMOTE_SKILL_PROTECTED_FILENAME
            ).read_bytes()
            local_contract_bytes = _read_remote_skill_contract(target_skill_path)
            if local_contract_bytes == remote_contract_bytes:
                skipped_skill_names.append(skill_name)
                continue
            if not force:
                raise RemoteTemplateSkillInstallError(
                    f"Refusing to overwrite user-owned skill '{skill_name}' at "
                    f"{target_skill_path}; its {_REMOTE_SKILL_PROTECTED_FILENAME} "
                    f"differs from the remote template. Pass force=True to replace "
                    f"it, or back up the existing directory manually."
                )
            shutil.rmtree(target_skill_path)
        target_skills_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_skill_path, target_skill_path, dirs_exist_ok=True)
        overwritten_skill_names.append(skill_name)
    return tuple(overwritten_skill_names), tuple(skipped_skill_names)
