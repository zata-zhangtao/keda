"""Repository-local configuration helpers for issue-agent-runner."""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from backend.engines.agent_runner.repository_gitignore import (  # noqa: F401
    GITIGNORE_BLOCK_FOOTER,
    GITIGNORE_BLOCK_HEADER,
    GitignoreSyncOptions,
    GitignoreSyncResult,
    IAR_GITIGNORE_SECTIONS,
    ensure_gitignore_entries,
)
from backend.infrastructure.config.settings import (
    AgentRunnerDeliberationSettings,
    AgentRunnerGeneratedContentSettings,
    AgentRunnerGitSettings,
    AgentRunnerInteractiveDecisionSettings,
    AgentRunnerLocalSettings,
    AgentRunnerPostPrSupervisorSettings,
    AgentRunnerPrePrReviewSettings,
    AgentRunnerPromptSettings,
    AgentRunnerRepositoryMetadataSettings,
    AgentRunnerRunnerSettings,
    AgentRunnerSafetySettings,
    AgentRunnerValidationSettings,
    AgentRunnerWorktreeSettings,
    IAR_REPOSITORY_CONFIG_FILENAME,
    load_agent_runner_local_settings,
)
from backend.core.shared.interfaces.runner_console import (
    DiscoveredRepositoryEntry,
    IRepositoryRegistryEditor,
)
from backend.infrastructure.process_runner import CommandResult, SubprocessRunner


_logger = logging.getLogger(__name__)

_MAX_SCAN_DEPTH = 4


class IARRepositoryNotInitializedError(Exception):
    """Raised when a target repository has not run `iar init`."""

    def __init__(self, repo_root_path: Path, config_path: Path) -> None:
        self.repo_root_path = repo_root_path
        self.config_path = config_path
        super().__init__(
            f"Repository '{repo_root_path}' is not initialized for iar. "
            f"Expected local config: {config_path}"
        )


def require_iar_repository_initialized(
    repo_root_path: Path,
    process_runner: SubprocessRunner | None = None,  # noqa: ARG001
) -> None:
    """Raise if the repository lacks a valid .iar.toml.

    A valid local config means:
    - `.iar.toml` exists as a regular file.
    - It is parseable TOML.
    - It contains an `[agent_runner]` section.
    - `repository.id` is non-empty.

    Args:
        repo_root_path: Target Git repository root path.
        process_runner: Optional subprocess runner (unused, kept for API
            symmetry with other repository-local helpers).

    Raises:
        IARRepositoryNotInitializedError: If the repository is not initialized.
    """
    config_path = repo_root_path / IAR_REPOSITORY_CONFIG_FILENAME
    try:
        local_settings = load_agent_runner_local_settings(repo_root_path)
    except ValueError as exc:
        raise IARRepositoryNotInitializedError(repo_root_path, config_path) from exc

    if local_settings is None or not local_settings.id:
        raise IARRepositoryNotInitializedError(repo_root_path, config_path)


@dataclass(frozen=True)
class RepositoryInitOptions:
    """Options for creating repository-local IAR configuration."""

    cwd: Path
    repo_id_override: str | None = None
    display_name_override: str | None = None
    remote_override: str | None = None
    base_branch_override: str | None = None
    dry_run: bool = False
    force: bool = False


@dataclass(frozen=True)
class RepositoryInitResult:
    """Result of rendering or writing repository-local IAR configuration."""

    repo_root_path: Path
    config_path: Path
    config_text: str
    wrote_file: bool
    repo_id: str | None = None
    display_name: str | None = None
    verification_commands: list[str] = field(default_factory=list)


def detect_git_repository_root(
    start_path: Path,
    process_runner: SubprocessRunner | None = None,
) -> Path:
    """Detect the Git repository root containing a path.

    Args:
        start_path: Directory or file path to inspect.
        process_runner: Optional subprocess runner.

    Returns:
        Resolved Git repository root path.

    Raises:
        ValueError: If the path does not exist or is outside a Git repository.
    """
    resolved_start_path = start_path.resolve()
    if not resolved_start_path.exists():
        raise ValueError(f"Path '{resolved_start_path}' does not exist.")

    cwd_path = resolved_start_path if resolved_start_path.is_dir() else resolved_start_path.parent
    git_result = _run_git(["rev-parse", "--show-toplevel"], cwd_path, process_runner)
    git_root_text = git_result.stdout.strip()
    if git_result.return_code != 0 or not git_root_text:
        raise ValueError(
            f"Path '{resolved_start_path}' is not inside a Git repository. "
            "Run iar from a target repository or pass --repo/--repo-id."
        )
    return Path(git_root_text).resolve()


TOML_HEADER_COMMENT = """# IAR 本地仓库配置
# 本文件只覆盖当前仓库特有的配置；未指定的字段继承 config.toml / 环境变量的全局默认值。
# 修改后无需重启 daemon，下一次轮询自动生效。
# 完整字段说明见 docs/guides/agent-runner.md。
"""

