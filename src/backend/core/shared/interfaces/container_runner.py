"""Container runner 抽象端口（ports）。

按照四层依赖方向（``api -> core -> engines -> infrastructure``），``core`` 层
禁止直接 import ``engines`` 或 ``infrastructure``。本模块声明 ``iar container``
相关用例所需的端口契约，具体实现由 ``engines.agent_runner.container_auth`` /
``container_ops`` 提供，并由 ``api`` 层在 dispatch 时注入。

模块内包含三个端口：

- ``IContainerAuthImporter``：把本机 agent CLI 认证快照到 ``~/.iar/container-auth/``。
- ``IContainerRunnerController``：封装 ``docker compose`` 子进程调用（up / down / logs）。
- ``IRunnerAssetsLocator``：从 keda package data 定位 runner 容器资产路径。

这种「依赖倒置 + 端口隔离」的设计，使 core 用例可以在不感知具体实现的前提下
被单元测试（用假实现替换）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ContainerAuthImportResult:
    """容器认证导入的聚合结果（core 侧只关心路径与逐 agent 成功/跳过状态）。

    Attributes:
        container_auth_dir: 实际写入的 ``~/.iar/container-auth/`` 绝对路径。
        agent_results: 每个 agent 的子结果（agent_name / skipped / copied_entries）。
        gitignore_protected: 是否成功把 ``container-auth`` 加入 gitignore。
    """

    container_auth_dir: Path
    agent_results: tuple["ContainerAgentImportResult", ...] = field(default_factory=tuple)
    gitignore_protected: bool = False


@dataclass(frozen=True)
class ContainerAgentImportResult:
    """单个 agent 的导入结果。"""

    agent_name: str
    source_dir: Path
    target_dir: Path
    copied_entries: tuple[str, ...] = field(default_factory=tuple)
    skipped: bool = False
    skip_reason: str | None = None


@dataclass(frozen=True)
class RunnerContainerAssets:
    """已定位的 runner 容器资产路径集合。"""

    template_dir: Path
    compose_file: Path
    env_example: Path


@dataclass(frozen=True)
class ContainerCommandPlan:
    """``dry_run=True`` 时返回的命令计划。"""

    argv: tuple[str, ...]
    env: dict[str, str]
    compose_file: Path


@dataclass(frozen=True)
class ContainerUpRequest:
    """``iar container up`` 的请求参数（core 侧用，无 engines 实现细节）。

    Attributes:
        repo_path: 挂载进容器的目标仓库绝对路径。
        gh_token: GitHub Token（容器内 gh 用此认证）。空串视为未设置。
        repo_id: 仓库 registry id，传给容器方便 iar daemon 锁定目标。
        uid: 容器内进程 UID；``None`` 时使用 ``os.getuid()``。
        gid: 容器内进程 GID；``None`` 时使用 ``os.getgid()``。
        extra_env: 其它要注入 compose 的环境变量。
        dry_run: True 时不真调 docker compose，仅打印计划。
        build: True 时 ``docker compose up --build``。
    """

    repo_path: Path
    gh_token: str = ""
    repo_id: str = ""
    uid: int | None = None
    gid: int | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False
    build: bool = False


# docker compose 子进程运行协议；与 engines 实现的 _DockerRunner 等价。
DockerRunnerCallable = Callable[..., object]


class IContainerAuthImporter(ABC):
    """容器认证导入端口。"""

    @abstractmethod
    def import_container_auth(
        self, *, global_iar_dir: Path | None = None
    ) -> ContainerAuthImportResult:
        """把本机当前 cc-switch profile 的认证 + skills 复制到 container-auth。"""


class IContainerRunnerController(ABC):
    """容器生命周期管理端口。"""

    @abstractmethod
    def resolve_packaged_runner_assets(self) -> RunnerContainerAssets:
        """从 keda package data 定位 runner 容器资产路径。"""

    @abstractmethod
    def run_container_up(
        self,
        compose_file: Path,
        request: ContainerUpRequest,
        *,
        runner: DockerRunnerCallable | None = None,
    ) -> ContainerCommandPlan:
        """执行 ``docker compose up``；``dry_run=True`` 时不真调 docker。"""

    @abstractmethod
    def run_container_down(
        self, compose_file: Path, *, runner: DockerRunnerCallable | None = None
    ) -> list[str]:
        """执行 ``docker compose down``。"""

    @abstractmethod
    def run_container_logs(
        self,
        compose_file: Path,
        *,
        follow: bool = True,
        runner: DockerRunnerCallable | None = None,
    ) -> list[str]:
        """执行 ``docker compose logs``（默认 streaming）。"""


__all__ = [
    "ContainerAgentImportResult",
    "ContainerAuthImportResult",
    "ContainerCommandPlan",
    "ContainerUpRequest",
    "DockerRunnerCallable",
    "IContainerAuthImporter",
    "IContainerRunnerController",
    "RunnerContainerAssets",
]
