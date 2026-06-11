"""仓库 registry（config.toml）的受限写回实现。

使用 ``tomlkit`` 做 round-trip 编辑：保留文件中的全部注释与格式，
只触碰 ``agent_runner.repositories.<repo_id>`` 子树。写入采用
「同目录临时文件 + ``os.replace``」原子替换，避免写坏配置文件。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.items import Table


@dataclass(frozen=True)
class RegistryRepositoryEntry:
    """registry 中一个仓库条目的摘要视图（与 core 同构，鸭子类型）。"""

    repo_id: str
    path: str
    enabled: bool
    display_name: str | None
    path_exists: bool


class TomlRegistryEditor:
    """``IRepositoryRegistryEditor`` 端口的 tomlkit 实现（鸭子类型）。"""

    def __init__(self, config_path: str | Path) -> None:
        """初始化编辑器。

        Args:
            config_path: ``config.toml`` 的路径。
        """
        self._config_path = Path(config_path).expanduser()

    def _read_document(self) -> tomlkit.TOMLDocument:
        raw_text = self._config_path.read_text(encoding="utf-8")
        return tomlkit.parse(raw_text)

    def _write_document(self, document: tomlkit.TOMLDocument) -> None:
        temp_path = self._config_path.with_name(self._config_path.name + ".tmp")
        temp_path.write_text(tomlkit.dumps(document), encoding="utf-8")
        os.replace(temp_path, self._config_path)

    @staticmethod
    def _repositories_table(document: tomlkit.TOMLDocument) -> Table:
        agent_runner_table = document.get("agent_runner")
        if agent_runner_table is None:
            agent_runner_table = tomlkit.table(is_super_table=True)
            document["agent_runner"] = agent_runner_table
        repositories_table = agent_runner_table.get("repositories")
        if repositories_table is None:
            repositories_table = tomlkit.table(is_super_table=True)
            agent_runner_table["repositories"] = repositories_table
        return repositories_table

    def list_repositories(self) -> list[RegistryRepositoryEntry]:
        """列出 registry 中的全部仓库条目。"""
        document = self._read_document()
        agent_runner_table = document.get("agent_runner") or {}
        repositories_table = agent_runner_table.get("repositories") or {}
        registry_entries: list[RegistryRepositoryEntry] = []
        for repo_id, repo_table in repositories_table.items():
            configured_path = str(repo_table.get("path", ""))
            resolved_path = Path(configured_path).expanduser()
            registry_entries.append(
                RegistryRepositoryEntry(
                    repo_id=str(repo_id),
                    path=configured_path,
                    enabled=bool(repo_table.get("enabled", True)),
                    display_name=(
                        str(repo_table["display_name"])
                        if "display_name" in repo_table
                        else None
                    ),
                    path_exists=resolved_path.exists(),
                )
            )
        return registry_entries

    def add_repository(
        self, *, repo_id: str, path: str, display_name: str | None
    ) -> None:
        """新增一个仓库条目（enabled 默认 true）。"""
        document = self._read_document()
        repositories_table = self._repositories_table(document)
        if repo_id in repositories_table:
            raise ValueError(f"Repository '{repo_id}' already exists in the registry.")
        repository_table = tomlkit.table()
        repository_table["path"] = path
        repository_table["enabled"] = True
        if display_name:
            repository_table["display_name"] = display_name
        repositories_table[repo_id] = repository_table
        self._write_document(document)

    def set_enabled(self, repo_id: str, *, enabled: bool) -> None:
        """启用或停用一个已有条目。"""
        document = self._read_document()
        agent_runner_table = document.get("agent_runner") or {}
        repositories_table = agent_runner_table.get("repositories")
        if repositories_table is None or repo_id not in repositories_table:
            raise KeyError(f"Repository '{repo_id}' not found in the registry.")
        repositories_table[repo_id]["enabled"] = enabled
        self._write_document(document)