# 生成文件中顶级 section 的展示顺序。
_IAR_SECTION_ORDER = (
    "repository",
    "git",
    "worktree",
    "runner",
    "safety",
    "validation",
    "prompts",
    "pre_pr_review",
    "post_pr_supervisor",
    "generated_content",
    "interactive_decision",
    "deliberation",
)

# section -> 段前说明注释（中文为主，关键术语保留英文）。
_IAR_SECTION_COMMENTS: dict[str, str] = {
    "repository": "仓库身份标识（用于多仓库管理时区分不同仓库）",
    "git": "Git 发布配置：推送 remote、目标基础分支 base_branch",
    "worktree": "Issue worktree 的创建与定位命令；默认使用 iar worktree，通常无需修改",
    "runner": "Runner 行为配置：每轮处理 Issue 数量、默认 agent、提交前验证命令",
    "safety": "发布前安全边界：自动合并开关、禁止提交的路径模式",
    "validation": "Realistic Validation 证据门禁配置",
    "prompts": "实现 Agent 的 prompt 模板；默认 phase 与自定义阶段模板",
    "pre_pr_review": "Draft PR 创建前的 AI review 门禁（push 之后、PR 之前）",
    "post_pr_supervisor": "Draft PR 创建后的自动 supervisor 配置",
    "generated_content": "GitHub Issue / PR 内容生成（面向人类阅读，不影响实现 Agent）",
    "interactive_decision": "交互式决策（iar ask）配置：默认 agent、输出目录、执行确认等",
    "deliberation": "多 agent 审议（iar deliberate）配置：轮数、合成 agent、参与角色",
}

# 子表路径 -> 子表说明注释。
_IAR_SUBTABLE_COMMENTS: dict[str, str] = {
    "generated_content.issue_from_prd": "从 PRD 生成 GitHub Issue 的模板",
    "generated_content.draft_pr": "从 commit 信息生成 Draft PR 的模板",
    "generated_content.prd_from_issue": "从 GitHub Issue 生成 / 重写 PRD（rework-prd 流程）",
    "deliberation.profiles.architect": "审议角色：架构师，关注系统设计与可维护性",
    "deliberation.profiles.skeptic": "审议角色：质疑者，挑战假设与风险",
    "deliberation.profiles.implementer": "审议角色：实现者，关注可行性与落地步骤",
}

