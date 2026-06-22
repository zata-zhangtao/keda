"""为 GitHub Issues 和 PRs 生成内容。

本模块提供 AI 辅助的内容生成能力，支持两种目标：

1. **Issue 内容生成**（``generate_issue_content``）：根据 PRD 上下文生成
   Issue 标题和正文，用于 ``iar issue create`` 工作流。
2. **PR 内容生成**（``generate_pr_content``）：根据 Issue 信息、提交日志和
   diff 统计生成 PR 标题和正文，用于 ``publish_changes`` 工作流。

两条路径都遵循相同的三级级联策略：

- **Agent 模式**：调用本地 AI agent（如 Claude/Codex/Kimi）生成内容。
- **Template 模式**：使用 ``.format()`` 模板渲染，变量来自上下文对象。
- **Hard fallback**：当上述模式均失败或禁用时，返回调用方提供的 fallback 内容。

特别地，当主模式为 agent 且 ``config.fallback == "template"`` 时，
agent 失败后还会尝试 template 模式作为中间兜底，最后才退回 hard fallback。

所有模板渲染通过 ``_render_template`` 统一处理，支持 ``IssueContext``
和 ``PrContext`` 两种上下文对象。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import (
    GeneratedContentConfig,
    GeneratedContentTargetConfig,
    GeneratedIssueContent,
    GeneratedPrContent,
    IssueSummary,
)

_logger = logging.getLogger(__name__)

# GitHub Issue 标题的最大长度限制。超过此长度会被截断并追加 "..."
_MAX_TITLE_LENGTH = 256


# ---------------------------------------------------------------------------
# 上下文对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueContext:
    """Issue 内容生成的上下文变量。

    这些字段会被模板字符串通过 ``.format(**context.__dict__)`` 引用，
    因此字段名称直接对应模板中的占位符名（如 ``{prd_introduction}``）。

    Attributes:
        issue_type: Issue 类型，如 ``"feature"``、``"bug"``。
        title: fallback 标题（通常由调用方从 PRD 文件名或 H1 提取）。
        prd_title: 从 PRD H1 标题中提取的纯净标题（去掉 ``PRD:`` 前缀）。
        relative_prd_path: PRD 文件相对于仓库根目录的路径（字符串形式）。
        acceptance_items: 验收清单条目，以换行符连接的 Markdown 复选框文本。
        prd_text: PRD 的完整原始文本，供 agent 模式作为完整上下文输入。
        prd_introduction: PRD 第一节（引言/概述）的内容。
        prd_goals: PRD 目标章节的内容。
        prd_requirement_shape: PRD 需求形态章节的内容。
        prd_change_impact_tree: PRD 变更影响树章节的内容。
    """

    issue_type: str
    title: str
    prd_title: str
    relative_prd_path: str
    acceptance_items: str
    prd_text: str
    prd_introduction: str
    prd_goals: str
    prd_requirement_shape: str
    prd_change_impact_tree: str


@dataclass(frozen=True)
class PrContext:
    """PR 内容生成的上下文变量。

    与 ``IssueContext`` 类似，字段名称直接作为模板占位符使用。

    Attributes:
        issue_number: 关联的 GitHub Issue 编号。
        issue_title: Issue 标题。
        issue_body: Issue 正文。
        branch: 当前工作分支名称。
        base_branch: 基础分支名称（如 ``main``）。
        commit_log: 从 ``base_branch`` 到 HEAD 的提交信息列表。
        commit_messages: ``commit_log`` 的别名，保持与旧模板兼容。
        diff_stat: ``git diff --stat`` 的输出，显示变更文件统计。
        git_diff_stat: ``diff_stat`` 的别名，保持与旧模板兼容。
    """

    issue_number: int
    issue_title: str
    issue_body: str
    branch: str
    base_branch: str
    commit_log: str
    commit_messages: str
    diff_stat: str
    git_diff_stat: str


@dataclass(frozen=True)
class GeneratedPrdContent:
    """PRD 内容生成结果。

    Attributes:
        text: 生成的 PRD 完整 markdown 文本。
        source: 生成来源标识，如 ``"template"``、``"agent"`` 或 ``"fallback"``。
    """

    text: str
    source: str = "fallback"


@dataclass(frozen=True)
class PrdContext:
    """PRD 内容生成的上下文变量。

    字段名称直接作为模板占位符使用，与 ``IssueContext`` / ``PrContext`` 一致。

    Attributes:
        issue_number: Issue 编号。
        issue_title: Issue 标题。
        issue_body: Issue 正文。
        issue_comments: 所有评论拼接后的文本，每条评论前缀 ``Comment:\n``。
        existing_prd_text: 现有 PRD 文本（如果 Issue 已关联 PRD）。
        repo_structure_summary: 仓库顶层结构摘要，用于 agent 了解项目布局。
    """

    issue_number: int
    issue_title: str
    issue_body: str
    issue_comments: str
    existing_prd_text: str
    repo_structure_summary: str


_IGNORED_REPO_ENTRIES: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".iar",
        ".agent-runner",
        "dist",
        "build",
        ".DS_Store",
    }
)


def _is_ignored_repo_entry(path: Path) -> bool:
    """判断目录项是否应被排除在仓库结构摘要之外。"""
    name = path.name
    if name in _IGNORED_REPO_ENTRIES:
        return True
    if name.startswith(".") and name not in {".github", ".claude"}:
        return True
    return False


def _build_repo_structure_summary(
    repo_path: Path,
    *,
    max_depth: int = 3,
    max_entries_per_dir: int = 30,
) -> str:
    """为 PRD prompt 构建仓库结构摘要。

    遍历仓库根目录下有限深度的目录树，输出目录和文件列表。
    用于给 agent 提供项目布局上下文，避免暴露大量无关细节。

    Args:
        repo_path: 仓库根目录。
        max_depth: 最大遍历深度。
        max_entries_per_dir: 每个目录最多列出的条目数。

    Returns:
        格式化的仓库结构摘要文本。
    """
    if not repo_path.exists():
        return ""

    summary_lines: list[str] = []

    def _walk(current_path: Path, depth: int, prefix: str) -> None:
        if depth > max_depth:
            return
        try:
            entries = [
                entry
                for entry in current_path.iterdir()
                if not _is_ignored_repo_entry(entry)
            ]
        except OSError:
            return
        entries.sort(key=lambda p: (p.is_file(), p.name.lower()))
        visible_entries = entries[:max_entries_per_dir]
        for entry in visible_entries:
            suffix = "/" if entry.is_dir() else ""
            summary_lines.append(f"{prefix}{entry.name}{suffix}")
            if entry.is_dir():
                _walk(entry, depth + 1, f"{prefix}  ")
        if len(entries) > max_entries_per_dir:
            summary_lines.append(
                f"{prefix}... ({len(entries) - max_entries_per_dir} more)"
            )

    _walk(repo_path, 1, "")
    return "\n".join(summary_lines)


# ---------------------------------------------------------------------------
# PRD 章节提取
# ---------------------------------------------------------------------------


def extract_prd_section(prd_text: str, section_keywords: tuple[str, ...]) -> str:
    """根据关键词从 PRD 中提取指定章节的内容。

    扫描逻辑：

    1. 逐行读取 PRD，寻找以 ``## `` 开头的 H2 标题。
    2. 将标题（去掉 ``## `` 后）转小写，检查是否包含任意一个关键词。
    3. 第一个匹配的 H2 标志着目标章节的开始；下一个 H2（或文件结束）标志着结束。
    4. 返回章节内所有行（不包括标题行本身），去除首尾空白。

    注意：本函数对中文 PRD 的兼容性依赖于 ``section_keywords`` 中是否包含
    中文关键词。例如引言章节需要 ``"引言"`` 或 ``"概述"`` 才能匹配
    ``## 1. 引言与目标``。

    Args:
        prd_text: PRD 的完整文本。
        section_keywords: 用于匹配章节标题的关键词元组。

    Returns:
        提取的章节内容字符串；未找到匹配章节时返回空字符串。
    """
    lines = prd_text.splitlines()
    in_section = False
    section_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if in_section:
                # 遇到下一个 H2 标题，当前章节结束。
                break
            heading = line[3:].strip().lower()
            if any(keyword in heading for keyword in section_keywords):
                in_section = True
                continue
        if in_section:
            section_lines.append(line)
    return "\n".join(section_lines).strip()


def extract_first_h2_section(prd_text: str) -> str:
    """提取 PRD 中第一个 ``## `` 二级标题下的全部内容。

    当按关键词提取引言失败时，使用此函数作为兜底：无论第一节标题如何命名，
    都把它下面的内容取出来，避免 Issue fallback 正文出现空的 Summary。

    Args:
        prd_text: PRD 的完整文本。

    Returns:
        第一个 ``## `` 章节的内容；未找到 ``## `` 标题时返回空字符串。
    """
    lines = prd_text.splitlines()
    in_section = False
    section_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if in_section:
                break
            in_section = True
            continue
        if in_section:
            section_lines.append(line)
    return "\n".join(section_lines).strip()


# ---------------------------------------------------------------------------
# 上下文构建
# ---------------------------------------------------------------------------


def build_issue_context(
    *,
    issue_type: str,
    title: str,
    relative_prd_path: Path,
    prd_text: str,
    acceptance_items: list[str],
) -> IssueContext:
    """为 Issue 内容生成构建上下文变量。

    从 PRD 文本中提取多个章节（引言、目标、需求形态、变更影响），
    同时从 PRD 的 H1 标题中提取纯净标题作为 ``prd_title``。

    Args:
        issue_type: Issue 类型标识。
        title: fallback 标题。
        relative_prd_path: PRD 文件相对路径。
        prd_text: 完整 PRD 文本。
        acceptance_items: 验收清单条目列表。

    Returns:
        供模板渲染或 agent prompt 使用的 ``IssueContext`` 实例。
    """
    # 提取 PRD 的各个章节。关键词列表需覆盖中英文场景。
    introduction = extract_prd_section(
        prd_text, ("introduction", "intro", "引言", "概述")
    )
    if not introduction:
        introduction = extract_first_h2_section(prd_text)
    goals = extract_prd_section(prd_text, ("goal", "目标"))
    requirement_shape = extract_prd_section(prd_text, ("requirement", "需求", "shape"))
    change_impact_tree = extract_prd_section(
        prd_text, ("change impact", "impact tree", "变更影响")
    )

    # 从 PRD 的第一行 H1 标题中提取纯净标题，去掉 "PRD:" / "PRD：" 前缀。
    prd_title_text = title
    for line in prd_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            prd_title_text = re.sub(r"^PRD[:：]\s*", "", stripped[2:]).strip()
            break

    return IssueContext(
        issue_type=issue_type,
        title=title,
        prd_title=prd_title_text,
        relative_prd_path=relative_prd_path.as_posix(),
        acceptance_items="\n".join(acceptance_items),
        prd_text=prd_text,
        prd_introduction=introduction,
        prd_goals=goals,
        prd_requirement_shape=requirement_shape,
        prd_change_impact_tree=change_impact_tree,
    )


# ---------------------------------------------------------------------------
# 模板渲染辅助函数
# ---------------------------------------------------------------------------


def build_prd_context(
    *,
    issue: IssueSummary,
    comments: list[str],
    existing_prd_text: str,
    repo_path: Path,
) -> PrdContext:
    """为 PRD 内容生成构建上下文变量。

    Args:
        issue: 关联的 GitHub Issue。
        comments: Issue 评论列表。
        existing_prd_text: 现有 PRD 文本（如有）。
        repo_path: 仓库根目录路径。

    Returns:
        供模板渲染或 agent prompt 使用的 ``PrdContext`` 实例。
    """
    return PrdContext(
        issue_number=issue.number,
        issue_title=issue.title,
        issue_body=issue.body,
        issue_comments="\n\n".join(f"Comment:\n{c}" for c in comments),
        existing_prd_text=existing_prd_text,
        repo_structure_summary=_build_repo_structure_summary(repo_path),
    )


def _render_template(
    template: str, context: IssueContext | PrContext | PrdContext
) -> str:
    """使用上下文变量渲染模板字符串。

    直接调用 ``str.format(**context.__dict__)``，因此模板中的占位符必须与
    上下文对象的字段名完全一致（如 ``{prd_introduction}``、``{issue_number}``）。

    Args:
        template: 包含占位符的模板字符串。
        context: 提供变量值的上下文对象。

    Returns:
        渲染后的字符串。

    Raises:
        KeyError: 当模板中包含上下文对象不存在的占位符时。
        ValueError: 当模板格式不合法时。
    """
    return template.format(**context.__dict__)


def _try_render_templates(
    target: GeneratedContentTargetConfig,
    context: IssueContext | PrContext | PrdContext,
) -> tuple[str, str]:
    """尝试渲染标题和正文模板。

    分别渲染 ``title_template`` 和 ``body_template``。如果某个模板未配置
    或渲染失败（KeyError/ValueError），则对应结果为空字符串。

    Args:
        target: 包含 ``title_template`` 和 ``body_template`` 的目标配置。
        context: 提供变量值的上下文对象。

    Returns:
        ``(generated_title, generated_body)`` 元组。空字符串表示
        该模板缺失或渲染失败。
    """
    generated_title = ""
    generated_body = ""
    if target.title_template:
        try:
            generated_title = _render_template(target.title_template, context)
        except (KeyError, ValueError):
            pass
    if target.body_template:
        try:
            generated_body = _render_template(target.body_template, context)
        except (KeyError, ValueError):
            pass
    return generated_title, generated_body


def _truncate_text(text: str, max_chars: int) -> str:
    """将文本截断至指定长度，超出部分用省略号替代。

    用于控制 agent prompt 的输入长度，防止超过模型的上下文窗口限制。

    Args:
        text: 原始文本。
        max_chars: 最大允许字符数。

    Returns:
        截断后的文本。如果原始文本未超过限制则原样返回。
    """
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


# ---------------------------------------------------------------------------
# 内容校验
# ---------------------------------------------------------------------------


def _validate_issue_body(body: str, relative_prd_path: str) -> bool:
    """验证 Issue 正文是否包含必需的 PRD 路径锚点。

    Issue 正文必须包含 ``- PRD path: `{relative_prd_path}` `` 这一行，
    否则 runner 无法从 Issue 中反向定位到对应的 PRD 文件。

    Args:
        body: 待验证的 Issue 正文。
        relative_prd_path: 期望的 PRD 相对路径。

    Returns:
        包含锚点时返回 ``True``，否则返回 ``False``。
    """
    if not body or not body.strip():
        return False
    anchor = f"- PRD path: `{relative_prd_path}`"
    return anchor in body


def _validate_pr_body(body: str, issue_number: int) -> bool:
    """验证 PR 正文是否包含必需的 Closes 锚点。

    PR 正文必须包含 ``Closes #{issue_number}`` 形式的文本，
    以便 GitHub 在合并 PR 时自动关闭关联的 Issue。

    Args:
        body: 待验证的 PR 正文。
        issue_number: 期望关闭的 Issue 编号。

    Returns:
        包含锚点时返回 ``True``，否则返回 ``False``。
    """
    if not body or not body.strip():
        return False
    closes_pattern = rf"Closes\s*#\s*{issue_number}"
    return bool(re.search(closes_pattern, body))


# ---------------------------------------------------------------------------
# Agent 调用与输出解析
# ---------------------------------------------------------------------------


# 当 target 与全局默认 agent 都为 "auto" 时收敛到的首选 agent。与
# run_agent_once.choose_agent 的最终兜底保持一致（auto -> claude），避免把 "auto"
# 透传到命令构造处被静默当作 codex。
_DEFAULT_AUTO_AGENT = "claude"


def _resolve_generation_agent(target_agent: str, default_agent: str) -> str:
    """解析内容生成使用的具体 agent 名称。

    解析顺序与 :func:`backend.core.use_cases.run_agent_once.choose_agent` 的兜底
    语义保持一致：target 级显式 agent 优先，其次 generated-content 的全局
    ``default_agent``；两者都为 ``"auto"`` 时收敛到 :data:`_DEFAULT_AUTO_AGENT`
    （``claude``），而不是把 ``"auto"`` 透传到命令构造处被静默当作 codex。

    Args:
        target_agent: 目标级配置的 agent（``GeneratedContentTargetConfig.agent``）。
        default_agent: generated-content 的全局默认 agent（``default_agent``）。

    Returns:
        具体 agent 名称（如 ``"claude"`` / ``"codex"`` / ``"kimi"``）。
    """
    if target_agent != "auto":
        return target_agent
    if default_agent != "auto":
        return default_agent
    return _DEFAULT_AUTO_AGENT


def _run_content_generator(
    generator: IContentGenerator,
    agent_name: str,
    prompt: str,
    cwd: Path,
    timeout_seconds: int,
) -> str:
    """运行内容生成器并返回原始输出文本。

    如果 agent 进程返回非零退出码，记录警告日志并返回空字符串，
    让调用方可以回退到 fallback 内容。

    Args:
        generator: 内容生成器接口实例。
        agent_name: 要使用的 agent 名称（如 ``"claude"``、``"kimi"``）。
        prompt: 发送给 agent 的完整 prompt 文本。
        cwd: agent 工作目录。
        timeout_seconds: agent 执行超时时间（秒）。

    Returns:
        agent 的标准输出（已去除首尾空白）。执行失败时返回空字符串。
    """
    result = generator.generate(
        agent_name=agent_name,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout_seconds,
    )
    if result.return_code != 0:
        _logger.warning(
            "Content generator exited with code %d: %s",
            result.return_code,
            result.stderr,
        )
        return ""
    return result.stdout.strip()


def _parse_json_output(output_text: str) -> tuple[str, str]:
    """从 JSON 输出中解析标题和正文。

    支持被 Markdown 代码块包裹的 JSON（如 `` ```json\n{...}\n``` ``），
    会自动去掉代码块标记后再解析。

    Args:
        output_text: agent 输出的原始文本。

    Returns:
        ``(title, body)`` 元组。解析失败时返回 ``("", "")``。
    """
    text = output_text.strip()
    # 处理被 Markdown 代码块包裹的情况（例如 ```json\n{...}\n```）
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            title = str(data.get("title", "")).strip()
            body = str(data.get("body", "")).strip()
            return title, body
    except json.JSONDecodeError:
        pass
    return "", ""


def _parse_markdown_output(output_text: str) -> tuple[str, str]:
    """从 Markdown 输出中提取标题。

    将第一个非空行作为标题（去掉前导的 ``# `` 标记）。
    正文返回完整的原始输出，由调用方决定如何使用。

    Args:
        output_text: agent 输出的原始文本。

    Returns:
        ``(title, body)`` 元组。如果输出为空，title 为空字符串。
    """
    lines = output_text.splitlines()
    title = ""
    body = output_text
    for line in lines:
        stripped = line.strip()
        if stripped:
            title = stripped.lstrip("# ").strip()
            body = output_text
            break
    return title, body


def _validate_prd_output(text: str) -> bool:
    """验证生成的 PRD 文本是否符合基本结构要求。

    检查项：

    1. 文本必须以 ``# PRD:`` 开头。
    2. 必须包含至少一个 ``## `` 二级标题。
    3. 必须包含 ``- GitHub Issue:`` 锚点行。

    Args:
        text: 待验证的 PRD 文本。

    Returns:
        符合基本要求时返回 ``True``，否则 ``False``。
    """
    if not text or not text.strip():
        return False
    stripped = text.strip()
    if not stripped.startswith("# PRD:"):
        return False
    if "## " not in stripped:
        return False
    if "- GitHub Issue:" not in stripped:
        return False
    return True


# ---------------------------------------------------------------------------
# prd skill 规范来源（单一来源；禁止硬编码安装路径）
# ---------------------------------------------------------------------------

# 环境变量覆盖 prd skill 路径，便于全局工具 / 跨仓库运行（runner 在产品仓执行，
# skill 在用户级目录）下显式指定，而非硬编码安装路径。
_PRD_SKILL_PATH_ENV_VAR = "IAR_PRD_SKILL_PATH"
# 默认安装位置相对用户主目录派生（不写死绝对安装路径）。
_DEFAULT_PRD_SKILL_RELATIVE_PATH = Path(".claude") / "skills" / "prd" / "SKILL.md"


def resolve_prd_skill_path(explicit_path: Path | None = None) -> Path:
    """解析 ``prd`` skill ``SKILL.md`` 路径，不硬编码绝对安装路径。

    解析优先级：显式入参 → ``IAR_PRD_SKILL_PATH`` 环境变量 →
    ``~/.claude/skills/prd/SKILL.md``（相对主目录派生）。

    Args:
        explicit_path: 调用方显式指定的路径；为 ``None`` 时回落到环境变量/默认。

    Returns:
        待读取的 skill 规范文件路径（不保证存在）。
    """
    if explicit_path is not None:
        return explicit_path
    env_value = os.environ.get(_PRD_SKILL_PATH_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser()
    return Path.home() / _DEFAULT_PRD_SKILL_RELATIVE_PATH


def load_prd_skill_spec(explicit_path: Path | None = None) -> str | None:
    """读取 ``prd`` skill 规范文本，不可达时安全返回 ``None``。

    Args:
        explicit_path: 显式 skill 路径；为 ``None`` 时按 :func:`resolve_prd_skill_path`
            的优先级解析。

    Returns:
        skill 规范文本（已 strip）；文件缺失/不可读/为空时返回 ``None``，
        由调用方回退到现有模板 prompt。
    """
    skill_path = resolve_prd_skill_path(explicit_path)
    try:
        skill_text = skill_path.read_text(encoding="utf-8")
    except OSError:
        _logger.warning(
            "prd skill spec unreachable at %s; falling back to template prompt.",
            skill_path,
        )
        return None
    skill_text = skill_text.strip()
    return skill_text or None


def _build_prd_agent_prompt(
    skill_spec: str, context: PrdContext, max_context_chars: int
) -> str:
    """用 ``prd`` skill 规范 + PRD 上下文组合 agent prompt。

    skill 规范是方法论与输出契约的单一来源，始终完整注入（不截断）；
    ``max_context_chars`` 只约束可变的输入上下文（Issue 正文/评论/现有 PRD/仓库
    结构），避免超长 Issue 线程撑爆 prompt 而又不丢失规范本身。

    Args:
        skill_spec: ``prd`` skill ``SKILL.md`` 全文。
        context: PRD 上下文变量。
        max_context_chars: 可变上下文部分的最大字符数。

    Returns:
        发送给内容生成器的完整 prompt。
    """
    context_block = "\n".join(
        [
            f"GitHub Issue #{context.issue_number}: {context.issue_title}",
            "",
            "Issue Body:",
            context.issue_body,
            "",
            "Issue Comments (chronological):",
            context.issue_comments,
            "",
            "Existing PRD (rewrite if present, otherwise empty):",
            context.existing_prd_text,
            "",
            "Repository Structure Summary:",
            context.repo_structure_summary,
        ]
    )
    context_block = _truncate_text(context_block, max_context_chars)
    return "\n".join(
        [
            skill_spec,
            "",
            "---",
            "",
            "Follow the PRD methodology and output contract above. Apply it to the "
            "GitHub Issue and repository context below.",
            "",
            context_block,
            "",
            "Output rules:",
            "- Write the PRD in the same language as the Issue title.",
            "- The PRD MUST start with `# PRD: <title>` and include a "
            "`- GitHub Issue:` line.",
            "- Output only the PRD markdown, with no extra commentary.",
        ]
    )


def generate_prd_content(
    *,
    config: GeneratedContentConfig,
    context: PrdContext,
    fallback_prd_text: str,
    generator: IContentGenerator | None = None,
    cwd: Path | None = None,
    prd_skill_path: Path | None = None,
) -> GeneratedPrdContent:
    """生成 PRD markdown，支持多级回退。

    执行流程：

    1. 如果 ``generated_content`` 被禁用，直接返回 fallback。
    2. 根据 ``target.mode`` 选择生成策略：
       - ``"template"``：使用 ``_render_template`` 渲染正文模板。
       - ``"agent"``：调用 AI agent 生成内容。
    3. 验证输出是否满足 ``_validate_prd_output``。
    4. 验证通过则返回生成结果，source 标记为 ``target.mode``。
    5. 如果 agent 模式失败且 ``config.fallback == "template"``，尝试 template 兜底。
    6. 最终仍失败则返回 ``fallback_prd_text``，source 标记为 ``"fallback"``。

    Args:
        config: 生成内容配置。
        context: PRD 上下文。
        fallback_prd_text: 当所有生成方式失败时使用的 PRD 文本。
        generator: agent 模式所需的内容生成器。
        cwd: agent 工作目录。
        prd_skill_path: 可选的 ``prd`` skill ``SKILL.md`` 显式路径；为 ``None`` 时按
            :func:`resolve_prd_skill_path` 解析。agent 模式优先用 skill 规范构建
            prompt（单一来源），skill 不可达时回退到配置的 ``target.prompt`` 模板。

    Returns:
        包含 text 和 source 的 ``GeneratedPrdContent`` 实例。
    """
    target = config.prd_from_issue
    if not config.enabled or not target.enabled:
        return GeneratedPrdContent(text=fallback_prd_text, source="fallback")

    generated_text = ""

    if target.mode == "template" and target.body_template:
        try:
            generated_text = _render_template(target.body_template, context)
        except (KeyError, ValueError):
            pass
    elif target.mode == "agent" and generator is not None and cwd is not None:
        agent_name = _resolve_generation_agent(target.agent, config.default_agent)
        # PRD 规范单一来源：优先注入 prd skill 规范；不可达时回退到配置模板 prompt。
        skill_spec = load_prd_skill_spec(prd_skill_path)
        if skill_spec:
            prompt = _build_prd_agent_prompt(
                skill_spec, context, config.max_input_chars
            )
        else:
            prompt = _truncate_text(
                _render_template(target.prompt, context), config.max_input_chars
            )
        generated_text = _run_content_generator(
            generator, agent_name, prompt, cwd, target.timeout_seconds
        )

    if generated_text and _validate_prd_output(generated_text):
        return GeneratedPrdContent(text=generated_text, source=target.mode)

    # Agent 失败：按配置尝试 template 中间兜底。
    if (
        target.mode == "agent"
        and config.fallback == "template"
        and target.body_template
    ):
        try:
            generated_text = _render_template(target.body_template, context)
        except (KeyError, ValueError):
            pass
        if generated_text and _validate_prd_output(generated_text):
            return GeneratedPrdContent(text=generated_text, source="template")

    return GeneratedPrdContent(text=fallback_prd_text, source="fallback")


# ---------------------------------------------------------------------------
# Issue 内容生成
# ---------------------------------------------------------------------------


def generate_issue_content(
    *,
    config: GeneratedContentConfig,
    context: IssueContext,
    fallback_title: str,
    fallback_body: str,
    generator: IContentGenerator | None = None,
    cwd: Path | None = None,
) -> GeneratedIssueContent:
    """生成 Issue 标题和正文，支持多级回退。

    执行流程：

    1. 如果 ``generated_content`` 被禁用，直接返回 fallback。
    2. 根据 ``target.mode`` 选择生成策略：
       - ``"template"``：使用 ``_try_render_templates`` 渲染模板。
       - ``"agent"``：调用 AI agent，然后根据 ``target.output`` 解析 JSON 或 Markdown。
    3. 截断标题和正文至安全长度，验证正文是否包含 ``- PRD path:`` 锚点。
    4. 验证通过则返回生成结果，source 标记为 ``target.mode``。
    5. 如果 agent 模式失败且 ``config.fallback == "template"``，尝试 template 兜底。
    6. 最终仍失败则返回 ``fallback_title`` / ``fallback_body``，source 标记为 ``"fallback"``。

    Args:
        config: 生成内容配置，包含 mode、fallback 策略、max_input_chars 等。
        context: Issue 上下文，提供模板/agent 所需的变量。
        fallback_title: 当所有生成方式失败时使用的标题。
        fallback_body: 当所有生成方式失败时使用的正文。
        generator: agent 模式所需的内容生成器。template 模式可为 ``None``。
        cwd: agent 工作目录。agent 模式必需。

    Returns:
        包含 title、body 和 source 的 ``GeneratedIssueContent`` 实例。
    """
    target = config.issue_from_prd
    if not config.enabled or not target.enabled:
        return GeneratedIssueContent(
            title=fallback_title, body=fallback_body, source="fallback"
        )

    generated_title = ""
    generated_body = ""

    # 根据配置模式选择生成策略。
    if target.mode == "template":
        generated_title, generated_body = _try_render_templates(target, context)
    elif target.mode == "agent" and generator is not None and cwd is not None:
        # 解析 agent：显式优先 → 全局默认 → 两者皆 auto 时收敛到 claude。
        agent_name = _resolve_generation_agent(target.agent, config.default_agent)
        # 渲染 prompt 模板并截断，防止超出模型上下文限制。
        prompt = _render_template(target.prompt, context)
        prompt = _truncate_text(prompt, config.max_input_chars)
        output_text = _run_content_generator(
            generator, agent_name, prompt, cwd, target.timeout_seconds
        )
        # 根据配置的输出格式解析 agent 返回内容。
        if target.output == "json":
            generated_title, generated_body = _parse_json_output(output_text)
        else:
            generated_title, generated_body = _parse_markdown_output(output_text)

    # 截断至安全长度。
    if generated_title:
        generated_title = generated_title[:_MAX_TITLE_LENGTH]
    if generated_body:
        generated_body = generated_body[: config.max_input_chars]

    # 验证并返回主模式结果。
    if (
        generated_title
        and generated_body
        and _validate_issue_body(generated_body, context.relative_prd_path)
    ):
        return GeneratedIssueContent(
            title=generated_title, body=generated_body, source=target.mode
        )

    # Agent 失败：按配置尝试 template 中间兜底。
    if target.mode == "agent" and config.fallback == "template":
        generated_title, generated_body = _try_render_templates(target, context)
        if generated_title:
            generated_title = generated_title[:_MAX_TITLE_LENGTH]
        if generated_body:
            generated_body = generated_body[: config.max_input_chars]
        if (
            generated_title
            and generated_body
            and _validate_issue_body(generated_body, context.relative_prd_path)
        ):
            return GeneratedIssueContent(
                title=generated_title, body=generated_body, source="template"
            )

    # 所有生成方式均失败，返回 hard fallback。
    return GeneratedIssueContent(
        title=fallback_title, body=fallback_body, source="fallback"
    )


# ---------------------------------------------------------------------------
# Git 信息收集（用于 PR 内容生成）
# ---------------------------------------------------------------------------


def _get_commit_log(
    worktree_path: Path,
    base_branch: str,
    process_runner: IProcessRunner,
) -> str:
    """获取从 ``base_branch`` 到 HEAD 的提交信息列表。

    使用 ``git log {base_branch}..HEAD --pretty=format:%s`` 命令，
    仅提取提交的 subject 行。

    Args:
        worktree_path: 工作树目录。
        base_branch: 基础分支名称。
        process_runner: 执行 Git 命令的 runner。

    Returns:
        以换行符分隔的提交 subject 列表。无提交时返回空字符串。
    """
    result = process_runner.run(
        ["git", "log", f"{base_branch}..HEAD", "--pretty=format:%s"],
        cwd=worktree_path,
        check=False,
    )
    return result.stdout.strip()


def _get_diff_stat(
    worktree_path: Path,
    base_branch: str,
    process_runner: IProcessRunner,
) -> str:
    """获取从 ``base_branch`` 到 HEAD 的 diff 统计信息。

    使用 ``git diff --stat {base_branch}...HEAD`` 命令，
    显示每个变更文件的增删行数统计。

    Args:
        worktree_path: 工作树目录。
        base_branch: 基础分支名称。
        process_runner: 执行 Git 命令的 runner。

    Returns:
        ``git diff --stat`` 的输出文本。无变更时返回空字符串。
    """
    result = process_runner.run(
        ["git", "diff", "--stat", f"{base_branch}...HEAD"],
        cwd=worktree_path,
        check=False,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# PR 上下文构建
# ---------------------------------------------------------------------------


def build_pr_context(
    *,
    issue: object,
    branch: str,
    base_branch: str,
    worktree_path: Path,
    process_runner: IProcessRunner,
    target_config: GeneratedContentTargetConfig,
) -> PrContext:
    """为 PR 内容生成构建上下文变量。

    根据 ``target_config`` 中的开关（``include_commit_log``、
    ``include_diff_stat``）决定是否执行相应的 Git 命令收集信息。
    这些 Git 操作可能耗时，因此仅在配置要求时才执行。

    Args:
        issue: 关联的 Issue 对象，需具有 ``number``、``title``、``body`` 属性。
        branch: 当前工作分支。
        base_branch: 基础分支。
        worktree_path: 工作树目录。
        process_runner: 执行 Git 命令的 runner。
        target_config: 生成目标配置，控制是否收集 commit log 和 diff stat。

    Returns:
        供模板渲染或 agent prompt 使用的 ``PrContext`` 实例。
    """
    commit_log = ""
    diff_stat = ""
    if target_config.include_commit_log:
        commit_log = _get_commit_log(worktree_path, base_branch, process_runner)
    if target_config.include_diff_stat:
        diff_stat = _get_diff_stat(worktree_path, base_branch, process_runner)
    return PrContext(
        issue_number=issue.number,
        issue_title=issue.title,
        issue_body=issue.body,
        branch=branch,
        base_branch=base_branch,
        commit_log=commit_log,
        commit_messages=commit_log,
        diff_stat=diff_stat,
        git_diff_stat=diff_stat,
    )


# ---------------------------------------------------------------------------
# PR 内容生成
# ---------------------------------------------------------------------------


def generate_pr_content(
    *,
    config: GeneratedContentConfig,
    context: PrContext,
    fallback_title: str,
    fallback_body: str,
    generator: IContentGenerator | None = None,
    cwd: Path | None = None,
) -> GeneratedPrContent:
    """生成 PR 标题和正文，支持多级回退。

    逻辑与 ``generate_issue_content`` 基本一致，区别：

    - 使用 ``config.draft_pr`` 而非 ``config.issue_from_prd`` 作为目标配置。
    - 正文校验使用 ``_validate_pr_body``（检查 ``Closes #{issue_number}``）。
    - title 允许为空，验证失败时会回退到 ``fallback_title``。

    Args:
        config: 生成内容配置。
        context: PR 上下文。
        fallback_title: 当所有生成方式失败时使用的标题。
        fallback_body: 当所有生成方式失败时使用的正文。
        generator: agent 模式所需的内容生成器。
        cwd: agent 工作目录。

    Returns:
        包含 title、body 和 source 的 ``GeneratedPrContent`` 实例。
    """
    target = config.draft_pr
    if not config.enabled or not target.enabled:
        return GeneratedPrContent(
            title=fallback_title, body=fallback_body, source="fallback"
        )

    generated_title = ""
    generated_body = ""

    if target.mode == "template":
        generated_title, generated_body = _try_render_templates(target, context)
    elif target.mode == "agent" and generator is not None and cwd is not None:
        agent_name = _resolve_generation_agent(target.agent, config.default_agent)
        prompt = _render_template(target.prompt, context)
        prompt = _truncate_text(prompt, config.max_input_chars)
        output_text = _run_content_generator(
            generator, agent_name, prompt, cwd, target.timeout_seconds
        )
        if target.output == "json":
            generated_title, generated_body = _parse_json_output(output_text)
        else:
            generated_title, generated_body = _parse_markdown_output(output_text)

    if generated_title:
        generated_title = generated_title[:_MAX_TITLE_LENGTH]
    if generated_body:
        generated_body = generated_body[: config.max_input_chars]

    if generated_body and _validate_pr_body(generated_body, context.issue_number):
        return GeneratedPrContent(
            title=generated_title or fallback_title,
            body=generated_body,
            source=target.mode,
        )

    # Agent 失败：按配置尝试 template 中间兜底。
    if target.mode == "agent" and config.fallback == "template":
        generated_title, generated_body = _try_render_templates(target, context)
        if generated_title:
            generated_title = generated_title[:_MAX_TITLE_LENGTH]
        if generated_body:
            generated_body = generated_body[: config.max_input_chars]
        if generated_body and _validate_pr_body(generated_body, context.issue_number):
            return GeneratedPrContent(
                title=generated_title or fallback_title,
                body=generated_body,
                source="template",
            )

    return GeneratedPrContent(
        title=fallback_title, body=fallback_body, source="fallback"
    )
