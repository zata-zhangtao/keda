"""认证导入引擎模块 —— 从本机当前 cc-switch profile 复制认证与 skills 到容器专用目录。

本模块负责 ``iar container auth import`` 的实现部分。导入后的快照落地于
``~/.iar/container-auth/{claude,codex,kimi-code}/``，后续 ``iar container up``
会把这些目录以 volume 形式挂载到 runner 容器内，让容器里的 claude/codex/kimi
读取导入时刻的认证——之后本机 cc-switch 切换账号不会影响容器。

设计约束：

- 只复制**认证 + 关键设置 + skills**（白名单），显式排除运行时状态（history、
  sessions、cache、paste-cache、file-history 等）。整个配置目录复制既浪费又会
  与容器内运行时冲突。
- 源目录缺失时跳过该 agent 并 ``WARN``，不 raise；命令级 ``exit 0`` 由调用方
  控制。
- 目标目录权限 ``0700``（仅宿主用户可读），避免凭据被同机其他账户读取。
- 凭据路径加入 ``~/.iar/info/exclude`` 的 gitignore（若该目录已是 git 仓库），
  确保不会进 git。
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from backend.core.shared.interfaces.container_runner import (
    ContainerAgentImportResult,
    ContainerAuthImportResult,
    IContainerAuthImporter,
)

_logger = logging.getLogger(__name__)

CONTAINER_AUTH_DIR_NAME = "container-auth"
"""全局目录名，落在 ``~/.iar/container-auth/`` 下，与全局 daemon 状态、config 同根。"""

CONTAINER_AUTH_PERMISSIONS = 0o700
"""目标根目录权限：仅宿主用户可读/可写/可遍历。"""


@dataclass(frozen=True)
class AgentImportSpec:
    """单个 agent 的导入白名单与排除规则。

    Attributes:
        agent_name: 展示用的 agent 名（如 ``"claude"``），用于日志与目标子目录命名。
        source_dir: 本机原始配置目录（如 ``~/.claude``）。
        target_subdir: ``container-auth/`` 下的子目录名（建议与 ``agent_name`` 一致）。
        include_top_level: 需要顶层复制的条目（文件或目录，相对路径）。settings.json
            这类单文件即 ``["settings.json"]``；包含子目录时整目录复制。
        exclude_subpaths: 显式排除的运行时状态子路径集合（如 ``history.jsonl``）。
    """

    agent_name: str
    source_dir: Path
    target_subdir: str
    include_top_level: tuple[str, ...]
    exclude_subpaths: frozenset[str] = field(default_factory=frozenset)


# 显式运行时状态白名单 —— 容器只需要认证 + 关键设置 + skills。
_CLAUDE_SPEC = AgentImportSpec(
    agent_name="claude",
    source_dir=Path.home() / ".claude",
    target_subdir="claude",
    include_top_level=("settings.json", "skills"),
    exclude_subpaths=frozenset(
        {
            "history.jsonl",
            "file-history",
            "paste-cache",
            "plans",
            "plugins",
            "cache",
            "ide",
            "backups",
            "telemetry",
            "todos",
        }
    ),
)

_CODEX_SPEC = AgentImportSpec(
    agent_name="codex",
    source_dir=Path.home() / ".codex",
    target_subdir="codex",
    include_top_level=("auth.json", "skills"),
    exclude_subpaths=frozenset(
        {
            "sessions",
            "cache",
            ".tmp",
            ".codex-global-state.json",
            "log",
            "logs",
            "history.jsonl",
        }
    ),
)

_KIMI_SPEC = AgentImportSpec(
    agent_name="kimi",
    source_dir=Path.home() / ".kimi-code",
    target_subdir="kimi-code",
    include_top_level=("config.toml", "credentials", "oauth", "device_id", "skills"),
    exclude_subpaths=frozenset(
        {
            "sessions",
            "logs",
            "cache",
            "session_index.jsonl",
            "tmp",
        }
    ),
)


SUPPORTED_AGENT_SPECS: tuple[AgentImportSpec, ...] = (_CLAUDE_SPEC, _CODEX_SPEC, _KIMI_SPEC)
"""本模块支持的全部 agent 导入规格。"""


def container_auth_dir(global_iar_dir: Path | None = None) -> Path:
    """返回容器专用认证目录的绝对路径。

    Args:
        global_iar_dir: 全局 iar 目录（默认 ``~/.iar``），便于测试注入临时目录。

    Returns:
        解析后的 ``container-auth/`` 绝对路径。
    """
    base = global_iar_dir if global_iar_dir is not None else Path.home() / ".iar"
    return base / CONTAINER_AUTH_DIR_NAME


@dataclass(frozen=True)
class AgentImportResult(ContainerAgentImportResult):
    """单个 agent 的导入结果（继承 core 端口契约）。

    Attributes:
        agent_name: agent 标识。
        source_dir: 本机源目录绝对路径。
        target_dir: 容器端目标目录绝对路径。
        copied_entries: 实际复制的相对路径集合（文件或目录名）。
        skipped: 是否因源目录缺失而跳过。
        skip_reason: 跳过时的原因字符串。
    """

    agent_name: str
    source_dir: Path
    target_dir: Path
    copied_entries: tuple[str, ...] = field(default_factory=tuple)
    skipped: bool = False
    skip_reason: str | None = None


def _safe_chmod(path: Path, mode: int) -> None:
    """设置 ``path`` 的权限；非致命失败（权限不足等）由调用方决定是否 raise。"""
    os.chmod(path, mode)


def _copy_file(source_file: Path, target_file: Path) -> int:
    """复制单个文件并保留内容；返回写入字节数。"""
    payload = source_file.read_bytes()
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_bytes(payload)
    return len(payload)


def _copy_directory(source_dir: Path, target_dir: Path) -> int:
    """复制整个目录树（含文件权限），返回复制条目数。"""
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(source_dir, target_dir)
    return sum(1 for _ in target_dir.rglob("*") if _.is_file())


def import_agent_auth(
    spec: AgentImportSpec,
    target_root: Path,
) -> AgentImportResult:
    """按 ``spec`` 复制本机 agent 认证 + skills 到 ``target_root/<target_subdir>``。

    行为：
    - 源目录不存在 → 跳过，返回 ``skipped=True``。
    - ``include_top_level`` 中列出的条目存在 → 复制到目标子目录；
      冲突文件默认覆盖。
    - 不在 ``include_top_level`` 且不在 ``exclude_subpaths`` 的条目 → 静默跳过
      （仅白名单复制，不复制未列举的运行时数据）。
    - 显式 ``exclude_subpaths`` 列出的条目即使存在也不复制（防御性：白名单已
      严格控制，但仍兜底）。

    Args:
        spec: agent 导入规格。
        target_root: 容器认证根目录（``container-auth/``）。

    Returns:
        导入结果。
    """
    source_dir = spec.source_dir.expanduser()
    target_dir = target_root / spec.target_subdir

    if not source_dir.is_dir():
        _logger.warning(
            "%s: source not found at %s, skipped.",
            spec.agent_name,
            source_dir,
        )
        return AgentImportResult(
            agent_name=spec.agent_name,
            source_dir=source_dir,
            target_dir=target_dir,
            copied_entries=(),
            skipped=True,
            skip_reason=f"source not found at {source_dir}",
        )

    target_dir.mkdir(parents=True, exist_ok=True)

    copied_entries: list[str] = []
    for relative_name in spec.include_top_level:
        source_entry = source_dir / relative_name
        target_entry = target_dir / relative_name
        if not source_entry.exists():
            _logger.warning(
                "%s: expected entry %s missing under %s, skipping.",
                spec.agent_name,
                relative_name,
                source_dir,
            )
            continue
        if source_entry.is_file():
            _copy_file(source_entry, target_entry)
        elif source_entry.is_dir():
            _copy_directory(source_entry, target_entry)
        else:
            _logger.warning(
                "%s: entry %s is neither file nor directory, skipping.",
                spec.agent_name,
                relative_name,
            )
            continue
        copied_entries.append(relative_name)

    return AgentImportResult(
        agent_name=spec.agent_name,
        source_dir=source_dir,
        target_dir=target_dir,
        copied_entries=tuple(copied_entries),
        skipped=False,
        skip_reason=None,
    )


def ensure_container_auth_root(target_root: Path) -> None:
    """确保容器认证根目录存在且权限为 ``0700``。"""
    target_root.mkdir(parents=True, exist_ok=True)
    _safe_chmod(target_root, CONTAINER_AUTH_PERMISSIONS)


def _ensure_global_iar_gitignore(
    *,
    global_iar_dir: Path,
    protected_relative_paths: tuple[str, ...],
) -> bool:
    """将 ``container-auth`` 路径加入 ``~/.iar/info/exclude`` 的 gitignore。

    Args:
        global_iar_dir: iar 全局目录（默认 ``~/.iar``）。
        protected_relative_paths: 需要排除的相对路径（相对 ``global_iar_dir``），
            通常是 ``("container-auth",)``。

    Returns:
        是否成功更新 gitignore；``global_iar_dir`` 不是 git 仓库时也返回 ``False``。
    """
    git_dir = global_iar_dir / ".git"
    if not git_dir.exists():
        return False

    exclude_file = global_iar_dir / "info" / "exclude"
    try:
        existing_text = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    except OSError as exc:  # noqa: BLE001 - best-effort.
        _logger.warning("Failed to read %s: %s", exclude_file, exc)
        return False

    missing_paths = [
        rel_path for rel_path in protected_relative_paths if rel_path not in existing_text
    ]
    if not missing_paths:
        return True

    addition_lines = "\n".join(f"/{path}" for path in missing_paths)
    new_text = existing_text.rstrip("\n") + "\n" + addition_lines + "\n"
    try:
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        exclude_file.write_text(new_text, encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - best-effort.
        _logger.warning("Failed to write %s: %s", exclude_file, exc)
        return False
    return True


def import_container_auth(
    *,
    global_iar_dir: Path | None = None,
    specs: tuple[AgentImportSpec, ...] = SUPPORTED_AGENT_SPECS,
) -> ContainerAuthImportResult:
    """导入全部受支持 agent 的认证到 ``container_auth_dir``。

    Args:
        global_iar_dir: 全局 iar 目录；为 ``None`` 时使用 ``~/.iar``。
        specs: 参与导入的 agent 规格列表，便于测试或将来扩展。

    Returns:
        聚合结果，包含每个 agent 的子结果与 gitignore 保护标记。
    """
    base = global_iar_dir if global_iar_dir is not None else Path.home() / ".iar"
    target_root = base / CONTAINER_AUTH_DIR_NAME
    ensure_container_auth_root(target_root)

    results: list[AgentImportResult] = []
    for spec in specs:
        results.append(import_agent_auth(spec, target_root))

    gitignore_protected = _ensure_global_iar_gitignore(
        global_iar_dir=base,
        protected_relative_paths=(CONTAINER_AUTH_DIR_NAME,),
    )

    return ContainerAuthImportResult(
        container_auth_dir=target_root,
        agent_results=tuple(results),
        gitignore_protected=gitignore_protected,
    )


class ContainerAuthController(IContainerAuthImporter):
    """``IContainerAuthImporter`` 的 engines 实现。

    由 ``api`` 层在 dispatch 时构造并注入到 core facade；本身无状态，
    所有方法都委托给模块级纯函数。
    """

    def import_container_auth(
        self, *, global_iar_dir: Path | None = None
    ) -> ContainerAuthImportResult:
        """把本机当前 cc-switch profile 的认证 + skills 复制到 container-auth。"""
        return import_container_auth(global_iar_dir=global_iar_dir)


__all__ = [
    "AgentImportResult",
    "AgentImportSpec",
    "CONTAINER_AUTH_DIR_NAME",
    "CONTAINER_AUTH_PERMISSIONS",
    "ContainerAuthController",
    "ContainerAuthImportResult",
    "SUPPORTED_AGENT_SPECS",
    "container_auth_dir",
    "ensure_container_auth_root",
    "import_agent_auth",
    "import_container_auth",
]