# 字段路径 -> 字段说明注释（中文为主，关键术语保留英文）。
_IAR_FIELD_COMMENTS: dict[str, str] = {
    "repository.id": "仓库在 IAR 中的唯一标识，通常与远程仓库名一致",
    "repository.enabled": "是否允许 runner 处理该仓库的 Issue",
    "repository.display_name": "管理终端 / 日志中显示的友好名称",
    "git.remote": "推送分支和创建 PR 时使用的 Git remote 名称",
    "git.base_branch": "创建 worktree 与 PR 的目标基础分支",
    "worktree.create_command": "创建新 worktree 的命令；{issue_number} 和 {base_branch} 会被替换",
    "worktree.reuse_command": "复用已有 worktree 时定位路径的命令",
    "worktree.path_command": "获取 worktree 绝对路径的命令",
    "runner.max_issues": "每次轮询每个仓库最多处理多少个 Issue",
    "runner.max_concurrent_issues": (
        "单轮内并行处理的 Issue 数量：1 为串行（默认）；>1 时同一轮并行跑多个 "
        "Issue，仅 iar daemon --concurrency 未指定时作为默认值"
    ),
    "runner.default_agent": "默认使用的 AI agent：auto / claude / codex / kimi",
    "runner.max_recovery_attempts": "Agent 失败后的最大重试次数",
    "runner.recovery_retry_delay_seconds": "每次重试前等待的秒数",
    "runner.agent_fallback_order": (
        "跨 agent fallback 链：主 agent 失败后依次尝试本机可用 agent（如 "
        '["claude", "kimi", "codex"]），命令不存在则自动跳过；设为空列表可关闭切换'
    ),
    "runner.max_agent_switches": (
        "最多切换 agent 次数（order=[a,b,c] 且此值=2 时最多尝试 3 个 agent）"
    ),
    "runner.transient_retry_attempts": ("瞬时网络错误（socket 断开 / 5xx / 超时）的就地重试次数"),
    "runner.transient_retry_delay_seconds": "瞬时错误每次重试前等待的秒数",
    "runner.verification_commands": "提交前自动运行的验证命令；任一命令失败会进入 recovery",
    "safety.auto_merge": "是否允许自动合并 PR（强烈建议保持 false）",
    "safety.forbidden_path_patterns": "提交前禁止变更的路径通配模式",
    "validation.enabled": "是否启用 Realistic Validation 证据门禁",
    "validation.evidence_dir": "worktree 内证据目录（默认被 info/exclude 排除，不会进入代码 diff）",
    "validation.branch_prefix": "orphan 证据分支前缀",
    "validation.evidence_format_check": "是否逐项检查证据文件格式",
    "validation.parse_evidence_format_with_agent": "是否用 agent 解析 PRD 中的格式要求",
    "validation.language": "证据 prompt 与 PR 评论的固定标签语言，如 zh-CN / en-US",
    "validation.structured_evidence": "是否要求带 iar:structured-evidence marker 的 Issue 提供 evidence.json manifest",
    "validation.require_negative_control": "是否要求每个结构化证据项提供 negative_control（红→绿判别力,默认开;关掉则回退旧行为）",
    "validation.reexecute_commands": "是否由 keda 复跑每个结构化证据项的 command 以确认其真的通过（默认开;关掉则只信 agent 证据文件）",
    "validation.reexecute_timeout_seconds": "keda 复跑单条命令的超时秒数（默认 300;超时判失败,命令须为自终止的检查）",
    "validation.reexecute_cache_enabled": "命中代码树指纹(HEAD^{tree})时跳过该项 RV 命令复跑,避免 blocked-continue/换 agent 重复跑（默认开;工作区脏则不缓存、照常复跑）",
    "validation.verifier_enabled": "是否启用独立 verifier agent 复验（默认关;开启后在开 PR 前换一个 agent 对抗复验,red 自动打回 builder）",
    "validation.verifier_agent": "verifier 用哪个 agent（auto=自动挑一个≠builder 的）",
    "validation.verifier_timeout_seconds": "verifier agent 运行超时秒数（默认 1800）",
    "prompts.default_phase": "默认使用的 prompt 阶段",
    "prompts.phases": "自定义阶段模板，值为字符串或字符串列表",
    "pre_pr_review.enabled": "是否启用 Draft PR 创建前的 AI review",
    "pre_pr_review.review_agent": "执行 review 的 agent：auto / claude / codex / kimi",
    "pre_pr_review.allow_same_agent": "是否允许实现 agent 与 reviewer 为同一个",
    "pre_pr_review.max_attempts": "review 不通过时的最大修复轮数",
    "pre_pr_review.timeout_seconds": "review agent 最长运行秒数",
    "pre_pr_review.commit_request_reminder_attempts": "reviewer 报出问题但未写 commit request 时，同一轮内追加提醒的最大次数",
    "post_pr_supervisor.enabled": "是否启用 post-PR supervisor",
    "post_pr_supervisor.supervisor_agent": "执行 supervisor 的 agent",
    "post_pr_supervisor.max_repair_attempts": "supervisor 要求修复时的最大修复 / rebase 次数",
    "post_pr_supervisor.max_agent_crash_retries": "supervisor agent 进程崩溃（API / 网络等基础设施错误）时同一 cycle 内的最大重试次数",
    "post_pr_supervisor.crash_retry_initial_backoff_seconds": "崩溃重试的初始退避秒数，之后每次重试翻倍",
    "post_pr_supervisor.crash_retry_max_backoff_seconds": "崩溃重试单次退避等待的最大秒数",
    "generated_content.enabled": "是否启用 AI 生成 Issue / PR 正文",
    "generated_content.fallback": "生成失败时的回退方式（当前仅支持 template）",
    "generated_content.max_input_chars": "生成 prompt 的最大字符数",
    "generated_content.default_agent": "执行生成的默认 agent：auto / claude / codex / kimi",
    "generated_content.issue_from_prd.enabled": "是否从 PRD 生成 Issue",
    "generated_content.issue_from_prd.mode": "生成模式：template（模板渲染）或 agent（调用 AI）",
    "generated_content.issue_from_prd.output": "输出格式：json / markdown",
    "generated_content.issue_from_prd.title_template": "Issue 标题模板",
    "generated_content.issue_from_prd.body_template": "Issue 正文模板，支持字符串或字符串列表",
    "generated_content.issue_from_prd.agent": "执行生成的 agent",
    "generated_content.issue_from_prd.timeout_seconds": "生成超时秒数",
    "generated_content.issue_from_prd.prompt": "agent 模式使用的 prompt",
    "generated_content.issue_from_prd.include_commit_log": "PR 生成时是否包含 commit log",
    "generated_content.issue_from_prd.include_diff_stat": "PR 生成时是否包含 diff stat",
    "generated_content.draft_pr.enabled": "是否生成 Draft PR 正文",
    "generated_content.draft_pr.mode": "生成模式：template（模板渲染）或 agent（调用 AI）",
    "generated_content.draft_pr.output": "输出格式：json / markdown",
    "generated_content.draft_pr.title_template": "PR 标题模板",
    "generated_content.draft_pr.body_template": "PR 正文模板，支持字符串或字符串列表",
    "generated_content.draft_pr.agent": "执行生成的 agent",
    "generated_content.draft_pr.timeout_seconds": "生成超时秒数",
    "generated_content.draft_pr.prompt": "agent 模式使用的 prompt",
    "generated_content.draft_pr.include_commit_log": "PR 生成时是否包含 commit log",
    "generated_content.draft_pr.include_diff_stat": "PR 生成时是否包含 diff stat",
    "generated_content.prd_from_issue.enabled": "是否从 Issue 生成 / 重写 PRD",
    "generated_content.prd_from_issue.mode": "生成模式：agent（调用 AI，推荐）或 template；PRD 无内置模板，template 会退回 fallback",
    "generated_content.prd_from_issue.output": "输出格式：json / markdown",
    "generated_content.prd_from_issue.title_template": "PRD 标题模板（template 模式用）",
    "generated_content.prd_from_issue.body_template": "PRD 正文模板（template 模式用；留空则退回 fallback）",
    "generated_content.prd_from_issue.agent": "执行生成的 agent",
    "generated_content.prd_from_issue.timeout_seconds": "生成超时秒数",
    "generated_content.prd_from_issue.prompt": "agent 模式 prompt（留空则用内置 prd skill 规范）",
    "generated_content.prd_from_issue.include_commit_log": "（PRD 生成未使用）",
    "generated_content.prd_from_issue.include_diff_stat": "（PRD 生成未使用）",
    "interactive_decision.enabled": "是否启用 iar ask 交互式决策",
    "interactive_decision.default_agent": "iar ask 默认使用的 agent：auto / claude / codex / kimi",
    "interactive_decision.default_output_dir": "决策日志与审计文件的默认输出目录",
    "interactive_decision.planner_timeout_seconds": "规划 agent 的最长运行秒数",
    "interactive_decision.max_context_chars": "输入 prompt 的最大字符数",
    "interactive_decision.allow_execute_yes": "是否允许 `iar ask --yes` 跳过人工确认",
    "deliberation.default_rounds": "默认审议轮数",
    "deliberation.default_synthesizer": "汇总最终结论的默认 agent",
    "deliberation.default_output_dir": "审议会话输出目录",
    "deliberation.profiles.architect.agent": "该角色使用的 agent",
    "deliberation.profiles.architect.role": "角色标识",
    "deliberation.profiles.architect.behavior_prompt": "角色行为 prompt",
    "deliberation.profiles.skeptic.agent": "该角色使用的 agent",
    "deliberation.profiles.skeptic.role": "角色标识",
    "deliberation.profiles.skeptic.behavior_prompt": "角色行为 prompt",
    "deliberation.profiles.implementer.agent": "该角色使用的 agent",
    "deliberation.profiles.implementer.role": "角色标识",
    "deliberation.profiles.implementer.behavior_prompt": "角色行为 prompt",
}

