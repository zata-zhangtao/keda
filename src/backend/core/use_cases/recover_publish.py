"""发布恢复用例：用于恢复此前失败的发布（publish）流程。

当 Agent 已经在本地 worktree 完成提交（commit），但后续的推送（push）或
建 PR 环节失败时，本模块负责在不重新运行 Agent、不重新提交、不改动工作树
内容的前提下，安全地把已有提交推送到远端并创建/复用草稿 PR，从而完成发布。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    PublishFailureCategory,
    PublishRecoveryRequest,
    PublishRecoveryResult,
)
from backend.core.use_cases.agent_runner_failure import (
    build_publish_failure_comment_body,
)
from backend.core.use_cases.agent_runner_validation import (
    build_validation_checklist_block,
    extract_realistic_validation_items,
    publish_validation_evidence,
    validation_required,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "PublishRecoveryError",
    "resolve_existing_worktree",
    "validate_worktree_clean",
    "validate_branch_safety",
    "recover_publish_issue",
    "build_recovery_success_comment",
]


class PublishRecoveryError(RuntimeError):
    """发布恢复流程的基础异常类型。

    当恢复流程因任意安全校验失败或外部命令出错而无法继续时抛出，
    上层调用方可统一捕获该异常类型来处理恢复失败。
    """

    def __init__(
        self,
        message: str,
        *,
        worktree_path: Path | None = None,
        failure_category: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.worktree_path = worktree_path
        self.failure_category = failure_category


def resolve_existing_worktree(
    repo_path: Path,
    issue_number: int,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> Path:
    """解析已存在的 Issue 工作树（worktree）路径，且不会创建新的工作树。

    恢复流程依赖一个已经存在、且包含本地提交的工作树，因此这里只做“解析 +
    校验存在性”，绝不在缺失时新建，以免覆盖或干扰用户已有的工作进度。

    Args:
        repo_path (Path): 主仓库路径。
        issue_number (int): GitHub Issue 编号。
        config (AppConfig): 应用配置。
        process_runner (IProcessRunner): 用于执行命令的进程执行器。

    Returns:
        Path: 解析后的工作树绝对路径。

    Raises:
        PublishRecoveryError: 当工作树路径无法解析、不存在，或不是合法的
            git 工作树时抛出。
    """
    from backend.core.use_cases.run_agent_once import format_command

    # 通过配置中的 path_command 模板（注入 issue_number）计算工作树路径，
    # 复用与正常发布流程相同的路径推导逻辑，保证两条路径一致。
    path_result = process_runner.run(
        format_command(config.worktree.path_command, issue_number=issue_number),
        cwd=repo_path,
    )
    worktree_path = Path(path_result.stdout.strip()).resolve()

    if not worktree_path.exists():
        raise PublishRecoveryError(
            f"Issue worktree does not exist: {worktree_path}. "
            f"Recovery requires an existing worktree with a local commit."
        )

    # 路径存在并不代表它就是有效的 git 工作树，需再用 git rev-parse 确认，
    # 防止误把普通目录当作工作树继续后续的 git 操作。check=False 以便手动判错。
    git_dir_result = process_runner.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=worktree_path,
        check=False,
    )
    if git_dir_result.return_code != 0:
        raise PublishRecoveryError(f"Path is not a valid git worktree: {worktree_path}")

    return worktree_path


def validate_worktree_clean(
    worktree_path: Path,
    process_runner: IProcessRunner,
) -> None:
    """校验工作树没有未提交的改动（保持干净状态）。

    恢复流程的契约是“只发布已有提交、不产生新提交”。若工作树存在未提交改动，
    说明状态不符合预期（可能 Agent 仍在运行或用户手动改动），此时贸然推送可能
    遗漏或混入未提交内容，因此必须拒绝。

    Args:
        worktree_path (Path): 工作树路径。
        process_runner (IProcessRunner): 用于执行命令的进程执行器。

    Raises:
        PublishRecoveryError: 当工作树存在未提交改动时抛出。
    """
    # --porcelain 输出稳定且易于机器解析：非空即代表存在未暂存/未提交改动。
    status_result = process_runner.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
    )
    if status_result.stdout.strip():
        raise PublishRecoveryError(
            f"Worktree has uncommitted changes. "
            f"Recovery requires a clean worktree with an existing commit. "
            f"Path: {worktree_path}"
        )


def validate_branch_safety(
    *,
    worktree_path: Path,
    issue_number: int,
    config: AppConfig,
    process_runner: IProcessRunner,
    expected_branch: str | None = None,
) -> str:
    """校验发布恢复时的分支安全性，确认当前分支可以安全推送。

    该函数是恢复流程的关键“护栏”，依次拦截三类危险场景：游离 HEAD、误用基线
    分支、以及分支与目标 Issue 不匹配，避免把提交推送到错误的分支上。

    Args:
        worktree_path (Path): 工作树路径。
        issue_number (int): GitHub Issue 编号。
        config (AppConfig): 应用配置。
        process_runner (IProcessRunner): 用于执行命令的进程执行器。
        expected_branch (str | None): 调用方显式指定的期望分支名；若提供，则当前
            分支必须与之完全一致。

    Returns:
        str: 校验通过后的当前分支名。

    Raises:
        PublishRecoveryError: 当处于游离 HEAD、当前为基线分支，或分支与期望/
            Issue 编号不匹配时抛出。
    """
    branch_result = process_runner.run(
        ["git", "branch", "--show-current"],
        cwd=worktree_path,
    )
    current_branch = branch_result.stdout.strip()

    # 游离 HEAD（detached HEAD）下没有可推送的分支名，无法安全发布。
    if not current_branch:
        raise PublishRecoveryError(
            "Cannot recover from detached HEAD state. " "Checkout a valid branch first."
        )

    # 严禁从基线分支（如 main）直接发布，否则会把 Issue 提交污染主干。
    if current_branch == config.git.base_branch:
        raise PublishRecoveryError(
            f"Refusing to publish from base branch '{config.git.base_branch}'. "
            f"Switch to the issue branch and retry."
        )

    # 若调用方显式指定了期望分支，则采用“精确匹配”这一最严格的确认方式，
    # 通过后直接返回，跳过下面基于命名约定的启发式校验。
    if expected_branch is not None:
        if current_branch != expected_branch:
            raise PublishRecoveryError(
                f"Current branch '{current_branch}' does not match "
                f"expected branch '{expected_branch}'. "
                f"Use --branch to confirm the current branch."
            )
        return current_branch

    # 未显式指定分支时，退而用命名约定做启发式校验：分支名应包含对应的 Issue
    # 编号作为完整 token 或路径 segment，避免 issue-421 被错误匹配到 Issue #42。
    issue_number_token = str(issue_number)
    branch_segments = re.split(r"[-_/]+", current_branch)
    branch_matches_issue = any(
        segment == issue_number_token for segment in branch_segments
    )

    if not branch_matches_issue:
        raise PublishRecoveryError(
            f"Branch '{current_branch}' does not appear to reference "
            f"Issue #{issue_number}. "
            f"Use --branch to explicitly confirm the current branch."
        )

    return current_branch


def _build_recovery_failure_comment(
    *,
    issue_number: int,
    failure_category: str,
    worktree_path: Path | None,
    exc: BaseException,
) -> str:
    """构建发布恢复失败后写入 Issue 的评论正文。"""
    return build_publish_failure_comment_body(
        header="## Agent Runner Publish Recovery Failed",
        intro="The publish recovery command failed.",
        action_intro="To retry publishing without re-running the agent:",
        issue_number=issue_number,
        failure_category=failure_category,
        worktree_path=worktree_path,
        exc=exc,
    )


def recover_publish_issue(
    *,
    request: PublishRecoveryRequest,
    repo_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> PublishRecoveryResult:
    """恢复某个 Issue 此前失败的发布（publish）操作。

    本函数为“已存在本地提交”的任务安全地续做发布，整个过程不会运行 Agent、
    不会创建提交、也不会改动工作树内容；它只做校验、推送已有提交并创建/复用 PR。
    当 post-PR supervisor 启用时，恢复后的 PR 会先进入 ``agent/supervising``，
    运行现有 supervisor repair loop，只有 supervisor approve 后才进入
    ``agent/review``。当 supervisor 禁用时，保留直接进入 ``agent/review`` 的 fallback。

    执行顺序经过精心编排：先完成全部本地安全校验与推送，确认成功后才更新标签、
    评论 Issue，确保只有真正发布成功才会改变 Issue 的可见状态。任何 push、PR lookup
    或 PR creation 失败都不会修改 labels，只会 posting failure comment。

    Args:
        request (PublishRecoveryRequest): 恢复请求，含 Issue 编号与可选分支。
        repo_path (Path): 主仓库路径。
        config (AppConfig): 应用配置。
        github_client (IGitHubClient): 用于 GitHub API 操作的客户端。
        process_runner (IProcessRunner): 用于执行 Git 命令的进程执行器。

    Returns:
        PublishRecoveryResult: 包含分支名、HEAD SHA、PR 链接及是否复用 PR 的结果。

    Raises:
        PublishRecoveryError: 当任一安全校验失败或推送失败，导致无法安全恢复时抛出。
    """
    from backend.core.use_cases.agent_runner_git import has_changes
    from backend.core.use_cases.agent_runner_supervisor import (
        _run_supervisor_with_repair_loop,
    )
    from backend.core.use_cases.run_agent_once import (
        get_head_sha,
        list_git_remotes,
    )

    issue_number = request.issue_number

    # 第一步：解析已存在的工作树（不创建）。
    worktree_path = resolve_existing_worktree(
        repo_path, issue_number, config, process_runner
    )

    # 第二步：确认工作树干净，保证只发布已有提交。
    validate_worktree_clean(worktree_path, process_runner)

    # 第三步：分支安全护栏，拒绝游离 HEAD / 基线分支 / 不匹配分支。
    branch = validate_branch_safety(
        worktree_path=worktree_path,
        issue_number=issue_number,
        config=config,
        process_runner=process_runner,
        expected_branch=request.expected_branch,
    )

    # 在任何推送/远端操作之前先记录 HEAD SHA，作为本次发布提交的稳定标识，
    # 后续用于评论与返回结果。
    head_sha = get_head_sha(worktree_path, process_runner)

    # 推送前校验配置的远端是否真实存在，提前给出可读的报错与可用远端列表，
    # 避免 git push 因远端不存在而产生晦涩的失败信息。
    remote_names = list_git_remotes(worktree_path, process_runner)
    configured_remote = config.git.remote
    if configured_remote not in remote_names:
        available_text = ", ".join(remote_names) if remote_names else "(none)"
        raise PublishRecoveryError(
            f"Configured git remote '{configured_remote}' does not exist. "
            f"Available remotes: {available_text}. "
            f"Update [agent_runner.git].remote in config.toml.",
            worktree_path=worktree_path,
            failure_category=PublishFailureCategory.PUSH.value,
        )

    # 第四步：将分支推送到配置的远端（-u 建立上游跟踪）。
    _logger.info(
        "Pushing branch '%s' to remote '%s' for Issue #%d",
        branch,
        configured_remote,
        issue_number,
    )
    push_result = process_runner.run(
        ["git", "push", "-u", configured_remote, branch],
        cwd=worktree_path,
        check=False,
    )
    if push_result.return_code != 0:
        exc = PublishRecoveryError(
            f"Failed to push branch '{branch}' to remote '{configured_remote}'. "
            f"Exit code: {push_result.return_code}. "
            f"Stderr: {push_result.stderr}",
            worktree_path=worktree_path,
            failure_category=PublishFailureCategory.PUSH.value,
        )
        github_client.comment_issue(
            issue_number,
            _build_recovery_failure_comment(
                issue_number=issue_number,
                failure_category=PublishFailureCategory.PUSH.value,
                worktree_path=worktree_path,
                exc=exc,
            ),
        )
        raise exc

    # 第五步：查找该分支是否已有处于 open 状态的 PR。恢复场景下 PR 可能在上次
    # 失败前已创建，复用可避免产生重复 PR。
    try:
        existing_pr_url = github_client.find_open_pr_by_head(branch)
    except Exception as lookup_exc:  # noqa: BLE001
        exc = PublishRecoveryError(
            f"Failed to look up open PR for branch '{branch}': {lookup_exc}",
            worktree_path=worktree_path,
            failure_category=PublishFailureCategory.PR_LOOKUP.value,
        )
        github_client.comment_issue(
            issue_number,
            _build_recovery_failure_comment(
                issue_number=issue_number,
                failure_category=PublishFailureCategory.PR_LOOKUP.value,
                worktree_path=worktree_path,
                exc=exc,
            ),
        )
        raise exc

    pr_reused = existing_pr_url is not None

    # Realistic Validation 上下文：恢复发布的 PR 同样需要人工签收清单与证据。
    recovered_issue: IssueSummary | None
    try:
        recovered_issue = github_client.get_issue(issue_number)
    except Exception as issue_lookup_exc:  # noqa: BLE001 - validation is best effort here.
        recovered_issue = None
        _logger.warning(
            "Could not load Issue #%d for validation materialization: %s",
            issue_number,
            issue_lookup_exc,
        )

    if existing_pr_url:
        pr_url = existing_pr_url
        _logger.info(
            "Reusing existing PR for Issue #%d: %s",
            issue_number,
            pr_url,
        )
    else:
        # 不存在可复用 PR 时创建草稿 PR；正文中的 Closes #N 用于在合并后自动关闭
        # 对应 Issue。优先使用 Issue 标题让 PR 标题更具可读性，取不到时回退到编号。
        issue_title = recovered_issue.title if recovered_issue is not None else None
        pr_title = (
            f"[Agent] {issue_title}"
            if issue_title
            else f"[Agent] Issue #{issue_number}"
        )
        pr_body = f"Closes #{issue_number}\n\nRecovered by issue-agent-runner.\n"
        if recovered_issue is not None and validation_required(
            recovered_issue.body, config
        ):
            validation_checklist_items = extract_realistic_validation_items(
                recovered_issue.body
            )
            if validation_checklist_items:
                checklist_block = build_validation_checklist_block(
                    validation_checklist_items
                )
                pr_body = f"{pr_body.rstrip()}\n\n{checklist_block}\n"

        _logger.info("Creating draft PR for Issue #%d", issue_number)
        try:
            pr_url = github_client.create_draft_pr(
                title=pr_title,
                body=pr_body,
                base_branch=config.git.base_branch,
                cwd=worktree_path,
            )
        except Exception as create_exc:  # noqa: BLE001
            exc = PublishRecoveryError(
                f"Failed to create draft PR for branch '{branch}': {create_exc}",
                worktree_path=worktree_path,
                failure_category=PublishFailureCategory.PR_CREATE.value,
            )
            github_client.comment_issue(
                issue_number,
                _build_recovery_failure_comment(
                    issue_number=issue_number,
                    failure_category=PublishFailureCategory.PR_CREATE.value,
                    worktree_path=worktree_path,
                    exc=exc,
                ),
            )
            raise exc

    # 第 5.5 步：上传验证证据并发 PR 证据评论（要求验证且证据存在时）。
    if recovered_issue is not None:
        try:
            publish_validation_evidence(
                issue=recovered_issue,
                worktree_path=worktree_path,
                config=config,
                github_client=github_client,
                process_runner=process_runner,
                pr_url=pr_url,
                head_sha=head_sha,
            )
        except Exception as evidence_exc:  # noqa: BLE001 - recovery must not abort here.
            _logger.warning(
                "Failed to publish validation evidence during recovery for "
                "Issue #%d: %s",
                issue_number,
                evidence_exc,
            )

    # 第六步：推送与 PR 均成功后，写入恢复成功评论。
    success_comment = build_recovery_success_comment(
        branch=branch,
        head_sha=head_sha,
        pr_url=pr_url,
        pr_reused=pr_reused,
    )
    try:
        github_client.comment_issue(issue_number, success_comment)
    except Exception as comment_exc:  # noqa: BLE001
        exc = PublishRecoveryError(
            f"Failed to post recovery success comment: {comment_exc}",
            worktree_path=worktree_path,
            failure_category=PublishFailureCategory.COMMENT_UPDATE.value,
        )
        raise exc

    # 第七步：根据 supervisor 配置决定标签流转。
    supervisor_action: str | None = None
    if config.post_pr_supervisor.enabled:
        # 成功恢复后先进入 supervising，运行 supervisor。
        try:
            github_client.edit_issue_labels(
                issue_number,
                add=[config.labels.supervising],
                remove=[
                    config.labels.failed,
                    config.labels.running,
                    config.labels.ready,
                    config.labels.review,
                ],
            )
        except Exception as label_exc:  # noqa: BLE001
            exc = PublishRecoveryError(
                f"Failed to update labels to supervising: {label_exc}",
                worktree_path=worktree_path,
                failure_category=PublishFailureCategory.LABEL_UPDATE.value,
            )
            raise exc

        # 获取 Issue context 和 PR context 以运行 supervisor。
        try:
            issue = github_client.get_issue(issue_number)
        except Exception as get_issue_exc:  # noqa: BLE001
            exc = PublishRecoveryError(
                f"Failed to fetch Issue #{issue_number} for supervisor: {get_issue_exc}",
                worktree_path=worktree_path,
                failure_category=PublishFailureCategory.UNKNOWN.value,
            )
            raise exc

        pr_context = github_client.get_pull_request_context(branch)
        if pr_context is None:
            _logger.warning(
                "Deferring post-PR supervisor for Issue #%d branch %s: "
                "complete PR context is unavailable.",
                issue_number,
                branch,
            )
            supervisor_action = "deferred_pr_context_unavailable"
        else:
            supervisor_config = config.post_pr_supervisor
            supervisor_agent = (
                config.runner.default_agent
                if supervisor_config.supervisor_agent == "auto"
                else supervisor_config.supervisor_agent
            )
            # 在运行 supervisor 前再次确认工作树干净（只读 supervisor 的契约）。
            if has_changes(worktree_path, process_runner):
                dirty_exc = PublishRecoveryError(
                    "Worktree has uncommitted changes before supervisor cycle. "
                    "Recovery requires a clean worktree.",
                    worktree_path=worktree_path,
                    failure_category=PublishFailureCategory.UNKNOWN.value,
                )
                github_client.comment_issue(
                    issue_number,
                    _build_recovery_failure_comment(
                        issue_number=issue_number,
                        failure_category=PublishFailureCategory.UNKNOWN.value,
                        worktree_path=worktree_path,
                        exc=dirty_exc,
                    ),
                )
                raise dirty_exc

            _run_supervisor_with_repair_loop(
                issue=issue,
                worktree_path=worktree_path,
                config=config,
                github_client=github_client,
                process_runner=process_runner,
                pr_context=pr_context,
                supervisor_agent=supervisor_agent,
            )
            supervisor_action = "supervisor_completed"
    else:
        # Supervisor 禁用时直接进入 review（fallback）。
        try:
            github_client.edit_issue_labels(
                issue_number,
                add=[config.labels.review],
                remove=[
                    config.labels.failed,
                    config.labels.running,
                    config.labels.ready,
                ],
            )
        except Exception as label_exc:  # noqa: BLE001
            exc = PublishRecoveryError(
                f"Failed to update labels to review: {label_exc}",
                worktree_path=worktree_path,
                failure_category=PublishFailureCategory.LABEL_UPDATE.value,
            )
            raise exc
        supervisor_action = "supervisor_disabled_fallback"

    _logger.info(
        "Publish recovery complete for Issue #%d: branch=%s, pr=%s, reused=%s, "
        "supervisor_action=%s",
        issue_number,
        branch,
        pr_url,
        pr_reused,
        supervisor_action,
    )

    return PublishRecoveryResult(
        issue_number=issue_number,
        branch=branch,
        head_sha=head_sha,
        pr_url=pr_url,
        pr_reused=pr_reused,
        supervisor_action=supervisor_action,
    )


def build_recovery_success_comment(
    *,
    branch: str,
    head_sha: str,
    pr_url: str,
    pr_reused: bool,
) -> str:
    """构建发布恢复成功后写入 Issue 的评论正文。

    评论包含 ``iar:event`` marker，``review_once`` 可通过该 marker 解析出
    PR branch 上下文。

    Args:
        branch (str): 已推送的分支名。
        head_sha (str): 提交的 HEAD SHA。
        pr_url (str): PR 链接（新建或复用）。
        pr_reused (bool): 是否复用了已存在的 PR。

    Returns:
        str: Markdown 格式的评论正文。
    """
    from backend.core.use_cases.agent_runner_events import format_event_marker

    marker = format_event_marker(
        phase="publish_recovered",
        cycle=1,
        head_sha=head_sha,
        pr_branch=branch,
    )
    # 复用与新建在文案上区分开，方便人工在 Issue 中快速识别 PR 来源。
    reuse_status = "reused" if pr_reused else "created"
    return "\n".join(
        [
            marker,
            "",
            "## Agent Runner Publish Recovered",
            "",
            f"- Branch: `{branch}`",
            f"- HEAD SHA: `{head_sha}`",
            f"- Draft PR ({reuse_status}): {pr_url}",
        ]
    )
