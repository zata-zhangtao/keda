"""Generate or rewrite a PRD from a GitHub Issue.

本模块实现 Issue 驱动 PRD 自动生成的核心流程，支持两种场景：

1. **新建 PRD**：Issue 没有关联 PRD → 生成新 PRD 文件，将 PRD 路径写回 Issue body。
2. **重写 PRD**：Issue 已有关联 PRD → 读取现有 PRD，结合 Issue 全部历史消息重写，覆盖原文件。

生成完成后自动更新 Issue label：移除 ``agent/rework-prd``，添加 ``source/prd``，
可选添加 ``agent/ready``。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    GeneratedContentConfig,
    IssueSummary,
)
from backend.core.use_cases.agent_runner_feedback import extract_prd_path
from backend.core.use_cases.generated_content import (
    build_prd_context,
    generate_prd_content,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CreatePrdFromIssueRequest:
    """创建 PRD 的输入参数。

    Attributes:
        repo_path: 仓库根目录绝对路径。
        issue: 待处理的 GitHub Issue。
        config: 应用配置（包含 label 配置）。
        generated_content_config: 内容生成配置；为 ``None`` 时使用 fallback。
        content_generator: AI 内容生成器；agent 模式必需。
        queue_ready: 成功后在 Issue 上添加 ``agent/ready`` label。
    """

    repo_path: Path
    issue: IssueSummary
    config: AppConfig
    generated_content_config: GeneratedContentConfig | None = None
    content_generator: IContentGenerator | None = None
    queue_ready: bool = False


_TYPE_ACRONYMS: dict[str, str] = {
    "feature": "FEAT",
    "feat": "FEAT",
    "bug": "BUG",
    "chore": "CHORE",
    "docs": "DOCS",
    "refactor": "REFACTOR",
    "spike": "SPIKE",
    "test": "TEST",
    "perf": "PERF",
    "security": "SEC",
}

_DEFAULT_PRD_PREFIX = "P2-FEAT"


def _generate_slug(issue_title: str) -> str:
    """将 Issue 标题转换为 URL 安全的 slug。

    处理步骤：

    1. 转小写。
    2. 移除非单词、非空格字符。
    3. 将空格和下划线压缩为 ``-``。
    4. 去除首尾 ``-``。
    5. 截断至 60 字符。

    Args:
        issue_title: Issue 标题。

    Returns:
        URL 安全的 slug 字符串。
    """
    slug = issue_title.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug[:60]


def _parse_prd_prefix(issue: IssueSummary) -> str:
    """从 Issue 标签或标题推导 PRD 文件名前缀。

    优先使用 ``priority/<p>`` 和 ``type/<t>`` label；缺失时从标题前缀 ``[Type]``
    推断；仍缺失则回退到 ``P2-FEAT``。

    Args:
        issue: GitHub Issue。

    Returns:
        ``P<priority>-<TYPE>`` 形式的前缀。
    """
    priority = ""
    issue_type = ""

    for label in issue.labels:
        label_lower = label.lower()
        if label_lower.startswith("priority/"):
            priority_value = label.split("/", 1)[1].strip()
            if priority_value:
                priority = f"P{priority_value.lstrip('Pp').upper()}"
        elif label_lower.startswith("type/"):
            type_value = label.split("/", 1)[1].strip().lower()
            issue_type = _TYPE_ACRONYMS.get(type_value, type_value.upper())

    if not issue_type:
        title_prefix_match = re.match(r"^\[([^\]]+)\]", issue.title)
        if title_prefix_match:
            issue_type = _TYPE_ACRONYMS.get(
                title_prefix_match.group(1).strip().lower(),
                title_prefix_match.group(1).strip().upper(),
            )

    return f"{priority or 'P2'}-{issue_type or 'FEAT'}"


def _resolve_prd_path(
    *,
    repo_path: Path,
    issue: IssueSummary,
    pending_dir: Path = Path("tasks/pending"),
) -> Path:
    """解析目标 PRD 文件路径。

    如果 Issue body 中已包含 ``PRD path:`` 锚点，则复用该路径；
    否则按 ``P<priority>-<TYPE>-YYYYMMDD-HHMMSS-prd-<slug>.md`` 命名规则在
    ``pending_dir`` 下生成新文件。

    Args:
        repo_path: 仓库根目录。
        issue: GitHub Issue。
        pending_dir: 新 PRD 的默认存放目录（相对于仓库根）。

    Returns:
        绝对路径，指向现有或待创建的 PRD 文件。
    """
    existing_prd_path = extract_prd_path(issue.body)
    if existing_prd_path:
        return repo_path / existing_prd_path

    slug = _generate_slug(issue.title)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    prefix = _parse_prd_prefix(issue)
    filename = f"{prefix}-{timestamp}-prd-{slug}.md"
    return repo_path / pending_dir / filename


def _build_fallback_prd(issue: IssueSummary) -> str:
    """构建最小 fallback PRD。

    当生成被禁用或全部生成方式失败时，返回包含基本结构的 markdown，
    确保 PRD 至少满足 ``_validate_prd_output`` 的校验。

    Args:
        issue: GitHub Issue。

    Returns:
        最小 PRD markdown 文本。
    """
    return "\n".join(
        [
            f"# PRD: {issue.title}",
            "",
            f"- GitHub Issue: {issue.url}",
            "",
            "## 1. Introduction & Goals",
            "",
            f"{issue.body}",
            "",
            "## 2. Requirement Shape",
            "",
            "- **Actor**: User",
            "- **Trigger**: TBD",
            "- **Expected Behavior**: TBD",
            "- **Scope Boundary**: TBD",
            "",
            "## 3. Acceptance Checklist",
            "",
            "- [ ] Define requirements",
            "- [ ] Implement the feature",
            "- [ ] Run verification",
            "",
        ]
    )


def _update_issue_body_with_prd_path(issue_body: str, prd_relative_path: str) -> str:
    """在 Issue body 中插入或更新 ``PRD path`` 锚点。

    如果 body 中已存在 ``PRD path:`` 行，则替换为新的路径；
    否则在 body 顶部插入新行。

    Args:
        issue_body: 原始 Issue body。
        prd_relative_path: PRD 文件相对于仓库根的路径。

    Returns:
        更新后的 Issue body。
    """
    prd_line = f"- PRD path: `{prd_relative_path}`"
    lines = issue_body.splitlines()
    updated_lines: list[str] = []
    path_written = False
    for line in lines:
        if re.search(r"^\s*(?:[-*]\s+)?PRD path:\s*`[^`]+`", line):
            updated_lines.append(prd_line)
            path_written = True
        else:
            updated_lines.append(line)
    if not path_written:
        updated_lines.insert(0, prd_line)
        updated_lines.insert(1, "")
    return "\n".join(updated_lines)


def _extract_existing_prd_text(prd_path: Path) -> str:
    """读取现有 PRD 文本。

    Args:
        prd_path: PRD 文件绝对路径。

    Returns:
        文件内容；文件不存在时返回空字符串。
    """
    if prd_path.exists():
        return prd_path.read_text(encoding="utf-8")
    return ""


def create_prd_from_issue(
    *,
    request: CreatePrdFromIssueRequest,
    github_client: IGitHubClient,
) -> Path:
    """根据 GitHub Issue 生成或重写 PRD。

    执行流程：

    1. 解析目标 PRD 路径（复用已有或新建）。
    2. 读取现有 PRD 文本（如有）。
    3. 获取 Issue 评论列表。
    4. 调用 ``generate_prd_content`` 生成 PRD 文本（支持 template/agent/fallback）。
    5. 写入文件（覆盖或新建）。
    6. 如果是新建 PRD，更新 Issue body 添加 ``PRD path`` 锚点。
    7. 更新 labels：移除 ``agent/rework-prd``，添加 ``source/prd``，可选添加 ``agent/ready``。
    8. 在 Issue 上评论成功通知。

    失败时抛出异常，由调用方（编排器）捕获并标记 ``agent/failed``。

    Args:
        request: 创建 PRD 的请求参数。
        github_client: GitHub 客户端接口。

    Returns:
        写入的 PRD 文件绝对路径。

    Raises:
        Exception: 当 PRD 生成或写入失败时，原样抛出异常。
    """
    issue = request.issue
    repo_path = request.repo_path
    labels_config = request.config.labels

    prd_path = _resolve_prd_path(repo_path=repo_path, issue=issue)
    existing_prd_text = _extract_existing_prd_text(prd_path)
    is_rewrite = bool(existing_prd_text)

    comments = github_client.list_issue_comments(issue.number)
    gc_config = request.generated_content_config

    if gc_config is not None and gc_config.enabled:
        gc_context = build_prd_context(
            issue=issue,
            comments=comments,
            existing_prd_text=existing_prd_text,
            repo_path=repo_path,
        )
        gc_cwd = repo_path if request.content_generator is not None else None
        generated = generate_prd_content(
            config=gc_config,
            context=gc_context,
            fallback_prd_text=_build_fallback_prd(issue),
            generator=request.content_generator,
            cwd=gc_cwd,
        )
        prd_text = generated.text
    else:
        prd_text = _build_fallback_prd(issue)

    prd_path.write_text(prd_text, encoding="utf-8")
    _logger.info("%s PRD at %s", "Rewrote" if is_rewrite else "Created", prd_path)

    relative_prd_path = prd_path.relative_to(repo_path.resolve()).as_posix()

    updated_body = _update_issue_body_with_prd_path(issue.body, relative_prd_path)
    if updated_body != issue.body:
        github_client.edit_issue_body(issue.number, updated_body)

    labels_to_remove = [labels_config.rework_prd]
    labels_to_add = ["source/prd"]
    if request.queue_ready:
        labels_to_add.append(labels_config.ready)

    github_client.edit_issue_labels(
        issue.number,
        add=labels_to_add,
        remove=labels_to_remove,
    )

    action = "rewritten" if is_rewrite else "generated"
    source_label = (
        "AI agent"
        if gc_config is not None and gc_config.enabled
        else "fallback template"
    )
    github_client.comment_issue(
        issue.number,
        f"PRD {action} successfully.\n\n"
        f"- PRD path: `{relative_prd_path}`\n"
        f"- Source: {source_label}\n",
    )

    return prd_path