# 注释掉的常用覆盖示例：[agent_runner.labels]。
_IAR_LABELS_EXAMPLE = """
# GitHub labels 状态流转配置（如你的仓库使用不同标签名，可取消注释并覆盖）
# [agent_runner.labels]
# ready = "agent/ready"
# running = "agent/running"
# supervising = "agent/supervising"
# review = "agent/review"
# failed = "agent/failed"
# blocked = "agent/blocked"
# codex = "agent/codex"
# claude = "agent/claude"
# kimi = "agent/kimi"
"""


def settings_to_toml_string(settings: AgentRunnerLocalSettings) -> str:
    """Serialize AgentRunnerLocalSettings to a commented .iar.toml string."""

    data = _filter_none_dict(settings.model_dump())
    # Wrap in [agent_runner] to match .iar.toml structure
    wrapped = {"agent_runner": data}
    toml_body = tomli_w.dumps(wrapped)
    annotated_body = _annotate_iar_toml(toml_body)
    return TOML_HEADER_COMMENT + "\n" + annotated_body + _IAR_LABELS_EXAMPLE


def _annotate_iar_toml(toml_body: str) -> str:
    """Inject Chinese comments and reorder sections for readability.

    tomli_w does not preserve comments, so we post-process the generated body.
    """
    section_blocks = _split_top_level_sections(toml_body)

    ordered_blocks: list[tuple[str, list[str]]] = []
    for section_name in _IAR_SECTION_ORDER:
        if section_name in section_blocks:
            ordered_blocks.append((section_name, section_blocks[section_name]))
    for section_name, block in section_blocks.items():
        if section_name not in _IAR_SECTION_ORDER:
            ordered_blocks.append((section_name, block))

    output_lines: list[str] = []
    for section_name, block_lines in ordered_blocks:
        while block_lines and block_lines[-1] == "":
            block_lines.pop()
        if output_lines and output_lines[-1] != "":
            output_lines.append("")
        section_comment = _IAR_SECTION_COMMENTS.get(section_name)
        if section_comment:
            output_lines.append(f"# {section_comment}")

        current_path = section_name
        for line in block_lines:
            subtable_match = re.match(r"^\[agent_runner\.(.+)\]\s*$", line)
            if subtable_match:
                current_path = subtable_match.group(1)
                sub_comment = _IAR_SUBTABLE_COMMENTS.get(current_path)
                if sub_comment:
                    output_lines.append(f"# {sub_comment}")
                output_lines.append(line)
                continue

            key_match = re.match(r"^([A-Za-z0-9_]+)\s*=", line)
            if key_match:
                dotted_key = f"{current_path}.{key_match.group(1)}"
                field_comment = _IAR_FIELD_COMMENTS.get(dotted_key)
                if field_comment:
                    output_lines.append(f"# {field_comment}")
            output_lines.append(line)

    return "\n".join(output_lines) + "\n"


