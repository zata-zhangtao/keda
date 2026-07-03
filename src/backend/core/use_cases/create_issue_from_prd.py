"""根据本地 PRD Markdown 文件创建 GitHub Issue。

本模块实现 ``iar issue create`` 工作流：

1. 读取本地 PRD Markdown 文件。
2. 提取元数据（标题、验收清单、引言）。
3. 可选：通过 AI 生成更丰富的 Issue 内容（agent/template 模式）。
4. 通过 ``IGitHubClient`` 创建 GitHub Issue。
5. 将创建的 Issue URL 回写到 PRD 中。
6. 可选：发布 PRD 文件（stage、commit、push），使 Issue 链接持久化到仓库。

内容生成遵循三级级联策略：

- **Agent 模式**（当 ``generated_content`` 启用且 ``mode="agent"`` 时）：
  AI agent 根据完整 PRD 上下文生成 Issue 标题和正文。
- **Template 模式**（当 agent 失败且 ``fallback="template"``，或
  直接设置 ``mode="template"`` 时）：
  使用类似 Jinja2 的 ``.format()`` 模板，通过 PRD 上下文变量
  （如 ``{prd_introduction}``、``{relative_prd_path}`` 等）渲染标题/正文。
- **Hard fallback**（始终可用）：``build_issue_body()`` 构建一个确定的
  Markdown 正文，包含 PRD 路径锚点、验收清单以及（自本次修复后）PRD 引言章节。

所有文件系统 I/O 均显式指定 ``encoding="utf-8"``。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import (
    GeneratedContentConfig,
    LabelConfig,
)
from backend.core.use_cases.agent_runner_dependencies import (
    format_dependency_marker,
    parse_dependency_marker,
    parse_delivery_dependencies,
)
from backend.core.use_cases.agent_runner_validation import (
    build_issue_validation_section,
    extract_evidence_format_waiver_reason,
    extract_realistic_validation_items,
    extract_validation_waiver_reason,
)
from backend.core.use_cases.generated_content import (
    build_issue_context,
    extract_first_h2_section,
    extract_prd_section,
    generate_issue_content,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 正则表达式：PRD 元数据解析
# ---------------------------------------------------------------------------

# 定位 "- GitHub Issue:" 元数据行（不校验值），用于创建 Issue 后替换该行。
# 值可能是真实 Issue URL，也可能是 "(待创建)"、"(to be created)" 等占位符。
ISSUE_LINE_RE = re.compile(r"^- GitHub Issue:")

# 仅匹配值为 URL 的链接行，用于判断 PRD 是否已关联真实 Issue；
# 占位符不视为已关联，创建 Issue 时会被真实链接替换。
ISSUE_LINK_LINE_RE = re.compile(r"^- GitHub Issue:\s*https?://")

# 从 GitHub Issue URL（如 ``https://github.com/org/repo/issues/42``）
# 中提取数字 Issue 编号。
ISSUE_NUMBER_RE = re.compile(r"/issues/(?P<issue_number>\d+)(?:\D*$|$)")


# ---------------------------------------------------------------------------
# 数据传输对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueFromPrdRequest:
    """从 PRD 创建 GitHub Issue 的输入参数。

    Attributes:
        repo_path: 仓库根目录的绝对路径。
        prd_path: PRD 文件路径。可以是绝对路径，也可以相对于 ``repo_path``。
        issue_type: 用于构建初始标签的类别，例如 ``"feature"`` → ``"type/feature"``。
        title_override: 如果提供，将同时覆盖 AI 生成的标题和 PRD 派生的 fallback 标题。
        queue_ready: 是否在 Issue 创建后立即添加 ``agent/ready`` 标签
            （当 ``publish_prd=True`` 时则在发布后添加）。
        issue_agent: 要附加的 agent 路由标签。必须是 ``LabelConfig.agent_labels``
            中的键（如 ``"claude"``、``"kimi"``），或 ``"auto"`` / ``"none"``。
        labels_config: 显式指定的标签名称。为 ``None`` 时使用默认值。
        force: 为 ``True`` 时替换 PRD 中已有的 ``- GitHub Issue:`` 行，
            而不是抛出 ``ValueError``。
        publish_prd: 为 ``True`` 时在 Issue 创建后对 PRD 文件执行 stage、commit、push。
        git_remote: 要 push 到的 Git remote 名称（默认 ``"origin"``）。
        git_base_branch: 基础分支名称。当 ``queue_ready=True`` 且 ``publish_prd=True`` 时必需。
        generated_content_config: 可选的 AI 内容生成配置。
            为 ``None`` 或 ``enabled=False`` 时使用确定性 fallback 正文。
        depends_on: 显式指定的上游 Issue 编号列表（与 PRD 声明合并去重）。
        depends_on_group: 显式指定的上游 group 列表（与 PRD 声明合并去重）。
        parse_evidence_format_with_agent: 是否用 agent 解析 PRD 中的格式要求。
        validation_language: Realistic Validation 固定标签语言，如 ``zh-CN``。
        structured_evidence: 是否为该 Issue 物化 ``iar:structured-evidence`` marker。
    """

    repo_path: Path
    prd_path: Path
    issue_type: str
    title_override: str | None = None
    queue_ready: bool = False
    issue_agent: str = "auto"
    labels_config: LabelConfig | None = None
    force: bool = False
    publish_prd: bool = False
    git_remote: str = "origin"
    git_base_branch: str = "main"
    generated_content_config: GeneratedContentConfig | None = None
    depends_on: tuple[int, ...] = ()
    depends_on_group: tuple[str, ...] = ()
    parse_evidence_format_with_agent: bool = True
    validation_language: str = "zh-CN"
    structured_evidence: bool = True


@dataclass(frozen=True)
class PrdPublishContext:
    """单个目标 PRD 文件的 Git 发布上下文。

    Attributes:
        repo_path: 仓库的绝对路径。
        relative_prd_path: 相对于 ``repo_path`` 的 PRD 路径。
        git_remote: 要 push 到的 remote 名称。
        current_branch: 当前检出的分支（用作 push 目标）。
    """

    repo_path: Path
    relative_prd_path: Path
    git_remote: str
    current_branch: str


# ---------------------------------------------------------------------------
# PRD 元数据提取辅助函数
# ---------------------------------------------------------------------------


def extract_title(prd_text: str, fallback_title: str) -> str:
    """从 PRD 文档中提取人类可读的标题。

    逐行扫描 PRD，查找第一个 Markdown H1 标题（``# ...``）。
    如果存在 ``PRD:`` / ``PRD：`` 前缀则将其去除。
    跳过 Part A / Part B 这类章节标题，避免把结构标题误当成 feature 标题。
    未找到 H1 时回退到 ``fallback_title``。

    Args:
        prd_text: PRD 文件的完整文本。
        fallback_title: 未找到 H1 标题时返回的标题。

    Returns:
        提取的标题或 ``fallback_title``。
    """

    for line in prd_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = re.sub(r"^PRD[:：]\s*", "", stripped[2:]).strip()
            if title and not re.match(r"^Part\s+[AB]\b", title, re.IGNORECASE):
                return title or fallback_title
    return fallback_title


def extract_acceptance_items(prd_text: str) -> list[str]:
    """从 PRD 中提取验收清单条目。

    搜索 H2 标题中包含 ``"acceptance"`` 或 ``"验收"``（不区分大小写）的章节。
    在该章节内，收集所有以 ``"- ["`` 开头的 Markdown 复选框行。
    已有的勾选状态（``[x]``、``[X]``）会被规范化为未勾选 ``[ ]``，
    因为 Issue 代表的是*待完成*的工作。

    如果未找到验收章节或没有清单条目，则返回默认的三项清单，
    确保 Issue 不会在没有可执行项的情况下被创建。

    Args:
        prd_text: PRD 文件的完整文本。

    Returns:
        Markdown 复选框字符串列表。
    """

    items: list[str] = []
    in_acceptance = False
    for line in prd_text.splitlines():
        stripped = line.strip()
        # 根据 H2 标题进入/退出验收章节。
        if stripped.startswith("## "):
            in_acceptance = bool(re.search(r"acceptance|验收", stripped, re.IGNORECASE))
            continue
        if in_acceptance and stripped.startswith("- ["):
            items.append(re.sub(r"^- \[[ xX]\]", "- [ ]", stripped))
    return items or [
        "- [ ] Review the canonical PRD acceptance checklist",
        "- [ ] Implement the linked task",
        "- [ ] Run the required verification",
    ]


# ---------------------------------------------------------------------------
# Issue 正文构建
# ---------------------------------------------------------------------------


def build_issue_body(
    *,
    relative_prd_path: Path,
    title: str,
    acceptance_items: list[str],
    prd_text: str,
    dependency_marker: str = "",
) -> str:
    """基于 PRD 元数据构建确定的 Issue 正文。

    这是 AI 内容生成被禁用或失败时的 *hard fallback*。正文包含：

    1. 带有 Issue 标题的摘要行。
    2. PRD 引言章节（如果存在），使读者无需打开 PRD 文件即可理解需求。
    3. 机器可读的 ``- PRD path: ...`` 锚点，runner 依赖它定位规范 PRD。
    4. 从 PRD 复制的验收清单。
    5. 交付说明（分支命名、worktree 命令、PR 规范）。
    6. 可选的 ``iar:depends-on`` hidden marker（当依赖门禁启用时）。

    Args:
        relative_prd_path: 相对于仓库根目录的 PRD 路径。
        title: Issue 标题（用于摘要行）。
        acceptance_items: 从 PRD 提取的清单条目。
        prd_text: 完整 PRD 文本，用于提取引言章节。
        dependency_marker: 物化的 ``iar:depends-on`` marker 字符串，为空时不写入。

    Returns:
        完整的 Issue 正文 Markdown 字符串。
    """

    # 从 PRD 中提取引言/目标章节，使 Issue 正文自成一体。
    # 使用与 AI 上下文构建器相同的关键词列表，确保 template、agent、
    # fallback 三条路径保持一致。若未匹配到关键词，则兜底取第一个 ## 标题
    # 下的全部内容，避免中文 PRD（如“背景与目标”）出现空引言。
    introduction = extract_prd_section(prd_text, ("introduction", "intro", "引言", "概述"))
    if not introduction:
        introduction = extract_first_h2_section(prd_text)
    body_parts: list[str] = [
        "## Summary",
        "",
        f"Tracked implementation task for `{title}`.",
    ]
    if introduction:
        body_parts.extend(["", introduction])
    body_parts.extend(
        [
            "",
            "## Canonical PRD",
            "",
            f"- PRD path: `{relative_prd_path.as_posix()}`",
            "",
            "## Acceptance Summary",
            "",
            *acceptance_items,
            "",
            "## Delivery Notes",
            "",
            "- Recommended branch: `task/<issue-number>-<slug>`",
            "- Worktree command: `just worktree --issue <issue-number>`",
            "- PR should include: `Closes #<issue-number>`",
            "",
        ]
    )
    if dependency_marker:
        body_parts.append(dependency_marker)
        body_parts.append("")
    return "\n".join(body_parts)


# ---------------------------------------------------------------------------
# 路径与标签辅助函数
# ---------------------------------------------------------------------------


def resolve_prd_paths(repo_path: Path, prd_path: Path) -> tuple[Path, Path]:
    """解析 PRD 的绝对路径和仓库相对路径。

    Args:
        repo_path: 目标仓库路径。
        prd_path: PRD 文件路径。

    Returns:
        ``(absolute_prd_path, relative_prd_path)`` 元组。
    """

    absolute_prd_path = (repo_path / prd_path).resolve() if not prd_path.is_absolute() else prd_path
    relative_prd_path = absolute_prd_path.relative_to(repo_path.resolve())
    return absolute_prd_path, relative_prd_path


def build_issue_labels(
    request: IssueFromPrdRequest, effective_labels_config: LabelConfig
) -> list[str]:
    """构建 GitHub Issue 创建时的初始标签。

    默认标签集合为::

        ["type/{issue_type}", "status/backlog", "source/prd"]

    当 ``queue_ready=True``（且 ``publish_prd=False``）时，
    立即添加 ``agent/ready`` 标签，使 daemon 无需额外手动打标即可拾取该 Issue。

    当 ``issue_agent`` 为显式 agent 键（非 ``"auto"`` 或 ``"none"``）时，
    追加对应的 agent 路由标签。

    Args:
        request: Issue 创建请求。
        effective_labels_config: 要应用的标签名称配置。

    Returns:
        Issue 的初始标签列表。

    Raises:
        ValueError: 当 ``issue_agent`` 不是可识别的值时。
    """

    labels = [f"type/{request.issue_type}", "status/backlog", "source/prd"]
    # 仅在用户显式要求且本次命令不发布 PRD 时才添加 "ready"。
    # 当 publish_prd=True 时，ready 在 push 成功*之后*再添加，
    # 避免在未发布的 PRD 上就开始工作。
    if request.queue_ready and not request.publish_prd:
        labels.append(effective_labels_config.ready)
    if request.issue_agent in effective_labels_config.agent_labels:
        labels.append(effective_labels_config.agent_labels[request.issue_agent])
    elif request.issue_agent not in {"auto", "none"}:
        allowed = ", ".join([*effective_labels_config.agent_labels.keys(), "auto", "none"])
        raise ValueError(f"issue_agent must be one of: {allowed}")
    return labels


# ---------------------------------------------------------------------------
# Issue URL 解析与 PRD 回写
# ---------------------------------------------------------------------------


def parse_issue_number(issue_url: str) -> int:
    """从 Issue URL 中解析 GitHub Issue 编号。

    Args:
        issue_url: 客户端返回的 GitHub Issue URL。

    Returns:
        解析出的 Issue 编号。

    Raises:
        ValueError: 当 URL 中不包含 Issue 编号时。
    """

    issue_number_match = ISSUE_NUMBER_RE.search(issue_url)
    if issue_number_match is None:
        raise ValueError(f"Could not parse GitHub Issue number from URL: {issue_url}")
    return int(issue_number_match.group("issue_number"))


def write_issue_link(
    *, prd_text: str, absolute_prd_path: Path, issue_url: str, force: bool
) -> None:
    """将 GitHub Issue URL 回写到 PRD 中。

    链接以 ``- GitHub Issue: <url>`` 的形式插入。

    * 如果 ``force=True``，则就地替换已有的 ``- GitHub Issue:`` 行。
    * 如果 ``force=False``，已有行只可能是占位符（真实链接已被调用方的
      防重复检查拦截），该行会被移除，真实链接插入到 H1 标题之后。
    * 如果不存在这样的行，则在 H1 标题行之后立即插入。
    * 最后的兜底（未找到 H1）时，将链接插入文件最顶部。

    Args:
        prd_text: 原始 PRD 文本。
        absolute_prd_path: 要更新的 PRD 文件路径。
        issue_url: 创建的 GitHub Issue URL。
        force: 是否替换已有的 Issue URL。
    """

    link_line = f"- GitHub Issue: {issue_url}"
    updated_lines: list[str] = []
    link_written = False
    for line in prd_text.splitlines():
        if ISSUE_LINE_RE.match(line):
            if force:
                updated_lines.append(link_line)
                link_written = True
            # 跳过旧行（占位符，或 force=True 时已被替换的旧链接）。
            continue
        updated_lines.append(line)
        # 如果尚未替换已有链接，则在第一个 H1 标题后插入链接。
        if not link_written and line.startswith("# "):
            updated_lines.extend(["", link_line])
            link_written = True
    if not link_written:
        # 未找到 H1 标题 —— 安全起见前置插入。
        updated_lines.insert(0, link_line)

    absolute_prd_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# PRD 发布的 Git 辅助函数
# ---------------------------------------------------------------------------


def current_git_branch(repo_path: Path, process_runner: IProcessRunner) -> str:
    """返回当前 Git 分支名称。

    Args:
        repo_path: 目标仓库路径。
        process_runner: 执行 Git 命令的 runner。

    Returns:
        当前分支名称。

    Raises:
        RuntimeError: 当仓库处于 detached HEAD 状态时。
    """

    branch_result = process_runner.run(["git", "branch", "--show-current"], cwd=repo_path)
    current_branch = branch_result.stdout.strip()
    if not current_branch:
        raise RuntimeError("Cannot publish a PRD from a detached HEAD checkout.")
    return current_branch


def validate_ready_publish_branch(
    *, current_branch: str, git_base_branch: str, queue_ready: bool
) -> None:
    """验证 ready PRD 必须从 runner 基础分支发布。

    从功能分支发布 ``ready`` PRD 会在该分支上创建提交，而这个提交可能永远
    无法进入 ``main``，导致 daemon 检出基础分支时找不到该 PRD。

    Args:
        current_branch: 当前 Git 分支名称。
        git_base_branch: 配置的 runner 基础分支。
        queue_ready: 用户是否要求添加 ready 标签。

    Raises:
        RuntimeError: 当 ready PRD 试图从错误分支发布时。
    """

    if queue_ready and current_branch != git_base_branch:
        raise RuntimeError(
            "Cannot publish a ready PRD from branch "
            f"'{current_branch}'. Switch to base branch '{git_base_branch}' "
            "or use --no-ready."
        )


def validate_staged_changes_are_prd_only(
    repo_path: Path, relative_prd_path: Path, process_runner: IProcessRunner
) -> None:
    """当暂存区包含非目标文件时拒绝发布。

    防止将无关工作意外打包进 PRD 发布提交。

    Args:
        repo_path: 目标仓库路径。
        relative_prd_path: 相对于 repo_path 的 PRD 路径。
        process_runner: 执行 Git 命令的 runner。

    Raises:
        RuntimeError: 当暂存区包含非目标 PRD 文件时。
    """

    staged_result = process_runner.run(
        ["git", "diff", "--cached", "--name-only", "--"], cwd=repo_path
    )
    target_prd_path_text = relative_prd_path.as_posix()
    staged_path_texts = [
        staged_line.strip()
        for staged_line in staged_result.stdout.splitlines()
        if staged_line.strip()
    ]
    non_target_staged_paths = [
        staged_path_text
        for staged_path_text in staged_path_texts
        if staged_path_text != target_prd_path_text
    ]
    if non_target_staged_paths:
        staged_paths_text = ", ".join(sorted(non_target_staged_paths))
        raise RuntimeError(
            "Refusing to publish PRD because Git index contains staged changes "
            f"outside target PRD: {staged_paths_text}"
        )


def build_prd_commit_message(relative_prd_path: Path) -> str:
    """构建 PRD 发布的提交信息。

    从文件名 ``-prd-`` 之后的段提取 slug。
    示例::

        tasks/pending/P2-FEAT-20260527-190923-prd-example.md
        → "docs(prd): publish example"

    Args:
        relative_prd_path: 相对于仓库的 PRD 路径。

    Returns:
        PRD 发布的提交信息。
    """

    prd_slug = relative_prd_path.stem.split("-prd-", maxsplit=1)[-1]
    return f"docs(prd): publish {prd_slug}"


def publish_prd_file(publish_context: PrdPublishContext, process_runner: IProcessRunner) -> None:
    """仅暂存、提交并推送目标 PRD 文件。

    使用 ``git add -- <path>``、``git commit -m <msg> -- <path>`` 和
    ``git push <remote> <branch>``，确保发布提交中*仅*包含该 PRD 文件。

    Args:
        publish_context: Git 发布上下文。
        process_runner: 执行 Git 命令的 runner。
    """

    relative_prd_path_text = publish_context.relative_prd_path.as_posix()
    process_runner.run(["git", "add", "--", relative_prd_path_text], cwd=publish_context.repo_path)
    process_runner.run(
        [
            "git",
            "commit",
            "-m",
            build_prd_commit_message(publish_context.relative_prd_path),
            "--",
            relative_prd_path_text,
        ],
        cwd=publish_context.repo_path,
    )
    process_runner.run(
        ["git", "push", publish_context.git_remote, publish_context.current_branch],
        cwd=publish_context.repo_path,
    )


# ---------------------------------------------------------------------------
# Dependency resolution helpers
# ---------------------------------------------------------------------------


def _resolve_dependencies(
    prd_text: str,
    *,
    repo_path: Path | None = None,
    current_prd_path: Path | None = None,
    depends_on: tuple[int, ...] = (),
    depends_on_group: tuple[str, ...] = (),
) -> tuple[str, tuple[int, ...], tuple[str, ...]]:
    """Merge PRD structured dependencies, explicit markers and CLI overrides.

    Three sources are merged with CLI taking highest precedence:

    1. ``Delivery Dependencies`` section in the PRD.
    2. Explicit ``iar:depends-on`` markers in the PRD body.
    3. CLI arguments ``--depends-on`` and ``--depends-on-group``.

    Args:
        prd_text: Full PRD Markdown text.
        repo_path: Repository root, required when the PRD declares PRD file
            dependencies in ``Depends on tasks/issues``.
        current_prd_path: Current PRD path, used to reject self-dependencies.
        depends_on: CLI override issue numbers.
        depends_on_group: CLI override group names.

    Returns:
        ``(gate_type, resolved_issues, resolved_groups)``.
        ``gate_type`` is the gate from the PRD section (``none`` if absent).
    """
    from_prd = parse_delivery_dependencies(prd_text)

    # Start with PRD section values
    gate_type = from_prd.gate_type
    resolved_issues = list(from_prd.depends_on_issues)
    resolved_groups = list(from_prd.depends_on_groups)
    if gate_type == "hard" and from_prd.depends_on_prds:
        prd_issues, prd_groups = _materialize_prd_dependencies(
            repo_path=repo_path,
            current_prd_path=current_prd_path,
            prd_refs=from_prd.depends_on_prds,
        )
        resolved_issues.extend(prd_issues)
        resolved_groups.extend(prd_groups)

    # Merge explicit markers in PRD body (compat path)
    explicit = parse_dependency_marker(prd_text)
    if explicit is not None:
        resolved_issues.extend(explicit.issue_numbers)
        resolved_groups.extend(explicit.groups)

    # CLI overrides are additive
    resolved_issues.extend(depends_on)
    resolved_groups.extend(depends_on_group)

    # Deduplicate while preserving order
    seen_issues: set[int] = set()
    deduped_issues = [n for n in resolved_issues if not (n in seen_issues or seen_issues.add(n))]
    seen_groups: set[str] = set()
    deduped_groups = [g for g in resolved_groups if not (g in seen_groups or seen_groups.add(g))]

    return (
        gate_type,
        tuple(deduped_issues),
        tuple(deduped_groups),
    )


def _materialize_prd_dependencies(
    *,
    repo_path: Path | None,
    current_prd_path: Path | None,
    prd_refs: tuple[str, ...],
) -> tuple[list[int], list[str]]:
    """Resolve PRD path/name dependencies into Issue numbers or group names."""
    if repo_path is None:
        raise ValueError(
            "Cannot resolve PRD dependencies from 'Depends on tasks/issues' "
            "without a repository path. Use GitHub Issue numbers such as '#42', "
            "or call issue creation with repo_path available."
        )

    repo_root = repo_path.resolve()
    current_path = current_prd_path.resolve() if current_prd_path else None
    issue_numbers: list[int] = []
    group_names: list[str] = []

    for prd_ref in prd_refs:
        referenced_prd_path = _resolve_dependency_prd_path(
            repo_root=repo_root,
            prd_ref=prd_ref,
        )
        if current_path is not None and referenced_prd_path == current_path:
            raise ValueError(
                "Cannot materialize PRD dependency "
                f"{prd_ref!r}: it resolves to the current PRD. Remove the "
                "self-dependency or replace it with the intended upstream "
                "Issue number, for example '#42'."
            )

        referenced_text = referenced_prd_path.read_text(encoding="utf-8")
        issue_number = _extract_prd_issue_number(referenced_text)
        if issue_number is not None:
            issue_numbers.append(issue_number)
            continue

        try:
            referenced_dependencies = parse_delivery_dependencies(referenced_text)
        except ValueError as exc:
            relative_path = _format_repo_relative_path(repo_root, referenced_prd_path)
            raise ValueError(
                "Cannot parse Delivery Dependencies for referenced PRD "
                f"{relative_path!r} from dependency {prd_ref!r}: {exc}"
            ) from exc

        if referenced_dependencies.group:
            group_names.append(referenced_dependencies.group)
            continue

        relative_path = _format_repo_relative_path(repo_root, referenced_prd_path)
        raise ValueError(
            "Cannot materialize PRD dependency "
            f"{prd_ref!r} resolved to {relative_path!r}: the referenced PRD "
            "has no '- GitHub Issue: .../issues/N' link and no "
            "'Delivery Dependencies' Group. Publish the upstream PRD first, "
            "add its GitHub Issue link, or add '- Group: <group-name>' to "
            "the referenced PRD so this dependency can be materialized as "
            "'group:<group-name>'."
        )

    return issue_numbers, group_names


def _extract_prd_issue_number(prd_text: str) -> int | None:
    """Extract the first GitHub Issue number linked from a PRD."""
    for line in prd_text.splitlines():
        if not ISSUE_LINK_LINE_RE.match(line):
            # 占位符（如 "(待创建)"）视为尚未关联 Issue。
            continue
        issue_number_match = ISSUE_NUMBER_RE.search(line)
        if issue_number_match is None:
            raise ValueError(f"Invalid GitHub Issue link in PRD: {line}")
        return int(issue_number_match.group("issue_number"))
    return None


def _resolve_dependency_prd_path(*, repo_root: Path, prd_ref: str) -> Path:
    """Resolve a PRD dependency reference to one Markdown file in the repo."""
    cleaned_ref = prd_ref.strip().strip("`").strip()
    if not cleaned_ref:
        raise ValueError(
            "Empty PRD dependency reference in 'Depends on tasks/issues'. "
            "Leave the field empty or write 'none' when there is no dependency."
        )

    raw_path = Path(cleaned_ref).expanduser()
    if raw_path.is_absolute():
        return _validate_dependency_prd_path(
            repo_root=repo_root,
            candidate_path=raw_path,
            prd_ref=cleaned_ref,
        )

    if "/" in cleaned_ref or "\\" in cleaned_ref:
        return _resolve_repo_relative_dependency_prd_path(
            repo_root=repo_root,
            relative_ref=cleaned_ref,
        )

    repo_relative_matches = _existing_markdown_path_candidates(repo_root / cleaned_ref)
    task_matches = _find_task_prd_matches(repo_root=repo_root, prd_ref=cleaned_ref)
    matches = _dedupe_paths([*repo_relative_matches, *task_matches])

    if not matches:
        raise ValueError(_format_missing_prd_dependency_error(cleaned_ref))
    if len(matches) > 1:
        matched_paths = ", ".join(
            _format_repo_relative_path(repo_root, matched_path) for matched_path in matches
        )
        raise ValueError(
            "Ambiguous PRD dependency reference "
            f"{cleaned_ref!r} in 'Depends on tasks/issues'. It matches "
            f"multiple PRD files: {matched_paths}. Use a repo-relative path, "
            "for example 'tasks/pending/<filename>.md'."
        )
    return _validate_dependency_prd_path(
        repo_root=repo_root,
        candidate_path=matches[0],
        prd_ref=cleaned_ref,
    )


def _resolve_repo_relative_dependency_prd_path(*, repo_root: Path, relative_ref: str) -> Path:
    """Resolve an explicit repo-relative PRD dependency path."""
    candidate_paths = _existing_markdown_path_candidates(repo_root / relative_ref)
    if not candidate_paths:
        raise ValueError(_format_missing_prd_dependency_error(relative_ref))
    return _validate_dependency_prd_path(
        repo_root=repo_root,
        candidate_path=candidate_paths[0],
        prd_ref=relative_ref,
    )


def _existing_markdown_path_candidates(candidate_path: Path) -> list[Path]:
    """Return existing path candidates, trying ``.md`` when omitted."""
    candidates = [candidate_path]
    if candidate_path.suffix == "":
        candidates.append(candidate_path.with_suffix(".md"))
    return [
        path.resolve() for path in candidates if path.is_file() and path.suffix.lower() == ".md"
    ]


def _find_task_prd_matches(*, repo_root: Path, prd_ref: str) -> list[Path]:
    """Find PRD dependency matches by filename or stem under ``tasks/``."""
    tasks_path = repo_root / "tasks"
    if not tasks_path.is_dir():
        return []
    matches: list[Path] = []
    for candidate_path in tasks_path.rglob("*.md"):
        if candidate_path.name == prd_ref or candidate_path.stem == prd_ref:
            matches.append(candidate_path.resolve())
    return sorted(matches, key=lambda path: path.as_posix())


def _validate_dependency_prd_path(*, repo_root: Path, candidate_path: Path, prd_ref: str) -> Path:
    """Validate that a resolved PRD path is a Markdown file inside the repo."""
    resolved_path = candidate_path.resolve()
    try:
        resolved_path.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(
            "PRD dependency reference "
            f"{prd_ref!r} resolves outside the repository. Use a GitHub Issue "
            "number such as '#42' or a PRD path under this repository."
        ) from exc
    if not resolved_path.is_file() or resolved_path.suffix.lower() != ".md":
        raise ValueError(_format_missing_prd_dependency_error(prd_ref))
    return resolved_path


def _dedupe_paths(candidate_paths: list[Path]) -> list[Path]:
    """Deduplicate paths while preserving deterministic order."""
    seen_paths: set[Path] = set()
    deduped_paths: list[Path] = []
    for candidate_path in candidate_paths:
        resolved_path = candidate_path.resolve()
        if resolved_path in seen_paths:
            continue
        seen_paths.add(resolved_path)
        deduped_paths.append(resolved_path)
    return sorted(deduped_paths, key=lambda path: path.as_posix())


def _format_missing_prd_dependency_error(prd_ref: str) -> str:
    """Return actionable guidance for an unresolved PRD dependency ref."""
    return (
        "Could not resolve PRD dependency reference "
        f"{prd_ref!r} in 'Depends on tasks/issues'. Use a GitHub Issue number "
        "such as '#42', a repo-relative PRD path such as "
        "'tasks/pending/P2-FEAT-20260527-190923-prd-from-issue.md', or a "
        "unique PRD filename/stem under 'tasks/'. For no dependency, leave "
        "the field empty or write 'none'."
    )


def _format_repo_relative_path(repo_root: Path, candidate_path: Path) -> str:
    """Format a path relative to the repository when possible."""
    try:
        return candidate_path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return candidate_path.as_posix()


# ---------------------------------------------------------------------------
# 主流程编排
# ---------------------------------------------------------------------------


_EVIDENCE_FORMAT_PROMPT_TEMPLATE = """\
判断以下 checklist item 是否明确要求执行者产出特定格式的证据文件。
只考虑 item 本身对"证据格式"的要求，忽略对参考文件、目录、外部资源的描述。

