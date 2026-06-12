"""Repository-local configuration helpers for issue-agent-runner."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

from backend.infrastructure.config.settings import (
    AgentRunnerGeneratedContentSettings,
    AgentRunnerGitSettings,
    AgentRunnerLocalSettings,
    AgentRunnerPrePushReviewSettings,
    AgentRunnerPromptSettings,
    AgentRunnerRepositoryMetadataSettings,
    AgentRunnerRunnerSettings,
    AgentRunnerSafetySettings,
    AgentRunnerValidationSettings,
    AgentRunnerWorktreeSettings,
    AgentRunnerPostPrSupervisorSettings,
    IAR_REPOSITORY_CONFIG_FILENAME,
)
from backend.infrastructure.process_runner import CommandResult, SubprocessRunner


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

    cwd_path = (
        resolved_start_path
        if resolved_start_path.is_dir()
        else resolved_start_path.parent
    )
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
    "pre_push_review",
    "post_pr_supervisor",
    "generated_content",
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
    "pre_push_review": "提交前 AI review 门禁",
    "post_pr_supervisor": "Draft PR 创建后的自动 supervisor 配置",
    "generated_content": "GitHub Issue / PR 内容生成（面向人类阅读，不影响实现 Agent）",
}

# 子表路径 -> 子表说明注释。
_IAR_SUBTABLE_COMMENTS: dict[str, str] = {
    "generated_content.issue_from_prd": "从 PRD 生成 GitHub Issue 的模板",
    "generated_content.draft_pr": "从 commit 信息生成 Draft PR 的模板",
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
    "runner.default_agent": "默认使用的 AI agent：auto / claude / codex / kimi",
    "runner.max_recovery_attempts": "Agent 失败后的最大重试次数",
    "runner.recovery_retry_delay_seconds": "每次重试前等待的秒数",
    "runner.verification_commands": "提交前自动运行的验证命令；任一命令失败会进入 recovery",
    "safety.auto_merge": "是否允许自动合并 PR（强烈建议保持 false）",
    "safety.forbidden_path_patterns": "提交前禁止变更的路径通配模式",
    "validation.enabled": "是否启用 Realistic Validation 证据门禁",
    "validation.evidence_dir": "worktree 内证据目录（默认被 info/exclude 排除，不会进入代码 diff）",
    "validation.branch_prefix": "orphan 证据分支前缀",
    "validation.evidence_format_check": "是否逐项检查证据文件格式",
    "validation.parse_evidence_format_with_agent": "是否用 agent 解析 PRD 中的格式要求",
    "prompts.default_phase": "默认使用的 prompt 阶段",
    "prompts.phases": "自定义阶段模板，值为字符串或字符串列表",
    "pre_push_review.enabled": "是否启用提交前 AI review",
    "pre_push_review.review_agent": "执行 review 的 agent：auto / claude / codex / kimi",
    "pre_push_review.allow_same_agent": "是否允许实现 agent 与 reviewer 为同一个",
    "pre_push_review.max_attempts": "review 不通过时的最大修复轮数",
    "pre_push_review.timeout_seconds": "review agent 最长运行秒数",
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


def _uv_dependency_flag(
    pyproject_data: dict[str, Any], package_name: str
) -> str | None:
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


def detect_verification_commands(repo_root_path: Path) -> list[str]:
    """Detect verification commands that actually run in the target repository.

    ``iar init`` previously copied this tool's own defaults (such as
    ``uv run mkdocs build``) into every repository, which fails in any project
    that does not install mkdocs by default. Detection keeps the safe
    ``git diff --check`` baseline and adds tool commands only when the target
    repository declares the tool in ``pyproject.toml``, using the ``uv run``
    invocation (``--extra`` / ``--group``) that matches where the dependency
    is declared.
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

    if (repo_root_path / "tests").is_dir():
        pytest_flag = _uv_dependency_flag(pyproject_data, "pytest")
        if pytest_flag is not None:
            verification_commands.append(f"uv run{pytest_flag} pytest -q")

    return verification_commands


