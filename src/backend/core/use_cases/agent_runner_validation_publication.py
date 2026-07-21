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
    2. ``git mktree`` 由 stdin 构造树对象
    3. ``git commit-tree``（无 ``-p``）生成 orphan 提交
    4. ``git push --force`` 更新 ``refs/heads/<prefix>issue-<N>``

    Returns:
        EvidenceUpload；证据目录为空时返回 ``None``。
    """
    evidence_files = validation.list_evidence_files(worktree_path, config)
    if not evidence_files:
        return None

    mktree_entries: list[str] = []
    uploaded_names: list[str] = []
    for evidence_file in evidence_files:
        blob_result = process_runner.run(
            ["git", "hash-object", "-w", "--", str(evidence_file)],
            cwd=worktree_path,
        )
        blob_sha = blob_result.stdout.strip()
        mktree_entries.append(f"100644 blob {blob_sha}\t{evidence_file.name}")
        uploaded_names.append(evidence_file.name)

    tree_result = process_runner.run(
        ["git", "mktree"],
        cwd=worktree_path,
        input_text="\n".join(mktree_entries) + "\n",
    )
    tree_sha = tree_result.stdout.strip()
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
