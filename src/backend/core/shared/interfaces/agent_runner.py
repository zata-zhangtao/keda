"""Agent Runner 抽象接口（端口 / ports）。

本模块定义 core 层与外部世界交互所需的一组抽象端口（Port）。
按照项目四层依赖方向（api -> core -> engines -> infrastructure），
``core`` 层只允许依赖自身以及 ``core/shared/interfaces/``，禁止直接
导入 ``engines`` 或 ``infrastructure``。因此这里只声明「能力契约」，
具体实现（如真正调用子进程、访问 GitHub CLI）由 ``engines`` /
``infrastructure`` 层提供，并在运行时通过工厂注入。

模块内包含四个端口：

- ``IProcessRunner``：执行任意外部命令的底层能力。
- ``IAgentTranscriptRunner``：运行 AI Agent 并流式产出审议事件。
- ``IContentGenerator``：以只读方式运行 Agent 生成 Markdown 文本。
- ``IGitHubClient``：封装与 GitHub 仓库/Issue/PR 的交互。

这种「依赖倒置 + 端口隔离」的设计，使 core 层的用例（use cases）
可以在不感知具体实现的前提下被单元测试（用假实现替换），同时也
便于替换底层工具（例如把 GitHub CLI 换成 REST API）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Sequence

from backend.core.shared.models.agent_runner import (
    CommandResult,
    IssueSummary,
    LabelConfig,
    PullRequestContext,
)
from backend.core.shared.models.agent_deliberation import (
    DeliberationEvent,
)


class IProcessRunner(ABC):
    """运行外部命令的端口。

    这是最底层的命令执行抽象，封装「启动一个子进程、等待结束、
    拿到退出码与输出」这一过程。上层用例不直接调用
    ``subprocess``，而是依赖本端口，从而把「如何执行命令」的细节
    （超时、编码、是否捕获输出等）下沉到 ``infrastructure`` 层实现。

    在测试中，可用一个返回预设 ``CommandResult`` 的假实现替换它，
    从而避免真正启动子进程。
    """

    @abstractmethod
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout: int | None = None,
        capture_output: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        """运行一条命令并捕获其结果。

        命令以参数序列（而非单个 shell 字符串）传入，可避免 shell
        注入并保证跨平台行为一致。

        Args:
            command: 要执行的命令及其参数，例如
                ``["git", "status", "--short"]``。第一个元素是可执行
                文件名，其余为参数；不会经过 shell 解析。
            cwd: 命令的工作目录。调用方必须显式指定，以避免依赖
                进程当前目录而产生路径漂移。
            check: 当子进程退出码非零时是否抛出异常。为 ``True`` 时，
                非零退出会触发错误向上抛出；为 ``False`` 时则把非零
                退出码原样放入返回的 ``CommandResult``，交由调用方
                自行判断。
            timeout: 可选的超时时间（秒）。超过该时间后子进程会被
                终止并视为失败；为 ``None`` 表示不设超时、一直等待。
            capture_output: 是否捕获 stdout/stderr。为 ``True`` 时输出
                被收集到返回值中；为 ``False`` 时输出直接透传到当前
                终端（用于需要实时可见的交互场景），此时返回的
                ``CommandResult`` 中的输出字段为空字符串。
            input_text: 可选的标准输入文本。提供时会原样写入子进程的
                stdin（如 ``git mktree`` 这类从标准输入读取的 plumbing
                命令），并强制以捕获模式运行；为 ``None`` 时不写 stdin。

        Returns:
            CommandResult: 包含退出码与（按需）捕获到的 stdout/stderr
            的命令结果对象。

        Raises:
            Exception: 当 ``check`` 为 ``True`` 且命令以非零码退出，
                或在 ``timeout`` 内未完成时，向上抛出相应异常。
        """
        ...


class IAgentTranscriptRunner(ABC):
    """运行 AI Agent 并产出结构化的审议（deliberation）事件。

    与 ``IProcessRunner`` 不同，本端口面向「长时间运行、会持续输出」
    的 AI Agent 进程。它在运行过程中把 Agent 的输出解析为一系列
    结构化的 ``DeliberationEvent``，并通过回调实时推送给调用方，
    从而支持「边运行边展示 / 边落盘」的流式体验。

    通过把「事件、可读输出、临时进度」拆分到三个独立回调，调用方
    可以分别决定：哪些内容计入正式 transcript、哪些只用于即时展示。
    """

    @abstractmethod
    def run(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        event_sink: Callable[[DeliberationEvent], None],
        output_sink: Callable[[str], None] | None = None,
        display_sink: Callable[[str], None] | None = None,
    ) -> CommandResult:
        """运行一个 Agent 并在过程中产出事件。

        本方法会阻塞直到 Agent 进程结束；在此期间，随着输出到达，
        会按顺序调用各回调（sink）。回调采用「推送」模式，调用方
        无需轮询。

        Args:
            agent_name: 要运行的 Agent 名称，取值如 ``claude``、
                ``kimi``、``codex``；用于选择对应的命令行调用方式。
            prompt: 传给 Agent 的完整提示词文本。
            cwd: Agent 进程的工作目录，通常是目标仓库或工作树根目录。
            event_sink: 针对每个解析出的结构化事件调用的回调。这是
                必填项，是审议流程消费 Agent 输出的主要通道。
            output_sink: 可选回调，用于接收「可读的、已渲染的文本块」。
                提供时，每一块渲染输出会在到达时即时传入，便于实时
                流式展示并追加写入工作区文件。这部分内容会计入正式
                transcript。
            display_sink: 可选回调，用于接收「临时性的进度文本」，
                例如 Agent 在 stderr 上输出的推理过程 / 工具调用日志。
                与 ``output_sink`` 的关键区别在于：它仅供即时展示，
                文本块会实时显示，但**不会**被收集进 transcript，也
                **不会**写入工作区文件。

        Returns:
            CommandResult: 包含退出码与已捕获输出的命令结果。即便已
            通过回调流式推送，最终结果仍会汇总返回，便于调用方做
            整体成败判断。
        """
        ...


class IContentGenerator(ABC):
    """通过本地只读 Agent 生成人类可读的 Markdown 内容。

    适用于「让 Agent 一次性产出一段文本」的场景（如生成 Issue 正文、
    PR 描述、总结报告），不需要流式事件，也不应对仓库产生副作用，
    因此约定以**只读**模式运行 Agent。与
    ``IAgentTranscriptRunner`` 相比，本端口更简单：一次调用、一段
    输出、不产出结构化事件。
    """

    @abstractmethod
    def generate(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        timeout: int | None = None,
    ) -> CommandResult:
        """运行一个只读的内容生成器并返回其输出。

        Args:
            agent_name: 要运行的 Agent 名称（``claude``、``kimi``、
                ``codex``）。
            prompt: 传给 Agent 的完整提示词文本。
            cwd: Agent 进程的工作目录。即便是只读运行，仍需提供工作
                目录以便 Agent 读取仓库上下文。
            timeout: 可选的超时时间（秒）。为 ``None`` 表示不设超时。

        Returns:
            CommandResult: 包含已捕获输出的命令结果；其中 stdout 即为
            生成的 Markdown 文本。
        """
        ...


class IGitHubClient(ABC):
    """与 GitHub 交互的端口。

    把仓库自动化所需的全部 GitHub 操作（标签管理、Issue 增删评论、
    PR 创建与查询等）收敛到一个契约下。core 层用例通过它驱动 GitHub
    工作流，而具体实现（通常基于 GitHub CLI ``gh`` 或 REST API）位于
    下层，可被独立替换与测试。
    """

    @abstractmethod
    def sync_labels(self, labels: LabelConfig) -> None:
        """创建或更新标准标签。

        以 ``labels`` 配置为准，在仓库中建立缺失的标签并对已存在的
        标签做幂等更新，保证标签集合与约定一致。
        """
        ...

    @abstractmethod
    def list_ready_issues(self, ready_label: str, limit: int) -> list[IssueSummary]:
        """列出带有 ready 标签的开放 Issue。

        Args:
            ready_label: 表示「就绪可处理」的标签名。
            limit: 返回结果的最大数量上限。

        Returns:
            list[IssueSummary]: 满足条件的开放 Issue 摘要列表。
        """
        ...

    @abstractmethod
    def edit_issue_labels(
        self,
        issue_number: int,
        *,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
    ) -> None:
        """为某个 Issue 添加和移除标签。

        Args:
            issue_number: 目标 Issue 编号。
            add: 需要添加的标签名序列；默认空，表示不添加。
            remove: 需要移除的标签名序列；默认空，表示不移除。
        """
        ...

    @abstractmethod
    def comment_issue(self, issue_number: int, body: str) -> None:
        """向某个 Issue 发布一条 Markdown 评论。

        Args:
            issue_number: 目标 Issue 编号。
            body: 评论正文，支持 Markdown。
        """
        ...

    @abstractmethod
    def create_issue(
        self,
        *,
        title: str,
        body: str,
        labels: Sequence[str],
    ) -> str:
        """创建一个 GitHub Issue 并返回其 URL。

        Args:
            title: Issue 标题。
            body: Issue 正文，支持 Markdown。
            labels: 创建时附加的标签名序列。

        Returns:
            str: 新建 Issue 的网页 URL。
        """
        ...

    @abstractmethod
    def create_draft_pr(
        self,
        *,
        title: str,
        body: str,
        base_branch: str,
        cwd: Path,
    ) -> str:
        """基于当前分支创建一个草稿（draft）Pull Request。

        Args:
            title: PR 标题。
            body: PR 描述，支持 Markdown。
            base_branch: 合并目标（基线）分支名。
            cwd: 工作目录，用于确定 PR 的来源（head）分支所在仓库。

        Returns:
            str: 新建 PR 的网页 URL。
        """
        ...

    @abstractmethod
    def list_review_candidate_issues(
        self, labels: Sequence[str], limit: int
    ) -> list[IssueSummary]:
        """列出带有给定任一标签的开放 Issue。

        与 ``list_ready_issues`` 的区别在于支持多个候选标签：只要
        Issue 命中其中任意一个标签即视为候选。

        Args:
            labels: 候选标签名序列，命中任一即纳入结果。
            limit: 返回结果的最大数量上限。

        Returns:
            list[IssueSummary]: 满足条件的开放 Issue 摘要列表。
        """
        ...

    @abstractmethod
    def get_pull_request_context(self, branch: str) -> PullRequestContext | None:
        """返回给定分支上开放 PR 的上下文。

        Args:
            branch: 作为来源（head）的分支名。

        Returns:
            PullRequestContext | None: 若该分支存在开放 PR，返回其
            上下文；否则返回 ``None``。
        """
        ...

    @abstractmethod
    def list_issue_comments(self, issue_number: int) -> list[str]:
        """返回某个 Issue 的原始评论正文列表。

        Args:
            issue_number: 目标 Issue 编号。

        Returns:
            list[str]: 按时间顺序排列的评论正文（原始文本）列表。
        """
        ...

    @abstractmethod
    def comment_pr(self, pr_number: int, body: str) -> None:
        """向某个 Pull Request 发布一条 Markdown 评论。

        Args:
            pr_number: 目标 PR 编号。
            body: 评论正文，支持 Markdown。
        """
        ...

    @abstractmethod
    def update_pull_request_body(self, pr_number: int, body: str) -> None:
        """整体替换某个 Pull Request 的描述正文。

        用于验证门禁在 PR head 漂移后重置 body 中的人工勾选清单。

        Args:
            pr_number: 目标 PR 编号。
            body: 新的 PR 描述正文，支持 Markdown。
        """
        ...

    @abstractmethod
    def list_pr_comments(self, pr_number: int) -> list[str]:
        """返回某个 PR 的原始评论正文列表。

        Args:
            pr_number: 目标 PR 编号。

        Returns:
            list[str]: 按时间顺序排列的评论正文（原始文本）列表。
        """
        ...

    @abstractmethod
    def find_open_pr_by_head(self, branch: str) -> str | None:
        """若该分支存在开放的 PR，则返回其 URL。

        Args:
            branch: 作为来源（head）的分支名。

        Returns:
            str | None: 命中的开放 PR 的 URL；若不存在则返回 ``None``。
        """
        ...

    @abstractmethod
    def get_remote_base_sha(self, remote: str, base_branch: str) -> str:
        """返回远端基线分支（base branch）的 SHA。

        Args:
            remote: 远端名称，例如 ``origin``。
            base_branch: 基线分支名。

        Returns:
            str: 该远端基线分支当前指向的提交 SHA。
        """
        ...

    @abstractmethod
    def get_issue(self, issue_number: int) -> IssueSummary:
        """返回指定 Issue 编号的摘要信息。

        Args:
            issue_number: 目标 Issue 编号。

        Returns:
            IssueSummary: Issue 摘要信息。
        """
        ...

    @abstractmethod
    def list_issues_by_label(
        self, label: str, limit: int, state: str = "all"
    ) -> list[IssueSummary]:
        """按标签列出 Issue，可跨 open/closed 状态。

        Args:
            label: 要筛选的标签名。
            limit: 返回结果的最大数量上限。
            state: Issue 状态筛选，``"open"``、``"closed"`` 或 ``"all"``。

        Returns:
            list[IssueSummary]: 满足条件的 Issue 摘要列表。
        """
        ...

    @abstractmethod
    def ensure_label(self, name: str) -> None:
        """确保仓库中存在指定标签，不存在则创建。

        Args:
            name: 标签名称。
        """
        ...
