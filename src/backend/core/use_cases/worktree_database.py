"""为 IAR worktree 准备隔离的关系型数据库。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorktreeDatabaseProvisionRequest:
    """描述一次 Issue worktree 数据库准备请求。"""

    repository_path: Path
    worktree_path: Path
    issue_number: int


def provision_worktree_database(
    request: WorktreeDatabaseProvisionRequest,
    process_runner: IProcessRunner,
) -> None:
    """为 worktree 创建独立数据库并迁移至当前 schema(尽力而为)。

    数据库连接从 worktree 的 ``.env.local`` 中读取。实际的 URL 解析与
    PostgreSQL/MySQL 建库由模板脚本承担;这里仅编排 daemon 生命周期,避免
    core 层依赖具体数据库驱动。

    由于 ``provision_database`` 默认全局开启,而大量仓库并未同步建库脚本、
    或根本不使用关系型数据库,任何一环失败都只记录 warning 并跳过,绝不
    阻断 worktree 创建——worktree 会退回使用其 ``.env.local`` 里原有的
    (通常是共享的)数据库。

    Args:
        request: 仓库、worktree 与 Issue 标识。
        process_runner: 执行建库和迁移命令的端口。
    """
    database_script_path = (
        request.repository_path / "scripts" / "shared" / "template" / "setup_copied_database.py"
    )
    if not database_script_path.is_file():
        _logger.warning(
            "worktree 数据库隔离已启用,但未找到建库脚本 %s;回退到共享数据库。"
            "如需隔离,请从模板仓同步 scripts/shared/template/setup_copied_database.py。",
            database_script_path,
        )
        return

    repository_digest = sha256(str(request.repository_path.resolve()).encode("utf-8")).hexdigest()[
        :8
    ]
    database_identifier = (
        f"{request.repository_path.name}_iar_issue_{request.issue_number}_{repository_digest}"
    )
    setup_result = process_runner.run(
        [
            "uv",
            "run",
            "python",
            str(database_script_path),
            database_identifier,
            str(request.worktree_path),
            "--strict",
        ],
        cwd=request.worktree_path,
        check=False,
    )
    if setup_result.return_code != 0:
        _logger.warning(
            "worktree 建库脚本失败(return_code=%s);回退到共享数据库。stdout=%r, stderr=%r",
            setup_result.return_code,
            setup_result.stdout,
            setup_result.stderr,
        )
        return

    if not (request.worktree_path / "alembic.ini").is_file():
        return
    migration_result = process_runner.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=request.worktree_path,
        check=False,
    )
    if migration_result.return_code != 0:
        _logger.warning(
            "worktree 数据库迁移失败(return_code=%s);worktree 继续,"
            "agent 运行时会再次执行迁移。stdout=%r, stderr=%r",
            migration_result.return_code,
            migration_result.stdout,
            migration_result.stderr,
        )
        return