Item: "{item_text}"

输出 JSON: {{"kind": "screenshot"}} 或 {{"kind": "none"}}
可选 kind: screenshot, pdf, txt, word, excel, csv, video, none
"""

_KNOWN_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {"screenshot", "pdf", "txt", "word", "excel", "csv", "video", "none"}
)


def resolve_agent_name(agent_name: str, default: str = "claude") -> str:
    """Return a concrete agent name, falling back to *default* for unknown values.

    CLI and config accept ``"auto"`` / ``"none"`` as aliases; this helper
    normalises them to a real agent that ``IContentGenerator`` can execute.
    """
    return agent_name if agent_name in ("claude", "codex", "kimi") else default


def _parse_evidence_format_with_agent(
    checklist_items: list[str],
    generator: IContentGenerator | None,
    cwd: Path,
    agent_name: str,
) -> dict[int, str]:
    """Use an agent to parse the required evidence format for each item.

    Returns a mapping ``{item_number: kind}``.  If the agent is unavailable
    or returns invalid JSON, returns an empty dict so the caller can fall
    back to regex matching.
    """
    if generator is None or not checklist_items:
        return {}

    resolved_agent = resolve_agent_name(agent_name)
    parsed_formats: dict[int, str] = {}

    for item_number, item_text in enumerate(checklist_items, start=1):
        prompt = _EVIDENCE_FORMAT_PROMPT_TEMPLATE.format(item_text=item_text)
        try:
            result = generator.generate(
                agent_name=resolved_agent,
                prompt=prompt,
                cwd=cwd,
                timeout=30,
            )
        except Exception:
            _logger.warning("Agent call failed for item %d, skipping", item_number)
            continue

        if result.return_code != 0:
            _logger.warning(
                "Agent exited with code %d for item %d: %s",
                result.return_code,
                item_number,
                result.stderr,
            )
            continue

        output_text = result.stdout.strip()
        if output_text.startswith("```"):
            lines = output_text.splitlines()
            if len(lines) >= 2:
                output_text = "\n".join(lines[1:-1]).strip()

        try:
            data = json.loads(output_text)
            if isinstance(data, dict):
                kind = str(data.get("kind", "")).strip().lower()
                if kind in _KNOWN_EVIDENCE_KINDS and kind != "none":
                    parsed_formats[item_number] = kind
        except json.JSONDecodeError:
            _logger.warning(
                "Agent output is not valid JSON for item %d: %s",
                item_number,
                output_text[:200],
            )

    return parsed_formats


def _build_evidence_format_markers(parsed_formats: dict[int, str]) -> str:
    """Build hidden ``iar:evidence-format`` markers from parsed formats."""
    if not parsed_formats:
        return ""
    lines = [
        f"<!-- iar:evidence-format item={num} kind={kind} -->"
        for num, kind in sorted(parsed_formats.items())
    ]
    return "\n".join(lines)


def create_issue_from_prd(
    *,
    request: IssueFromPrdRequest,
    github_client: IGitHubClient,
    process_runner: IProcessRunner | None = None,
    content_generator: IContentGenerator | None = None,
) -> str:
    """从 PRD 创建 GitHub Issue 并将 URL 回写到 PRD。

    编排流程：

    1. **路径解析** —— 将 ``prd_path`` 转换为绝对路径和仓库相对路径。
    2. **PRD 校验** —— 读取 PRD 并检查是否已存在 Issue 链接
       （防止重复创建，除非 ``force=True``）。
    3. **标题提取** —— 从 PRD H1 标题或文件名 slug 派生 fallback 标题；
       ``title_override`` 优先级最高。
    4. **标签组装** —— 构建初始标签集合（type、backlog、source，
       可选的 ready/agent 标签）。
    5. **发布预检**（可选）—— 当 ``publish_prd=True`` 时，
       验证分支状态和暂存区变更，然后捕获 ``PrdPublishContext`` 供第 9 步使用。
    6. **Fallback 正文** —— 基于 PRD 元数据构建确定的 Issue 正文
       （验收清单 + 引言 + 交付说明）。
    7. **内容生成**（可选）—— 当 ``generated_content_config`` 启用时，
       调用 ``generate_issue_content()``，遵循模块文档中描述的
       ``agent → template → fallback`` 级联策略。
       返回的 title/body 替换 fallback 值。
    8. **Issue 创建** —— 调用 ``github_client.create_issue()``。
    9. **回写** —— 将 Issue URL 持久化到 PRD 文件。
    10. **发布**（可选）—— 执行预检时捕获的发布上下文
        （stage、commit、push）。如果 ``queue_ready=True``，
        在 push 成功*之后*再添加 ready 标签。

    Args:
        request: Issue 创建请求。
        github_client: 与 GitHub 交互的客户端。
        process_runner: 用于 PRD 发布 Git 命令的可选 runner。
            当 ``publish_prd=True`` 时必需。
        content_generator: 用于 AI 生成 Issue 内容的可选生成器。
            当 ``generated_content_config`` 使用 agent 模式时必需。

    Returns:
        创建的 GitHub Issue URL。

    Raises:
        ValueError: 当 PRD 已有 GitHub Issue 链接且 ``force=False``，
            或当 ``publish_prd=True`` 时缺少 ``process_runner``。
        RuntimeError: 当 PRD 发布无法完成时（错误分支、暂存区冲突、
            push 失败等）。
    """

    # ------------------------------------------------------------------
    # 1. 解析路径，得到绝对路径（用于 I/O）和相对路径（用于 Issue 正文和提交信息）。
    # ------------------------------------------------------------------
    absolute_prd_path, relative_prd_path = resolve_prd_paths(request.repo_path, request.prd_path)

    # ------------------------------------------------------------------
    # 2. 读取 PRD 并防止意外重复创建 Issue。
    # ------------------------------------------------------------------
    prd_text = absolute_prd_path.read_text(encoding="utf-8")
    if not request.force and any(ISSUE_LINK_LINE_RE.match(line) for line in prd_text.splitlines()):
        raise ValueError("PRD already has a GitHub Issue link. Use --force to replace it.")

    # ------------------------------------------------------------------
    # 3. 派生 fallback 标题。当 PRD 没有 H1 标题时，使用文件名 slug（"-prd-" 之后部分）作为最后手段。
    # ------------------------------------------------------------------
    fallback_title = absolute_prd_path.stem.split("-prd-", maxsplit=1)[-1].replace("-", " ")
    fallback_title = (
        request.title_override
        or f"[{request.issue_type.title()}] {extract_title(prd_text, fallback_title)}"
    )

    # ------------------------------------------------------------------
    # 4. 构建标签。
    # ------------------------------------------------------------------
    effective_labels_config = request.labels_config or LabelConfig()
    labels = build_issue_labels(request, effective_labels_config)

    # ------------------------------------------------------------------
    # 4.5 解析并物化依赖声明。
    # ------------------------------------------------------------------
    gate_type, resolved_issues, resolved_groups = _resolve_dependencies(
        prd_text,
        repo_path=request.repo_path,
        current_prd_path=absolute_prd_path,
        depends_on=request.depends_on,
        depends_on_group=request.depends_on_group,
    )
    dependency_marker = ""
    if gate_type == "hard" and (resolved_issues or resolved_groups):
        dependency_marker = format_dependency_marker(
            issue_numbers=resolved_issues,
            groups=resolved_groups,
        )

    # ------------------------------------------------------------------
    # 5. 发布预检（仅在 publish_prd=True 时执行）。
    # ------------------------------------------------------------------
    publish_context: PrdPublishContext | None = None
    if request.publish_prd:
        if process_runner is None:
            raise ValueError("process_runner is required when publish_prd=True.")
        current_branch = current_git_branch(request.repo_path, process_runner)
        validate_ready_publish_branch(
            current_branch=current_branch,
            git_base_branch=request.git_base_branch,
            queue_ready=request.queue_ready,
        )
        validate_staged_changes_are_prd_only(request.repo_path, relative_prd_path, process_runner)
        publish_context = PrdPublishContext(
            repo_path=request.repo_path,
            relative_prd_path=relative_prd_path,
            git_remote=request.git_remote,
            current_branch=current_branch,
        )

    # ------------------------------------------------------------------
    # 6. 构建确定的 fallback 正文。
    # ------------------------------------------------------------------
    fallback_body = build_issue_body(
        relative_prd_path=relative_prd_path,
        title=fallback_title,
        acceptance_items=extract_acceptance_items(prd_text),
        prd_text=prd_text,
        dependency_marker=dependency_marker,
    )

    # ------------------------------------------------------------------
    # 7. 可选的 AI 内容生成（agent → template → fallback）。
    # ------------------------------------------------------------------
    title = fallback_title
    body = fallback_body
    gc_config = request.generated_content_config
    if gc_config is not None and gc_config.enabled:
        gc_context = build_issue_context(
            issue_type=request.issue_type,
            title=fallback_title,
            relative_prd_path=relative_prd_path,
            prd_text=prd_text,
            acceptance_items=extract_acceptance_items(prd_text),
        )
        gc_cwd = request.repo_path if content_generator is not None else None
        generated = generate_issue_content(
            config=gc_config,
            context=gc_context,
            fallback_title=fallback_title,
            fallback_body=fallback_body,
            generator=content_generator,
            cwd=gc_cwd,
        )
        title = generated.title
        body = generated.body

    # Ensure dependency marker survives AI-generated body as well.
    if dependency_marker and dependency_marker not in body:
        body = f"{body.rstrip()}\n\n{dependency_marker}\n"

    # ------------------------------------------------------------------
    # 7.5 物化 Realistic Validation 区块。
    # 确定性步骤，独立于 AI 生成正文：waiver 声明物化为 hidden marker，
    # 否则把 PRD 的验证清单复制为 Issue body 的未勾选清单，
    # 供 runner 的证据门禁与 PR 人工签收清单消费。
    # ------------------------------------------------------------------
    validation_checklist_items = extract_realistic_validation_items(prd_text)
    validation_waiver_reason = extract_validation_waiver_reason(prd_text)
    if validation_checklist_items or validation_waiver_reason is not None:
        format_markers = ""
        if request.parse_evidence_format_with_agent and content_generator is not None:
            parsed_formats = _parse_evidence_format_with_agent(
                checklist_items=validation_checklist_items,
                generator=content_generator,
                cwd=request.repo_path,
                agent_name=request.issue_agent,
            )
            format_markers = _build_evidence_format_markers(parsed_formats)
        validation_section = build_issue_validation_section(
            checklist_items=validation_checklist_items,
            waiver_reason=validation_waiver_reason,
            format_waiver_reason=extract_evidence_format_waiver_reason(prd_text),
            language=request.validation_language,
            structured_evidence=request.structured_evidence,
        )
        if format_markers:
            validation_section = f"{format_markers}\n\n{validation_section}"
        body = f"{body.rstrip()}\n\n{validation_section}\n"

    # ------------------------------------------------------------------
    # 8. 创建 GitHub Issue。
    # ------------------------------------------------------------------
    issue_url = github_client.create_issue(title=title, body=body, labels=labels)

    # ------------------------------------------------------------------
    # 9. 将 Issue URL 回写到 PRD。
    # ------------------------------------------------------------------
    write_issue_link(
        prd_text=prd_text,
        absolute_prd_path=absolute_prd_path,
        issue_url=issue_url,
        force=request.force,
    )

    # ------------------------------------------------------------------
    # 10. 发布 PRD 文件并可选地添加 ready 标签。
    # ------------------------------------------------------------------
    if publish_context is not None:
        publish_prd_file(publish_context, process_runner)
        if request.queue_ready:
            github_client.edit_issue_labels(
                parse_issue_number(issue_url), add=[effective_labels_config.ready]
            )

    _logger.info("Created GitHub Issue: %s", issue_url)
    return issue_url
