"""Realistic Validation 证据上传与 PR 评论发布。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import AppConfig, IssueSummary
from backend.core.use_cases.agent_runner_evidence_format import IMAGE_EVIDENCE_SUFFIXES
from backend.core.use_cases.agent_runner_structured_evidence import (
    EvidenceUpload,
    build_evidence_blob_url,
    has_structured_evidence_marker,
    render_structured_evidence_comment,
    validate_evidence_manifest,
)
from backend.core.use_cases import agent_runner_validation as validation

_logger = logging.getLogger(__name__)
_PR_URL_PATTERN = re.compile(
    r"https?://[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)
_EVIDENCE_MARKER_PATTERN = re.compile(
    r"<!--\s*iar:validation-evidence\s+"
    r"version=(?P<version>\d+)\s+"
    r"head=(?P<head>[a-f0-9]+)\s+"
    r"branch=(?P<branch>[^\s>]+)\s+"
    r"count=(?P<count>\d+)"
    r"\s*-->"
)
_INLINE_TEXT_SUFFIXES = {".txt", ".log", ".md", ".out"}
_MAX_INLINE_EVIDENCE_CHARS = 3000


def evidence_branch_name(issue_number: int, config: AppConfig) -> str:
    """Return the orphan evidence branch name for an Issue."""
    return f"{config.validation.branch_prefix}issue-{issue_number}"


def _write_evidence_tree(
    *,
    blob_shas_by_path: dict[str, str],
    worktree_path: Path,
    process_runner: IProcessRunner,
) -> str:
    """Build a (possibly nested) git tree bottom-up and return its SHA.

    ``git mktree`` 只接受单层条目,子目录必须先成树再由父层以
    ``040000 tree <sha>`` 引用。走自底向上递归而非 ``GIT_INDEX_FILE`` 临时索引,
    是因为 :class:`IProcessRunner` 的 ``run`` 没有 ``env`` 参数,临时索引方案需要
    改动跨层接口。

    Args:
        blob_shas_by_path: 相对当前层的 POSIX 路径 → 已写入的 blob SHA。
        worktree_path: git 命令的工作目录。
        process_runner: 进程执行器。

    Returns:
        当前层的 tree SHA。
    """
    direct_blob_shas: dict[str, str] = {}
    subdir_blob_shas: dict[str, dict[str, str]] = {}
    for relative_path, blob_sha in blob_shas_by_path.items():
        directory_name, separator, remainder = relative_path.partition("/")
        if not separator:
            direct_blob_shas[relative_path] = blob_sha
            continue
        subdir_blob_shas.setdefault(directory_name, {})[remainder] = blob_sha

    mktree_entries = [
        f"100644 blob {blob_sha}\t{file_name}"
        for file_name, blob_sha in sorted(direct_blob_shas.items())
    ]
    for directory_name, nested_blob_shas in sorted(subdir_blob_shas.items()):
        subtree_sha = _write_evidence_tree(
            blob_shas_by_path=nested_blob_shas,
            worktree_path=worktree_path,
            process_runner=process_runner,
        )
        mktree_entries.append(f"040000 tree {subtree_sha}\t{directory_name}")

    tree_result = process_runner.run(
        ["git", "mktree"],
        cwd=worktree_path,
        input_text="\n".join(mktree_entries) + "\n",
    )
    return tree_result.stdout.strip()


def upload_evidence_branch(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> EvidenceUpload | None:
    """Push evidence files to the orphan evidence branch.

    使用 plumbing 命令构造树与无父提交，不触碰 worktree 的 HEAD / index：

    1. ``git hash-object -w`` 逐个写入 blob
    2. 自底向上 ``git mktree`` 构造(可含子目录的)树对象
    3. ``git commit-tree``（无 ``-p``）生成 orphan 提交
    4. ``git push --force`` 更新 ``refs/heads/<prefix>issue-<N>``

    收集文件用 :func:`validation.list_evidence_upload_files` 而非单层的
    ``list_evidence_files``:证据分支要连 ``{evidence_dir}/scripts/`` 下的 oracle
    源码一起带走,审阅者才能看到产出这份证据的断言是怎么写的。

    Returns:
        EvidenceUpload；证据目录为空时返回 ``None``。
    """
    evidence_relative_paths = validation.list_evidence_upload_files(worktree_path, config)
    if not evidence_relative_paths:
        return None

    evidence_dir = validation.evidence_dir_path(worktree_path, config)
    blob_shas_by_path: dict[str, str] = {}
    for relative_path in evidence_relative_paths:
        blob_result = process_runner.run(
            ["git", "hash-object", "-w", "--", str(evidence_dir / relative_path)],
            cwd=worktree_path,
        )
        blob_shas_by_path[relative_path] = blob_result.stdout.strip()
    uploaded_names = list(evidence_relative_paths)

    tree_sha = _write_evidence_tree(
        blob_shas_by_path=blob_shas_by_path,
        worktree_path=worktree_path,
        process_runner=process_runner,
    )
    commit_result = process_runner.run(
        [
            "git",
            "commit-tree",
            tree_sha,
            "-m",
            f"Realistic Validation evidence for issue #{issue.number}",
        ],
        cwd=worktree_path,
    )
    commit_sha = commit_result.stdout.strip()
    branch = evidence_branch_name(issue.number, config)
    process_runner.run(
        [
            "git",
            "push",
            "--force",
            config.git.remote,
            f"{commit_sha}:refs/heads/{branch}",
        ],
        cwd=worktree_path,
    )
    return EvidenceUpload(
        branch=branch,
        commit_sha=commit_sha,
        file_names=tuple(uploaded_names),
    )


def parse_pr_number(pr_url: str) -> int | None:
    """Extract the PR number from a GitHub PR URL."""
    url_match = _PR_URL_PATTERN.search(pr_url)
    if not url_match:
        return None
    return int(url_match.group("number"))


def _truncate_inline_evidence(file_text: str) -> str:
    """Limit inline-quoted evidence text in PR comments."""
    if len(file_text) <= _MAX_INLINE_EVIDENCE_CHARS:
        return file_text
    return (
        file_text[:_MAX_INLINE_EVIDENCE_CHARS]
        + "\n[evidence truncated; open the file on the evidence branch]"
    )


def build_evidence_comment(
    *,
    upload: EvidenceUpload,
    worktree_path: Path,
    config: AppConfig,
    pr_url: str,
    head_sha: str,
    issue_body: str = "",
) -> str:
    """Build the PR evidence comment with embedded images and quoted text.

    当 ``issue_body`` 带 ``iar:structured-evidence`` marker 时，按 checklist item
    分组渲染结构化证据块（命令、摘要、解释、风险、SHA-256）；否则按文件名平铺，
    保持与旧 Issue 的兼容。
    """
    if has_structured_evidence_marker(issue_body):
        checklist_items = validation.extract_realistic_validation_items(issue_body)
        report = validate_evidence_manifest(
            issue_body=issue_body,
            checklist_items=checklist_items,
            worktree_path=worktree_path,
            config=config,
        )
        return render_structured_evidence_comment(
            report=report,
            upload=upload,
            worktree_path=worktree_path,
            config=config,
            pr_url=pr_url,
            head_sha=head_sha,
        )

    marker = (
        f"<!-- iar:validation-evidence version=1 head={head_sha} "
        f"branch={upload.branch} count={len(upload.file_names)} -->"
    )
    comment_lines = [
        marker,
        "",
        "## Realistic Validation Evidence",
        "",
        f"- Evidence branch: `{upload.branch}` (orphan; never merged; "
        "auto-deleted after the issue closes)",
        f"- Code head at capture time: `{head_sha}`",
        "",
        "Review the evidence below, then tick the Realistic Validation "
        "checklist in the PR description to sign off.",
    ]
    for file_name in upload.file_names:
        file_suffix = Path(file_name).suffix.lower()
        file_blob_url = build_evidence_blob_url(pr_url, upload.branch, file_name)
        comment_lines.extend(["", f"### {file_name}"])
        if file_blob_url and file_suffix in IMAGE_EVIDENCE_SUFFIXES:
            comment_lines.append(f"![{file_name}]({file_blob_url}?raw=true)")
            comment_lines.append(f"[Open image]({file_blob_url})")
            continue
        if file_suffix in _INLINE_TEXT_SUFFIXES:
            evidence_file_path = validation.evidence_dir_path(worktree_path, config) / file_name
            try:
                file_text = evidence_file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                file_text = "[unreadable evidence file]"
            comment_lines.append("```text")
            comment_lines.append(_truncate_inline_evidence(file_text.rstrip()))
            comment_lines.append("```")
        if file_blob_url:
            comment_lines.append(f"[Open file]({file_blob_url})")
    return "\n".join(comment_lines)


def parse_latest_evidence_marker(pr_comments: list[str]) -> validation.EvidenceMarker | None:
    """Parse the latest iar:validation-evidence marker from PR comments."""
    for comment_body in reversed(pr_comments):
        marker_match = _EVIDENCE_MARKER_PATTERN.search(comment_body)
        if marker_match:
            return validation.EvidenceMarker(
                head_sha=marker_match.group("head"),
                branch=marker_match.group("branch"),
                count=int(marker_match.group("count")),
            )
    return None


def publish_validation_evidence(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    pr_url: str,
    head_sha: str,
) -> EvidenceUpload | None:
    """Upload evidence and post the PR evidence comment.

    Returns:
        EvidenceUpload；不要求验证或无证据文件时返回 ``None``。
    """
    if not validation.validation_required(issue.body, config):
        return None
    upload = upload_evidence_branch(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        process_runner=process_runner,
    )
    if upload is None:
        _logger.warning(
            "Issue #%d requires validation but no evidence files were found "
            "when publishing evidence.",
            issue.number,
        )
        return None
    pr_number = parse_pr_number(pr_url)
    if pr_number is None:
        raise RuntimeError(f"Cannot post validation evidence: unparsable PR URL {pr_url!r}")
    github_client.comment_pr(
        pr_number,
        build_evidence_comment(
            upload=upload,
            worktree_path=worktree_path,
            config=config,
            pr_url=pr_url,
            head_sha=head_sha,
            issue_body=issue.body,
        ),
    )
    return upload


def publish_validation_evidence_best_effort(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    pr_url: str,
    head_sha: str,
) -> EvidenceUpload | None:
    """尽力发布证据评论；失败只记录日志，绝不向上抛异常。

    证据评论是审计信息的镶边，真正的门禁是 PR body 里的 checklist 与
    verifier/checks 标签——评论本身发不出去（例如 GitHub 边缘偶发的瞬时
    4xx/5xx）不该让调用方把已经成功的 push/PR/label 状态回滚成失败。首次
    发布、rework 证据刷新、手动 recover 三个调用点都需要这个语义，因此收敛
    成一个共享实现，而不是各自复制一份 try/except。

    Returns:
        EvidenceUpload；失败、不要求验证或无证据文件时返回 ``None``。
    """
    try:
        return publish_validation_evidence(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            github_client=github_client,
            process_runner=process_runner,
            pr_url=pr_url,
            head_sha=head_sha,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort by design, see docstring.
        _logger.warning(
            "Failed to publish validation evidence for Issue #%d (non-fatal): %s",
            issue.number,
            exc,
        )
        return None