def build_repository_local_config_text(
    options: RepositoryInitOptions,
    process_runner: SubprocessRunner | None = None,
) -> tuple[Path, str]:
    """Render repository-local IAR TOML for a Git repository.

    Args:
        options: Init options, including cwd and explicit overrides.
        process_runner: Optional subprocess runner.

    Returns:
        A tuple of the detected repository root path and rendered TOML text.
    """
    repo_root_path = detect_git_repository_root(options.cwd, process_runner)
    selected_remote = options.remote_override or _detect_default_remote(
        repo_root_path, process_runner
    )
    detected_repo_id = _detect_repository_id(
        repo_root_path, selected_remote, process_runner
    )
    selected_repo_id = options.repo_id_override or detected_repo_id
    selected_display_name = options.display_name_override or repo_root_path.name
    selected_base_branch = options.base_branch_override or _detect_default_base_branch(
        repo_root_path, selected_remote, process_runner
    )

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
        runner=AgentRunnerRunnerSettings(
            verification_commands=detect_verification_commands(repo_root_path)
        ),
        safety=AgentRunnerSafetySettings(),
        validation=AgentRunnerValidationSettings(),
        prompts=AgentRunnerPromptSettings(),
        pre_push_review=AgentRunnerPrePushReviewSettings(),
        post_pr_supervisor=AgentRunnerPostPrSupervisorSettings(),
        generated_content=AgentRunnerGeneratedContentSettings(),
    )

    return repo_root_path, settings_to_toml_string(settings)


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
    repo_root_path, config_text = build_repository_local_config_text(
        options, process_runner
    )
    config_path = repo_root_path / IAR_REPOSITORY_CONFIG_FILENAME
    if config_path.exists() and not options.force and not options.dry_run:
        raise ValueError(
            f"IAR local config already exists at {config_path}. "
            "Use --force to overwrite it."
        )
    if options.dry_run:
        return RepositoryInitResult(
            repo_root_path=repo_root_path,
            config_path=config_path,
            config_text=config_text,
            wrote_file=False,
        )

    config_path.write_text(config_text, encoding="utf-8")
    return RepositoryInitResult(
        repo_root_path=repo_root_path,
        config_path=config_path,
        config_text=config_text,
        wrote_file=True,
    )


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
    current_branch_result = _run_git(
        ["branch", "--show-current"], repo_root_path, process_runner
    )
    current_branch = current_branch_result.stdout.strip()
    if current_branch:
        upstream_remote_result = _run_git(
            ["config", f"branch.{current_branch}.remote"],
            repo_root_path,
            process_runner,
        )
        upstream_remote = upstream_remote_result.stdout.strip()
        if upstream_remote:
            return upstream_remote

    configured_remotes = _list_git_remotes(repo_root_path, process_runner)
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
    remote_url_result = _run_git(
        ["remote", "get-url", remote_name], repo_root_path, process_runner
    )
    remote_url = remote_url_result.stdout.strip()
    if remote_url:
        remote_repo_name = _repository_name_from_remote_url(remote_url)
        if remote_repo_name:
            return _normalize_repository_id(remote_repo_name)
    return _normalize_repository_id(repo_root_path.name)


def _repository_name_from_remote_url(remote_url: str) -> str:
    trimmed_url = remote_url.rstrip("/")
    if trimmed_url.endswith(".git"):
        trimmed_url = trimmed_url[:-4]
    return re.split(r"[:/]", trimmed_url)[-1]


def _normalize_repository_id(raw_repo_name: str) -> str:
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
        remote_head_branch = _remote_head_branch(
            repo_root_path, candidate_remote, process_runner
        )
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

    current_branch_result = _run_git(
        ["branch", "--show-current"], repo_root_path, process_runner
    )
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