def _split_top_level_sections(toml_body: str) -> dict[str, list[str]]:
    """Split TOML body into top-level [agent_runner.<name>] blocks.

    Nested tables such as [agent_runner.generated_content.issue_from_prd] are
    kept inside the ``generated_content`` block.
    """
    blocks: dict[str, list[str]] = {}
    current_name: str | None = None

    for raw_line in toml_body.splitlines():
        line = raw_line.rstrip()
        header_match = re.match(r"^\[agent_runner\.([A-Za-z0-9_]+)\]\s*$", line)
        if header_match:
            current_name = header_match.group(1)
            blocks.setdefault(current_name, []).append(line)
            continue
        if current_name is not None:
            blocks[current_name].append(line)

    return blocks


def _filter_none_dict(data: dict) -> dict:
    """Recursively remove None values from dict for TOML serialization."""

    def filter_value(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, dict):
            return filter_dict(v)
        if isinstance(v, list):
            return [filter_value(item) for item in v]
        return v

    def filter_dict(d: dict) -> dict:
        result = {k: filter_value(v) for k, v in d.items() if v is not None}
        return result if result else {}

    return filter_dict(data)


def _dependency_name_matches(dependency_spec: str, package_name: str) -> bool:
    """Check whether a PEP 508 dependency spec names the given package."""
    spec_package_name = re.split(r"[<>=!~\[ ;]", dependency_spec.strip(), maxsplit=1)[0]
    return spec_package_name.replace("_", "-").lower() == package_name


def _uv_dependency_flag(pyproject_data: dict[str, Any], package_name: str) -> str | None:
    """Return the ``uv run`` flag needed to reach a declared dependency.

    Returns ``""`` when the package is in the main dependencies or in the
    default ``dev`` dependency group (both installed by ``uv run`` without
    extra flags), ``" --group <name>"`` / ``" --extra <name>"`` when it lives
    in a non-default group or an optional-dependencies extra, and ``None``
    when the package is not declared at all.
    """
    project_table = pyproject_data.get("project") or {}
    for dependency_spec in project_table.get("dependencies") or []:
        if _dependency_name_matches(dependency_spec, package_name):
            return ""
    dependency_groups = pyproject_data.get("dependency-groups") or {}
    for group_name, group_entries in dependency_groups.items():
        group_specs = [entry for entry in group_entries if isinstance(entry, str)]
        if any(_dependency_name_matches(spec, package_name) for spec in group_specs):
            return "" if group_name == "dev" else f" --group {group_name}"
    optional_extras = project_table.get("optional-dependencies") or {}
    for extra_name, extra_specs in optional_extras.items():
        if any(_dependency_name_matches(spec, package_name) for spec in extra_specs):
            return f" --extra {extra_name}"
    return None


# Recipe header at column zero, optionally prefixed by ``@`` (quiet recipe),
# e.g. ``test:``, ``@test:`` or ``test *args:``; the ``:(?!=)`` lookahead skips
# ``test := ...`` variable assignments and ``\btest\b`` skips ``test_setup:``.
_JUST_TEST_RECIPE_PATTERN = re.compile(r"^@?test\b[^\n]*:(?!=)", re.MULTILINE)
# Justfile ``import`` / ``import?`` / ``!include`` directive with the (optionally
# quoted) target path captured, so a ``test`` recipe living in an imported file
# (such as a shared ``justfile.shared`` template) is still discovered.
_JUST_IMPORT_PATTERN = re.compile(
    r"""^\s*(?:import\??|!include)\s+['"]?([^'"\n]+?)['"]?\s*$""",
    re.MULTILINE,
)
_MAX_JUSTFILE_IMPORT_DEPTH = 8


def _justfile_defines_test_recipe(
    justfile_path: Path,
    visited_paths: set[Path] | None = None,
    depth: int = 0,
) -> bool:
    """Return whether ``justfile_path`` or any file it imports defines ``test``.

    Follows ``import`` directives relative to each justfile's own directory,
    guarding against import cycles and runaway depth.
    """
    if visited_paths is None:
        visited_paths = set()
    if depth > _MAX_JUSTFILE_IMPORT_DEPTH:
        return False
    try:
        resolved_path = justfile_path.resolve()
    except OSError:
        return False
    if resolved_path in visited_paths:
        return False
    visited_paths.add(resolved_path)
    if not justfile_path.is_file():
        return False
    try:
        justfile_text = justfile_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if _JUST_TEST_RECIPE_PATTERN.search(justfile_text):
        return True
    for import_match in _JUST_IMPORT_PATTERN.finditer(justfile_text):
        imported_path = justfile_path.parent / import_match.group(1).strip()
        if _justfile_defines_test_recipe(imported_path, visited_paths, depth + 1):
            return True
    return False


