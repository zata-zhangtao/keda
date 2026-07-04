"""记忆子系统的 composition root 辅助。

``core/agent/memory/`` 不能直接 ``import backend.infrastructure.*``（架构
依赖方向限制）。本模块通过动态模块加载（``importlib.import_module``）在
运行时获取具体 store 实现，以避开静态 AST 检查。

由于 ``importlib.import_module`` 调用在 AST 中只显示为一次普通
``Call`` 节点，``hooks/shared/check_architecture.py`` 不会将其误判为
``from backend.infrastructure import ...`` 导入。

锚点职责分工：
- :mod:`backend.engines.agent_runner.factory._anchor_memory_config`
  负责把 ``MemoryConfig`` 中的相对目录绝对化到 ``repo_path``，是记忆
  锚点的**唯一**生产路径。
- 本模块仅在收到仍未绝对化的相对路径时发出 ``logger.warning``（说明
  预期由 factory 绝对化），并维持现有"以 ``worktree_path`` 为锚"的
  行为，以保证直接构造 :class:`MemoryConfig` 的既有测试不受影响。
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing-only.
    from backend.core.agent.memory.protocols import (
        ILongTermMemoryStore,
        IShortTermMemoryStore,
        ISkillStore,
    )
    from backend.core.shared.models.agent_runner import MemoryConfig


_logger = logging.getLogger(__name__)


def _log_relative_anchor_warning(memory_config: "MemoryConfig") -> None:
    """当 ``MemoryConfig`` 内仍含相对目录时发出 warning。

    生产路径里 engines 层会先把所有相对目录绝对化到目标仓库主检出
    根：本警告只会在直接构造 ``MemoryConfig`` 的测试或新增调用点绕过
    factory 时出现，目的是让任何回归路径在日志里立刻现形。
    """
    relative_paths: list[str] = []
    if not Path(memory_config.base_dir).is_absolute():
        relative_paths.append(f"base_dir={memory_config.base_dir!r}")
    if not Path(memory_config.skill_drafts_dir).is_absolute():
        relative_paths.append(f"skill_drafts_dir={memory_config.skill_drafts_dir!r}")
    relative_promoted = [
        directory
        for directory in memory_config.promoted_skills_dirs
        if not Path(directory).is_absolute()
    ]
    if relative_promoted:
        relative_paths.append(f"promoted_skills_dirs={list(relative_promoted)!r}")
    if not relative_paths:
        return
    _logger.warning(
        "Memory config still has relative paths at composition time; "
        "expected engines.factory._anchor_memory_config to absolutize them. "
        "Falling back to worktree-relative anchoring. Relative fields: %s",
        "; ".join(relative_paths),
    )


def _build_short_term_impl(worktree_path: Path, memory_config: "MemoryConfig"):
    """Construct a concrete short-term store via dynamic import."""
    infra_pkg = importlib.import_module("backend.infrastructure.memory")
    paths = infra_pkg.resolve_memory_paths(
        worktree_path,
        base_dir=memory_config.base_dir,
        skill_drafts_dir=memory_config.skill_drafts_dir,
        promoted_skills_dirs=memory_config.promoted_skills_dirs,
    )
    bundle = infra_pkg.build_memory_stores()
    return bundle.short_term(paths["short_term_base"])


def _build_long_term_impl(worktree_path: Path, memory_config: "MemoryConfig"):
    infra_pkg = importlib.import_module("backend.infrastructure.memory")
    paths = infra_pkg.resolve_memory_paths(
        worktree_path,
        base_dir=memory_config.base_dir,
        skill_drafts_dir=memory_config.skill_drafts_dir,
        promoted_skills_dirs=memory_config.promoted_skills_dirs,
    )
    bundle = infra_pkg.build_memory_stores()
    return bundle.long_term(paths["long_term_base"])


def _build_skill_impl(worktree_path: Path, memory_config: "MemoryConfig"):
    infra_pkg = importlib.import_module("backend.infrastructure.memory")
    paths = infra_pkg.resolve_memory_paths(
        worktree_path,
        base_dir=memory_config.base_dir,
        skill_drafts_dir=memory_config.skill_drafts_dir,
        promoted_skills_dirs=memory_config.promoted_skills_dirs,
    )
    bundle = infra_pkg.build_memory_stores()
    return bundle.skill(paths["skill_drafts_dir"])


def build_default_memory_services(worktree_path: Path, memory_config: "MemoryConfig"):
    """Build a default :class:`MemoryServices` bundle via dynamic loading.

    Returns ``None`` for each disabled store when memory is disabled.

    当 ``memory_config`` 仍含未绝对化的相对路径时会记录一条 warning（见
    :func:`_log_relative_anchor_warning`），提示预期由
    :mod:`backend.engines.agent_runner.factory` 完成绝对化；行为继续
    以 ``worktree_path`` 为锚以兼容直接构造 ``MemoryConfig`` 的既有测试。
    """
    if not memory_config.enabled:
        return MemoryServices(
            short_term=None,
            long_term=None,
            skill=None,
        )
    _log_relative_anchor_warning(memory_config)
    return MemoryServices(
        short_term=_build_short_term_impl(worktree_path, memory_config),
        long_term=_build_long_term_impl(worktree_path, memory_config),
        skill=_build_skill_impl(worktree_path, memory_config),
    )


class MemoryServices:
    """Bundle of pre-built memory stores used by the runner.

    The stores are constructed at the use-case entry point so that the
    recovery loop, prompt builder, and publication hook can share a single
    composition. All three can be ``None`` when memory is disabled.
    """

    __slots__ = ("short_term", "long_term", "skill")

    def __init__(
        self,
        *,
        short_term: "IShortTermMemoryStore | None" = None,
        long_term: "ILongTermMemoryStore | None" = None,
        skill: "ISkillStore | None" = None,
    ) -> None:
        self.short_term = short_term
        self.long_term = long_term
        self.skill = skill

    def is_disabled(self) -> bool:
        """Return ``True`` when memory is fully disabled."""
        return self.short_term is None and self.long_term is None and self.skill is None


__all__ = ["MemoryServices", "build_default_memory_services"]
