"""仓库 registry 管理用例（校验 + 受限写回编排）。

registry（``config.toml`` 的 ``[agent_runner.repositories.*]``）仍是
项目接入的唯一事实来源；本用例只是它的受控编辑器：

- ``repo_id`` 必须满足 ``^[a-z0-9][a-z0-9-]*$``。
- 路径必须存在且是 git 仓库（含 ``.git`` 目录或文件，worktree 亦可）。
- 新增条目默认 enabled；启停只翻转 ``enabled`` 字段。
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.core.shared.interfaces.runner_console import (
    IRepositoryRegistryEditor,
    RegistryRepositoryEntry,
)

_REPO_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class RegistryValidationError(ValueError):
    """registry 操作的校验失败。"""


def list_registry_repositories(
    editor: IRepositoryRegistryEditor,
) -> list[RegistryRepositoryEntry]:
    """列出 registry 的全部条目。"""
    return editor.list_repositories()


def add_registry_repository(
    *,
    editor: IRepositoryRegistryEditor,
    repo_id: str,
    path: str,
    display_name: str | None,
) -> RegistryRepositoryEntry:
    """校验并新增一个 registry 条目。

    Args:
        editor: registry 写回端口。
        repo_id: 新条目 ID。
        path: 仓库本地路径。
        display_name: 可选显示名。

    Returns:
        RegistryRepositoryEntry: 新增后的条目视图。

    Raises:
        RegistryValidationError: repo_id 非法、路径不存在、不是 git
            仓库或 ID 已存在。
    """
    if not _REPO_ID_PATTERN.match(repo_id):
        raise RegistryValidationError(
            "repo_id must match ^[a-z0-9][a-z0-9-]*$ " f"(got '{repo_id}')."
        )
    resolved_path = Path(path).expanduser().resolve()
    if not resolved_path.exists():
        raise RegistryValidationError(f"Path '{resolved_path}' does not exist.")
    if not (resolved_path / ".git").exists():
        raise RegistryValidationError(
            f"Path '{resolved_path}' is not a git repository (no .git entry)."
        )
    existing_ids = {entry.repo_id for entry in editor.list_repositories()}
    if repo_id in existing_ids:
        raise RegistryValidationError(f"Repository '{repo_id}' already exists in the registry.")
    try:
        editor.add_repository(repo_id=repo_id, path=str(resolved_path), display_name=display_name)
    except ValueError as exc:
        raise RegistryValidationError(str(exc)) from exc
    return RegistryRepositoryEntry(
        repo_id=repo_id,
        path=str(resolved_path),
        enabled=True,
        display_name=display_name,
        path_exists=True,
    )


def set_registry_repository_enabled(
    *,
    editor: IRepositoryRegistryEditor,
    repo_id: str,
    enabled: bool,
) -> None:
    """启用或停用一个 registry 条目。

    Raises:
        RegistryValidationError: repo_id 不存在。
    """
    try:
        editor.set_enabled(repo_id, enabled=enabled)
    except KeyError as exc:
        raise RegistryValidationError(str(exc)) from exc