def _has_just_test_recipe(repo_root_path: Path) -> bool:
    """Return whether a justfile in the repo defines a ``test`` recipe.

    Matches a recipe header such as ``test:``, ``@test:`` or ``test *args:`` at
    column zero (ignoring ``test := ...`` assignments) and follows ``import``
    directives so a ``test`` recipe in an imported ``justfile.shared`` is still
    detected. Used to align the generated verification command with the
    project's own aggregate gate.
    """
    for filename in ("justfile", "Justfile", ".justfile"):
        justfile_path = repo_root_path / filename
        if justfile_path.is_file() and _justfile_defines_test_recipe(justfile_path):
            return True
    return False


def _detect_precommit_verification_command(
    repo_root_path: Path, pyproject_data: dict[str, Any]
) -> str | None:
    """Return a generic ``pre-commit run`` verification command, or ``None``.

    Running the repository's own pre-commit hooks during verification surfaces
    lint/format failures while the runner can still recover, instead of letting
    them hard-fail at the commit step. Returns ``None`` when:

    - there is no ``.pre-commit-config.yaml`` (nothing to run);
    - ``pre-commit`` is not a declared dependency (``uv run pre-commit`` would
      fail); or
    - the config installs the ``check-test-flag`` gate. That hook only accepts a
      marker written by ``just test``; this helper is reached only when no
      ``just test`` recipe was detected, so a bare ``pre-commit run`` would
      deadlock on the stale marker. Skip it and let pytest stand alone.
    """
    precommit_config_path = repo_root_path / ".pre-commit-config.yaml"
    if not precommit_config_path.is_file():
        return None
    precommit_flag = _uv_dependency_flag(pyproject_data, "pre-commit")
    if precommit_flag is None:
        return None
    try:
        precommit_config_text = precommit_config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if "check-test-flag" in precommit_config_text:
        _logger.warning(
            "Repository at %s installs the check-test-flag pre-commit hook but "
            "has no detectable `just test` recipe to refresh its marker; "
            "omitting `pre-commit run` from verification_commands to avoid a "
            "commit deadlock. Add a `just test` recipe or remove check-test-flag.",
            repo_root_path,
        )
        return None
    return f"uv run{precommit_flag} pre-commit run --all-files"


def detect_verification_commands(repo_root_path: Path) -> list[str]:
    """Detect verification commands that actually run in the target repository.

    ``iar init`` previously copied this tool's own defaults (such as
    ``uv run mkdocs build``) into every repository, which fails in any project
    that does not install mkdocs by default. Detection keeps the safe
    ``git diff --check`` baseline and adds tool commands only when the target
    repository declares the tool in ``pyproject.toml``, using the ``uv run``
    invocation (``--extra`` / ``--group``) that matches where the dependency
    is declared.

    When the repository exposes a ``just test`` aggregate gate (directly or via
    an imported ``justfile.shared``) it is preferred, since it runs the same
    lint/format/test hooks pre-commit enforces at ``git commit``. Otherwise a
    generic ``pre-commit run --all-files`` (when safe) plus ``pytest -q`` keep
    the runner's verification aligned with the commit gate.
    """
    verification_commands = ["git diff --check"]
    pyproject_path = repo_root_path / "pyproject.toml"
    if not pyproject_path.is_file():
        return verification_commands
    try:
        with open(pyproject_path, "rb") as pyproject_file:
            pyproject_data: dict[str, Any] = tomllib.load(pyproject_file)
    except tomllib.TOMLDecodeError:
        return verification_commands

    if (repo_root_path / "mkdocs.yml").is_file():
        mkdocs_flag = _uv_dependency_flag(pyproject_data, "mkdocs")
        if mkdocs_flag is not None:
            verification_commands.append(f"uv run{mkdocs_flag} mkdocs build")

    has_tests = (repo_root_path / "tests").is_dir()
    if _has_just_test_recipe(repo_root_path):
        # ``just test`` is the project's aggregate gate: it runs the same
        # lint/format/test hooks pre-commit enforces at ``git commit`` (and
        # refreshes any just-test marker), so a commit cannot fail pre-commit
        # after the runner's verification already passed. It already covers
        # pre-commit, so no separate ``pre-commit run`` is added here.
        if has_tests:
            verification_commands.append("just test")
    else:
        # Generic fallback for repos without a ``just test`` gate: run the
        # project's own pre-commit hooks (so lint/format failures surface during
        # verification, not at commit time) plus pytest. A bare ``pytest -q``
        # alone misses ruff/format hooks, letting them hard-fail at commit time.
        precommit_command = _detect_precommit_verification_command(repo_root_path, pyproject_data)
        if precommit_command is not None:
            verification_commands.append(precommit_command)
        if has_tests:
            pytest_flag = _uv_dependency_flag(pyproject_data, "pytest")
            if pytest_flag is not None:
                verification_commands.append(f"uv run{pytest_flag} pytest -q")

    return verification_commands


