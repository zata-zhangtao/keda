"""为 IAR worktree 准备隔离的关系型数据库。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner


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
    """为 worktree 创建独立数据库并迁移至当前 schema。

    数据库连接从 worktree 的 ``.env.local`` 中读取。实际的 URL 解析与
    PostgreSQL/MySQL 建库由模板脚本承担；这里仅编排 daemon 生命周期，避免
    core 层依赖具体数据库驱动。

    Args:
        request: 仓库、worktree 与 Issue 标识。
        process_runner: 执行建库和迁移命令的端口。

    Raises:
        FileNotFoundError: 当前仓库没有数据库准备脚本时抛出。
        RuntimeError: 建库或迁移命令失败时抛出。
    """
    database_script_path = (
        request.repository_path / "scripts" / "shared" / "template" / "setup_copied_database.py"
    )
    if not database_script_path.is_file():
        raise FileNotFoundError(f"worktree database setup script not found: {database_script_path}")

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
        raise RuntimeError(
            "worktree database setup failed: "
            f"stdout={setup_result.stdout!r}, stderr={setup_result.stderr!r}"
        )

    if not (request.worktree_path / "alembic.ini").is_file():
        return
    migration_result = process_runner.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=request.worktree_path,
        check=False,
    )
    if migration_result.return_code != 0:
        raise RuntimeError(
            "worktree database migration failed: "
            f"stdout={migration_result.stdout!r}, stderr={migration_result.stderr!r}"
        )