def build_repository_local_config_text(
    options: RepositoryInitOptions,
    process_runner: SubprocessRunner | None = None,
) -> tuple[Path, str, list[str]]:
    """Render repository-local IAR TOML for a Git repository.

    Args:
        options: Init options, including cwd and explicit overrides.
        process_runner: Optional subprocess runner.

    Returns:
        A tuple of the detected repository root path, rendered TOML text, and
        the verification commands that were detected for the repository.
    """
    repo_root_path = detect_git_repository_root(options.cwd, process_runner)
    selected_remote = options.remote_override or _detect_default_remote(
        repo_root_path, process_runner
    )
    detected_repo_id = _detect_repository_id(repo_root_path, selected_remote, process_runner)
    selected_repo_id = options.repo_id_override or detected_repo_id
    selected_display_name = options.display_name_override or repo_root_path.name
    selected_base_branch = options.base_branch_override or _detect_default_base_branch(
        repo_root_path, selected_remote, process_runner
    )

    verification_commands = detect_verification_commands(repo_root_path)
    settings = AgentRunnerLocalSettings(
        repository=AgentRunnerRepositoryMetadataSettings(
            id=selected_repo_id,
            enabled=True,
            display_name=selected_display_name,
        ),
        git=AgentRunnerGitSettings(
            remote=selected_remote,
            base_branch=selected_base_branch,
        ),
        worktree=AgentRunnerWorktreeSettings(),
        runner=AgentRunnerRunnerSettings(verification_commands=verification_commands),
        safety=AgentRunnerSafetySettings(),
        validation=AgentRunnerValidationSettings(),
        prompts=AgentRunnerPromptSettings(),
        pre_pr_review=AgentRunnerPrePrReviewSettings(),
        post_pr_supervisor=AgentRunnerPostPrSupervisorSettings(),
        generated_content=AgentRunnerGeneratedContentSettings(),
        interactive_decision=AgentRunnerInteractiveDecisionSettings(),
        deliberation=AgentRunnerDeliberationSettings(),
    )

    return repo_root_path, settings_to_toml_string(settings), verification_commands


def initialize_repository_local_config(
    options: RepositoryInitOptions,
    process_runner: SubprocessRunner | None = None,
) -> RepositoryInitResult:
    """Render or write ``.iar.toml`` for the current Git repository.

    Args:
        options: Init options controlling overrides and write behavior.
        process_runner: Optional subprocess runner.

    Returns:
        Init result with generated TOML and write status.

    Raises:
        ValueError: If ``.iar.toml`` already exists and overwrite was not forced.
    """
    repo_root_path, config_text, verification_commands = build_repository_local_config_text(
        options, process_runner
    )
    config_path = repo_root_path / IAR_REPOSITORY_CONFIG_FILENAME
    selected_remote = options.remote_override or _detect_default_remote(
        repo_root_path, process_runner
    )
    detected_repo_id = _detect_repository_id(repo_root_path, selected_remote, process_runner)
    selected_repo_id = options.repo_id_override or detected_repo_id
    selected_display_name = options.display_name_override or repo_root_path.name

    def _make_result(wrote_file: bool) -> RepositoryInitResult:
        return RepositoryInitResult(
            repo_root_path=repo_root_path,
            config_path=config_path,
            config_text=config_text,
            wrote_file=wrote_file,
            repo_id=selected_repo_id,
            display_name=selected_display_name,
            verification_commands=verification_commands,
        )

    if config_path.exists() and not options.force and not options.dry_run:
        existing_text = config_path.read_text(encoding="utf-8")
        if existing_text != config_text:
            raise ValueError(
                f"IAR local config already exists at {config_path}. " "Use --force to overwrite it."
            )
        return _make_result(wrote_file=False)
    if options.dry_run:
        return _make_result(wrote_file=False)

    config_path.write_text(config_text, encoding="utf-8")
    return _make_result(wrote_file=True)


def _run_git(
    git_args: list[str],
    cwd_path: Path,
    process_runner: SubprocessRunner | None,
) -> CommandResult:
    runner = process_runner or SubprocessRunner()
    return runner.run(
        ["git", *git_args],
        cwd=cwd_path,
        check=False,
        capture_output=True,
    )


def _detect_default_remote(
    repo_root_path: Path,
    process_runner: SubprocessRunner | None,
) -> str:
    configured_remotes = _list_git_remotes(repo_root_path, process_runner)

    current_branch_result = _run_git(["branch", "--show-current"], repo_root_path, process_runner)
    current_branch = current_branch_result.stdout.strip()
    if current_branch:
        upstream_remote_result = _run_git(
            ["config", f"branch.{current_branch}.remote"],
            repo_root_path,
            process_runner,
        )
        upstream_remote = upstream_remote_result.stdout.strip()
        if upstream_remote and upstream_remote in configured_remotes:
            return upstream_remote

    if "origin" in configured_remotes:
        return "origin"
    if configured_remotes:
        return configured_remotes[0]
    return "origin"


def _list_git_remotes(
    repo_root_path: Path,
    process_runner: SubprocessRunner | None,
) -> list[str]:
    remotes_result = _run_git(["remote"], repo_root_path, process_runner)
    return [line.strip() for line in remotes_result.stdout.splitlines() if line.strip()]


def _detect_repository_id(
    repo_root_path: Path,
    remote_name: str,
    process_runner: SubprocessRunner | None,
) -> str:
    remote_url_result = _run_git(["remote", "get-url", remote_name], repo_root_path, process_runner)
    remote_url = remote_url_result.stdout.strip()
    if remote_url:
        remote_repo_name = _repository_name_from_remote_url(remote_url)
        if remote_repo_name:
            return normalize_repository_id(remote_repo_name)
    return normalize_repository_id(repo_root_path.name)


def _repository_name_from_remote_url(remote_url: str) -> str:
    trimmed_url = remote_url.rstrip("/")
    if trimmed_url.endswith(".git"):
        trimmed_url = trimmed_url[:-4]
    return re.split(r"[:/]", trimmed_url)[-1]


def normalize_repository_id(raw_repo_name: str) -> str:
    normalized_repo_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_repo_name)
    stripped_repo_name = normalized_repo_name.strip("-_.").lower()
    return stripped_repo_name or "repository"


def _detect_default_base_branch(
    repo_root_path: Path,
    remote_name: str,
    process_runner: SubprocessRunner | None,
) -> str:
    remote_candidates = tuple(dict.fromkeys((remote_name, "origin")))
    for candidate_remote in remote_candidates:
        remote_head_branch = _remote_head_branch(repo_root_path, candidate_remote, process_runner)
        if remote_head_branch:
            return remote_head_branch

    for branch_name in ("main", "master"):
        branch_result = _run_git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            repo_root_path,
            process_runner,
        )
        if branch_result.return_code == 0:
            return branch_name

    current_branch_result = _run_git(["branch", "--show-current"], repo_root_path, process_runner)
    current_branch = current_branch_result.stdout.strip()
    return current_branch or "main"


def _remote_head_branch(
    repo_root_path: Path,
    remote_name: str,
    process_runner: SubprocessRunner | None,
) -> str | None:
    remote_head_result = _run_git(
        ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote_name}/HEAD"],
        repo_root_path,
        process_runner,
    )
    remote_head_text = remote_head_result.stdout.strip()
    prefix = f"{remote_name}/"
    if remote_head_result.return_code == 0 and remote_head_text.startswith(prefix):
        return remote_head_text[len(prefix) :]
    return None


def discover_iar_repositories(
    *,
    scan_root: Path,
    editor: IRepositoryRegistryEditor,
) -> list[DiscoveredRepositoryEntry]:
    """扫描本地目录，发现已初始化 IAR 的 git 仓库。

    扫描 ``scan_root`` 下最多 ``_MAX_SCAN_DEPTH`` 层子目录，
    对每个同时存在 ``.git`` 与 ``.iar.toml`` 的目录，读取本地配置
    生成候选条目，并标记是否已注册到 registry。

    Args:
        scan_root: 扫描起始目录。
        editor: registry 读取端口，用于判断仓库是否已注册。

    Returns:
        按 repo_id 排序的候选仓库列表。

    Raises:
        ValueError: ``scan_root`` 不存在或不是目录。
    """
    resolved_root = scan_root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise ValueError(f"扫描目录不存在或不是目录：{resolved_root}")

    registered_paths = {
        str(Path(entry.path).expanduser().resolve()) for entry in editor.list_repositories()
    }

    discovered: dict[str, DiscoveredRepositoryEntry] = {}
    for directory in _walk_directories(resolved_root, _MAX_SCAN_DEPTH):
        if not _is_iar_git_repository(directory):
            continue
        local_settings = load_agent_runner_local_settings(directory)
        repo_id = (
            local_settings.id
            if local_settings and local_settings.id
            else normalize_repository_id(directory.name)
        )
        display_name = (
            local_settings.display_name
            if local_settings and local_settings.display_name
            else directory.name
        )
        resolved_path = str(directory)
        if repo_id in discovered:
            continue
        discovered[repo_id] = DiscoveredRepositoryEntry(
            repo_id=repo_id,
            path=resolved_path,
            display_name=display_name,
            already_registered=resolved_path in registered_paths,
        )

    return [discovered[repo_id] for repo_id in sorted(discovered)]


def _walk_directories(root: Path, max_depth: int):
    """按广度优先遍历目录，yield 目录路径。"""
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for child in current.iterdir():
            if not child.is_dir():
                continue
            yield child
            queue.append((child, depth + 1))


def _is_iar_git_repository(directory: Path) -> bool:
    """判断目录是否为带 IAR 配置的 git 仓库。"""
    return (directory / ".git").exists() and (directory / IAR_REPOSITORY_CONFIG_FILENAME).is_file()
