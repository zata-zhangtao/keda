"""Realistic Validation evidence gate for the agent runner.

本模块承载"验证证据门禁"的全部 core 逻辑：

1. **物化解析** — 从 PRD / Issue body 解析 ``Realistic Validation`` 清单与
   ``Validation Waiver`` 豁免声明。
2. **证据隔离** — 把证据目录写入 worktree 的 ``info/exclude``，并在发布前
   拒绝混入代码 diff 的证据路径。
3. **证据强制** — commit 前要求证据目录非空（``ValidationEvidenceError``
   进入既有 recovery 循环）。
4. **证据呈现** — 用 git plumbing（``hash-object``/``mktree``/``commit-tree``）
   构造无父提交并 force-push 到 orphan 证据分支，再在 PR 上发证据评论。
5. **软门禁** — daemon 轮询 ``agent/review`` Issue，按 PR body 勾选状态维护
   ``validation/pending`` / ``validation/passed`` label，head 漂移时重置勾选，
   Issue 关闭后清理证据分支。

所有 hidden marker 与 ``agent_runner_events.py`` 的 ``iar:event`` 同型
（``<!-- iar:... -->`` + 命名捕获组正则）。GitHub Issue / PR 仍是唯一状态源。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    PullRequestContext,
)
from backend.core.use_cases.agent_runner_events import (
    format_event_marker,
    parse_latest_event_marker,
    parse_latest_event_marker_for_phases,
)
from backend.core.use_cases.agent_runner_evidence_format import (
    EvidenceKindRule as EvidenceKindRule,
    IMAGE_EVIDENCE_SUFFIXES,
    collect_evidence_coverage_problems,
    demanded_evidence_kinds as demanded_evidence_kinds,
    extract_evidence_format_markers as extract_evidence_format_markers,
)
from backend.core.use_cases.agent_runner_git import list_changed_paths
from backend.core.use_cases.agent_runner_structured_evidence import (
    EvidenceUpload,
    ValidationEvidenceError,
    build_evidence_blob_url,
    build_structured_evidence_prompt_suffix,
    format_structured_evidence_marker,
    has_structured_evidence_marker,
    render_structured_evidence_comment,
    validate_evidence_manifest,
)

_logger = logging.getLogger(__name__)

_VALIDATION_SECTION_TITLE = "realistic validation"
_VALIDATION_SECTION_HEADER_RE = re.compile(
    r"^(?:\d+\.\s+)?" + re.escape(_VALIDATION_SECTION_TITLE),
    re.IGNORECASE,
)
_WAIVER_LINE_PATTERN = re.compile(
    r"^[-*\s]*Validation Waiver[:：]\s*(?P<reason>.+)$",
    re.IGNORECASE,
)
_WAIVER_MARKER_PATTERN = re.compile(
    r"<!--\s*iar:validation-waived(?:\s+reason=\"(?P<reason>[^\"]*)\")?\s*-->"
)
_FORMAT_WAIVER_LINE_PATTERN = re.compile(
    r"^[-*\s]*Evidence Format Waiver[:：]\s*(?P<reason>.+)$",
    re.IGNORECASE,
)
_FORMAT_WAIVER_MARKER_PATTERN = re.compile(
    r"<!--\s*iar:evidence-format-waived(?:\s+reason=\"(?P<reason>[^\"]*)\")?\s*-->"
)
_CHECKLIST_START_PATTERN = re.compile(
    r"<!--\s*iar:realistic-validation\s+version=(?P<version>\d+)\s+total=(?P<total>\d+)\s*-->"
)
_CHECKLIST_END_MARKER = "<!-- iar:realistic-validation-end -->"
_EVIDENCE_MARKER_PATTERN = re.compile(
    r"<!--\s*iar:validation-evidence\s+"
    r"version=(?P<version>\d+)\s+"
    r"head=(?P<head>[a-f0-9]+)\s+"
    r"branch=(?P<branch>[^\s>]+)\s+"
    r"count=(?P<count>\d+)"
    r"\s*-->"
)
_CHECKED_ITEM_PATTERN = re.compile(r"^\s*[-*] \[[xX]\] ")
_UNCHECKED_ITEM_PATTERN = re.compile(r"^\s*[-*] \[ \] ")
_PR_URL_PATTERN = re.compile(
    r"https?://[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)

_INLINE_TEXT_SUFFIXES = {".txt", ".log", ".md", ".out"}
_MAX_INLINE_EVIDENCE_CHARS = 3000


@dataclass(frozen=True)
class ValidationChecklistState:
    """Parsed state of the PR body Realistic Validation checklist."""

    total: int
    checked_count: int
    unchecked_count: int


@dataclass(frozen=True)
class EvidenceMarker:
    """Parsed iar:validation-evidence hidden marker from a PR comment."""

    head_sha: str
    branch: str
    count: int


# ---------------------------------------------------------------------------
# Markdown 解析：Realistic Validation 清单与 Waiver 声明
# ---------------------------------------------------------------------------


def _iterate_validation_section_lines(markdown_text: str) -> list[str]:
    """Return the lines inside the Realistic Validation section.

    接受任意级别的 Markdown 标题（PRD 用 ``###``、Issue body 用 ``##``），
    标题文本以 ``Realistic Validation`` 开头（大小写不敏感）即进入小节，
    遇到同级或更高级标题退出。
    """
    section_lines: list[str] = []
    section_heading_level = 0
    for line in markdown_text.splitlines():
        stripped_line = line.strip()
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped_line)
        if heading_match:
            heading_level = len(heading_match.group(1))
            heading_text = heading_match.group(2).strip().lower()
            if section_heading_level:
                if heading_level <= section_heading_level:
                    break
                section_lines.append(line)
                continue
            if _VALIDATION_SECTION_HEADER_RE.match(heading_text):
                section_heading_level = heading_level
            continue
        if section_heading_level:
            section_lines.append(line)
    return section_lines


def extract_realistic_validation_items(markdown_text: str) -> list[str]:
    """Extract checkbox items from the Realistic Validation section.

    勾选状态会被规范化为未勾选 ``- [ ]``，因为清单代表的是*待人工确认*
    的验证项。

    Args:
        markdown_text: PRD 全文或 Issue body。

    Returns:
        规范化后的 Markdown 复选框行列表；无小节或无条目时为空列表。
    """
    checklist_items: list[str] = []
    for section_line in _iterate_validation_section_lines(markdown_text):
        stripped_line = section_line.strip()
        if stripped_line.startswith("- ["):
            checklist_items.append(re.sub(r"^- \[[ xX]\]", "- [ ]", stripped_line))
    return checklist_items


def extract_validation_waiver_reason(markdown_text: str) -> str | None:
    """Extract an explicit ``Validation Waiver: <reason>`` declaration.

    只接受 Realistic Validation 小节内的显式声明行，不做自然语言推断。

    Returns:
        豁免理由文本；无显式声明时返回 ``None``。
    """
    for section_line in _iterate_validation_section_lines(markdown_text):
        waiver_match = _WAIVER_LINE_PATTERN.match(section_line.strip())
        if waiver_match:
            return waiver_match.group("reason").strip()
    return None


def format_validation_waiver_marker(reason: str) -> str:
    """Format the hidden waiver marker for an Issue body."""
    sanitized_reason = reason.replace('"', "'").replace("\n", " ").strip()
    return f'<!-- iar:validation-waived reason="{sanitized_reason}" -->'


def has_validation_waiver_marker(text: str) -> bool:
    """Return True when the text carries an iar:validation-waived marker."""
    return _WAIVER_MARKER_PATTERN.search(text) is not None


def extract_evidence_format_waiver_reason(markdown_text: str) -> str | None:
    """Extract an ``Evidence Format Waiver: <reason>`` declaration.

    与 :func:`extract_validation_waiver_reason` 同型：只接受 Realistic
    Validation 小节内的显式声明行。该豁免只关闭逐项格式对账，证据本身
    仍然必须存在。

    Returns:
        豁免理由文本；无显式声明时返回 ``None``。
    """
    for section_line in _iterate_validation_section_lines(markdown_text):
        format_waiver_match = _FORMAT_WAIVER_LINE_PATTERN.match(section_line.strip())
        if format_waiver_match:
            return format_waiver_match.group("reason").strip()
    return None


def format_evidence_format_waiver_marker(reason: str) -> str:
    """Format the hidden evidence-format waiver marker for an Issue body."""
    sanitized_reason = reason.replace('"', "'").replace("\n", " ").strip()
    return f'<!-- iar:evidence-format-waived reason="{sanitized_reason}" -->'


def has_evidence_format_waiver_marker(text: str) -> bool:
    """Return True when the text carries an iar:evidence-format-waived marker."""
    return _FORMAT_WAIVER_MARKER_PATTERN.search(text) is not None


def evidence_format_check_required(issue_body: str, config: AppConfig) -> bool:
    """Return True when per-item evidence format matching should run.

    配置 ``validation.evidence_format_check = false`` 全局关闭；Issue body
    带 ``iar:evidence-format-waived`` marker（来自 PRD 的 Evidence Format
    Waiver 声明）按任务关闭。
    """
    if not config.validation.evidence_format_check:
        return False
    return not has_evidence_format_waiver_marker(issue_body)


def build_issue_validation_section(
    *,
    checklist_items: list[str],
    waiver_reason: str | None,
    format_waiver_reason: str | None = None,
    language: str = "zh-CN",
    structured_evidence: bool = True,
) -> str:
    """Build the deterministic ``## Realistic Validation`` Issue body block.

    与 AI 生成正文无关的确定性物化：waiver 优先（出现 marker、无清单），
    否则输出未勾选清单与证据要求说明；PRD 声明了 Evidence Format Waiver
    时附带格式豁免 marker（证据仍必须存在，仅跳过逐项格式对账）。

    当 ``structured_evidence`` 为 true 且存在 checklist 时，在区块开头附加
    ``iar:structured-evidence`` hidden marker。
    """
    structured_marker = ""
    if structured_evidence and checklist_items and waiver_reason is None:
        structured_marker = format_structured_evidence_marker(language) + "\n\n"

    if waiver_reason is not None:
        section_lines = [
            "## Realistic Validation",
            "",
        ]
        if structured_marker:
            section_lines.append(structured_marker.rstrip())
            section_lines.append("")
        section_lines.extend(
            [
                format_validation_waiver_marker(waiver_reason),
                "",
                f"Validation waived by operator: {waiver_reason}",
            ]
        )
        return "\n".join(section_lines)

    format_waiver_lines: list[str] = []
    if format_waiver_reason is not None:
        format_waiver_lines = [
            format_evidence_format_waiver_marker(format_waiver_reason),
            "",
            f"Evidence format matching waived by operator: {format_waiver_reason}",
            "",
        ]
    return "\n".join(
        [
            "## Realistic Validation",
            "",
            structured_marker,
            *format_waiver_lines,
            "The executing agent MUST run each item through the real entry "
            "point and save evidence (screenshots or captured output) to "
            "`.iar/evidence/` in the worktree. The runner refuses to publish "
            "without evidence.",
            "",
            *checklist_items,
        ]
    )


def validation_required(issue_body: str, config: AppConfig) -> bool:
    """Return True when the Issue demands evidence-backed validation."""
    if not config.validation.enabled:
        return False
    if has_validation_waiver_marker(issue_body):
        return False
    return bool(extract_realistic_validation_items(issue_body))


def build_validation_prompt_line(issue: IssueSummary, config: AppConfig) -> str:
    """Build the execution-prompt instruction enforcing real validation.

    Returns:
        指令文本；该 Issue 不要求证据时返回空字符串。
    """
    if not validation_required(issue.body, config):
        return ""
    if evidence_format_check_required(issue.body, config):
        enforcement_text = (
            "The runner checks evidence against the checklist before "
            "publishing: every item must have its own `rv-<n>-*` file, and "
            "when an item names an evidence format (截图/screenshot, pdf, "
            "txt, word, excel, csv, 录屏/video), a file with a matching "
            "suffix is required. "
        )
    else:
        enforcement_text = (
            "The runner refuses to publish when the evidence directory is " "empty. "
        )
    prompt_parts = [
        "Realistic Validation is MANDATORY for this Issue: actually execute "
        "every item of the Realistic Validation checklist through the real "
        "entry points (not only unit tests), and save one evidence file per "
        f"item into `{config.validation.evidence_dir}/` inside the worktree, "
        "named `rv-<item-number>-<slug>.<ext>` (PNG screenshots for UI "
        "behavior; captured terminal output as .txt for CLI behavior). "
        f"{enforcement_text}"
        "Do not substitute the real entry point an item describes with "
        "fakes, mocks, or TestClient. Never put evidence files under "
        "version control and never capture secrets in them."
    ]
    if has_structured_evidence_marker(issue.body):
        structured_suffix = build_structured_evidence_prompt_suffix(
            config.validation.language
        )
        prompt_parts.append(
            structured_suffix.format(evidence_dir=config.validation.evidence_dir)
        )
    return " ".join(prompt_parts)


# ---------------------------------------------------------------------------
# 证据目录：隔离与强制
# ---------------------------------------------------------------------------


def evidence_dir_path(worktree_path: Path, config: AppConfig) -> Path:
    """Return the absolute evidence directory path inside the worktree."""
    return worktree_path / config.validation.evidence_dir


def list_evidence_files(worktree_path: Path, config: AppConfig) -> list[Path]:
    """List first-level regular evidence files, sorted by name.

    隐藏文件（``.`` 开头）与子目录被忽略；v1 只收集证据目录第一层。
    """
    evidence_dir = evidence_dir_path(worktree_path, config)
    if not evidence_dir.is_dir():
        return []
    return sorted(
        candidate_path
        for candidate_path in evidence_dir.iterdir()
        if candidate_path.is_file() and not candidate_path.name.startswith(".")
    )


def ensure_evidence_dir_excluded(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> None:
    """Idempotently exclude the evidence dir via git ``info/exclude``.

    使用 ``git rev-parse --git-path info/exclude`` 解析排除文件位置
    （worktree 下指向 commondir，规则对主仓与所有 worktree 共享生效）。
    该文件是本地配置，不进入版本库，因此不会像修改 ``.gitignore``
    那样产生需要合并的代码变更。
    """
    exclude_path_result = process_runner.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=worktree_path,
        check=False,
    )
    exclude_path_text = exclude_path_result.stdout.strip()
    if exclude_path_result.return_code != 0 or not exclude_path_text:
        # 拿不到排除文件路径时降级为警告：发布前的
        # ensure_no_evidence_paths_in_changes 仍会拦截证据泄漏。
        _logger.warning(
            "Could not resolve git info/exclude for %s; evidence exclusion "
            "falls back to the publish guard.",
            worktree_path,
        )
        return
    exclude_path = Path(exclude_path_text)
    if not exclude_path.is_absolute():
        exclude_path = worktree_path / exclude_path
    if exclude_path.is_dir():
        _logger.warning(
            "Resolved info/exclude path is a directory (%s); skipping "
            "evidence exclusion.",
            exclude_path,
        )
        return
    exclude_line = f"/{config.validation.evidence_dir.strip('/')}/"
    existing_text = ""
    if exclude_path.exists():
        existing_text = exclude_path.read_text(encoding="utf-8")
    if exclude_line in existing_text.splitlines():
        return
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    appended_text = existing_text
    if appended_text and not appended_text.endswith("\n"):
        appended_text += "\n"
    appended_text += f"{exclude_line}\n"
    exclude_path.write_text(appended_text, encoding="utf-8")


def ensure_validation_evidence_ready(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
) -> None:
    """Require per-item evidence when the Issue demands validation.

    除了证据目录非空，还逐项核对清单：每个条目都要有 ``rv-<n>-*`` 文件，
    条目点名的格式（截图、pdf、txt、word……）必须有对应后缀的证据。
    逐项对账可全局关（``validation.evidence_format_check = false``）或
    按任务关（Issue body 带 ``iar:evidence-format-waived`` marker），
    关闭后退化为仅要求证据目录非空。

    对于带 ``iar:structured-evidence`` marker 的 Issue，额外校验
    ``evidence.json`` manifest：字段完整性、item 覆盖、证据文件存在性与
    编号一致性、语言一致性。

    Raises:
        ValidationEvidenceError: 要求验证但证据缺失或与清单不匹配。
    """
    if not validation_required(issue.body, config):
        return
    evidence_files = list_evidence_files(worktree_path, config)
    if not evidence_files:
        raise ValidationEvidenceError(
            "Realistic Validation evidence is required but "
            f"`{config.validation.evidence_dir}/` is empty or missing. "
            "Actually execute the PRD's Realistic Validation Plan through "
            "real entry points and save evidence files (PNG screenshots for "
            "UI behavior, captured terminal output as .txt for CLI behavior) "
            "named like `rv-1-<slug>.png` into that directory."
        )
    if has_structured_evidence_marker(issue.body):
        checklist_items = extract_realistic_validation_items(issue.body)
        validate_evidence_manifest(
            issue_body=issue.body,
            checklist_items=checklist_items,
            worktree_path=worktree_path,
            config=config,
        )
        return
    if not evidence_format_check_required(issue.body, config):
        return
    coverage_problems = collect_evidence_coverage_problems(
        extract_realistic_validation_items(issue.body),
        evidence_files,
        issue_body=issue.body,
    )
    if coverage_problems:
        problems_text = "\n".join(
            f"- {coverage_problem}" for coverage_problem in coverage_problems
        )
        raise ValidationEvidenceError(
            "Realistic Validation evidence does not match the checklist:\n"
            f"{problems_text}\n"
            "Each checklist item needs its own evidence file numbered "
            "`rv-<item-number>-<slug>.<ext>`, in the file format the item "
            "names (screenshot → image, pdf → .pdf, txt → .txt, and so on). "
            "Execute every item through the real entry point it describes — "
            "fakes, mocks, or TestClient substitutes do not satisfy the item."
        )


def format_validation_evidence_failure(message: str) -> str:
    """Build the failure section for an evidence recovery prompt."""
    return "\n".join(
        [
            "Realistic Validation evidence check failed.",
            message,
            "Run the validation plan for real and write the evidence files; "
            "do not fabricate evidence and do not capture secrets.",
        ]
    )


def ensure_no_evidence_paths_in_changes(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> None:
    """Refuse to publish when evidence paths leak into the code diff.

    ``info/exclude`` 已经阻止常规跟踪，本守卫拦截 ``git add -f`` 一类
    的强制加入，是发布前的双保险。
    """
    evidence_dir_prefix = config.validation.evidence_dir.strip("/") + "/"
    leaked_paths = [
        changed_path
        for changed_path in list_changed_paths(worktree_path, process_runner)
        if changed_path.startswith(evidence_dir_prefix)
    ]
    if leaked_paths:
        leaked_paths_text = ", ".join(sorted(set(leaked_paths)))
        raise RuntimeError(
            "Refusing to publish: validation evidence files must never enter "
            f"the code diff: {leaked_paths_text}"
        )


# ---------------------------------------------------------------------------
# PR body 勾选清单区块
# ---------------------------------------------------------------------------


def build_validation_checklist_block(checklist_items: list[str]) -> str:
    """Build the marker-wrapped human sign-off checklist for a PR body."""
    return "\n".join(
        [
            f"<!-- iar:realistic-validation version=1 total={len(checklist_items)} -->",
            "## Realistic Validation (human sign-off required)",
            "",
            "Review the evidence comment on this PR, then tick each item "
            "once you verified it against the evidence:",
            "",
            *checklist_items,
            "",
            _CHECKLIST_END_MARKER,
        ]
    )


def _find_checklist_block(pr_body: str) -> tuple[int, int, int] | None:
    """Locate the checklist block. Returns (start, end, declared_total)."""
    start_match = _CHECKLIST_START_PATTERN.search(pr_body)
    if not start_match:
        return None
    end_index = pr_body.find(_CHECKLIST_END_MARKER, start_match.end())
    if end_index == -1:
        end_index = len(pr_body)
    return start_match.start(), end_index, int(start_match.group("total"))


def parse_validation_checklist_state(pr_body: str) -> ValidationChecklistState | None:
    """Parse checkbox state inside the marker-wrapped PR body block."""
    block_location = _find_checklist_block(pr_body)
    if block_location is None:
        return None
    block_start, block_end, declared_total = block_location
    block_text = pr_body[block_start:block_end]
    checked_count = 0
    unchecked_count = 0
    for block_line in block_text.splitlines():
        if _CHECKED_ITEM_PATTERN.match(block_line):
            checked_count += 1
        elif _UNCHECKED_ITEM_PATTERN.match(block_line):
            unchecked_count += 1
    return ValidationChecklistState(
        total=max(declared_total, checked_count + unchecked_count),
        checked_count=checked_count,
        unchecked_count=unchecked_count,
    )


def reset_validation_checklist(pr_body: str) -> str:
    """Return the PR body with all block checkboxes reset to unchecked."""
    block_location = _find_checklist_block(pr_body)
    if block_location is None:
        return pr_body
    block_start, block_end, _declared_total = block_location
    block_text = pr_body[block_start:block_end]
    reset_lines = [
        re.sub(r"^(\s*[-*] )\[[xX]\] ", r"\1[ ] ", block_line)
        for block_line in block_text.splitlines()
    ]
    return pr_body[:block_start] + "\n".join(reset_lines) + pr_body[block_end:]


# ---------------------------------------------------------------------------
# 证据上传（orphan 分支）与 PR 证据评论
# ---------------------------------------------------------------------------


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
    evidence_files = list_evidence_files(worktree_path, config)
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
        checklist_items = extract_realistic_validation_items(issue_body)
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
            evidence_file_path = evidence_dir_path(worktree_path, config) / file_name
            try:
                file_text = evidence_file_path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                file_text = "[unreadable evidence file]"
            comment_lines.append("```text")
            comment_lines.append(_truncate_inline_evidence(file_text.rstrip()))
            comment_lines.append("```")
        if file_blob_url:
            comment_lines.append(f"[Open file]({file_blob_url})")
    return "\n".join(comment_lines)


def parse_latest_evidence_marker(pr_comments: list[str]) -> EvidenceMarker | None:
    """Parse the latest iar:validation-evidence marker from PR comments."""
    for comment_body in reversed(pr_comments):
        marker_match = _EVIDENCE_MARKER_PATTERN.search(comment_body)
        if marker_match:
            return EvidenceMarker(
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
    if not validation_required(issue.body, config):
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
        raise RuntimeError(
            f"Cannot post validation evidence: unparsable PR URL {pr_url!r}"
        )
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


# ---------------------------------------------------------------------------
# daemon 软门禁
# ---------------------------------------------------------------------------


def build_validation_passed_comment(*, head_sha: str, pr_url: str) -> str:
    """Build the audit comment for a fully signed-off checklist."""
    marker = format_event_marker(
        phase="validation_passed",
        cycle=1,
        head_sha=head_sha,
    )
    return "\n".join(
        [
            marker,
            "",
            "## Realistic Validation Signed Off",
            "",
            f"- PR: {pr_url}",
            f"- Head SHA at sign-off: `{head_sha}`",
            "",
            "A human reviewer verified the validation evidence and ticked "
            "every Realistic Validation checklist item.",
        ]
    )


def build_validation_reset_comment(*, head_sha: str, evidence_head: str) -> str:
    """Build the notice comment posted when sign-off goes stale."""
    marker = format_event_marker(
        phase="validation_reset",
        cycle=1,
        head_sha=head_sha,
    )
    return "\n".join(
        [
            marker,
            "",
            "## Realistic Validation Sign-off Reset",
            "",
            f"- New commits were pushed after evidence was captured at "
            f"`{evidence_head}` (PR head is now `{head_sha}`).",
            "- The checklist has been unticked. Fresh evidence and a new "
            "human sign-off are required before merge.",
        ]
    )


def _ensure_issue_validation_labels(
    *,
    issue: IssueSummary,
    config: AppConfig,
    github_client: IGitHubClient,
    target_passed: bool,
) -> None:
    """Idempotently converge Issue validation labels to the target state."""
    pending_label = config.labels.validation_pending
    passed_label = config.labels.validation_passed
    desired_label = passed_label if target_passed else pending_label
    obsolete_label = pending_label if target_passed else passed_label
    if desired_label in issue.labels and obsolete_label not in issue.labels:
        return
    github_client.edit_issue_labels(
        issue.number,
        add=[desired_label],
        remove=[obsolete_label],
    )


def _gate_single_issue(
    *,
    issue: IssueSummary,
    config: AppConfig,
    github_client: IGitHubClient,
) -> None:
    """Run the validation soft gate for one ``agent/review`` Issue."""
    issue_comments = github_client.list_issue_comments(issue.number)
    lifecycle_marker = parse_latest_event_marker(issue_comments)
    pr_branch = lifecycle_marker.pr_branch if lifecycle_marker else None
    if not pr_branch:
        return
    pr_context: PullRequestContext | None = github_client.get_pull_request_context(
        pr_branch
    )
    if pr_context is None or pr_context.number is None:
        return
    checklist_state = parse_validation_checklist_state(pr_context.body)
    if checklist_state is None or checklist_state.total == 0:
        return

    if checklist_state.unchecked_count > 0:
        _ensure_issue_validation_labels(
            issue=issue,
            config=config,
            github_client=github_client,
            target_passed=False,
        )
        return

    pr_comments = github_client.list_pr_comments(pr_context.number)
    evidence_marker = parse_latest_evidence_marker(pr_comments)
    if evidence_marker is not None and evidence_marker.head_sha != pr_context.head_sha:
        # 勾选后又有新 push：重置勾选，要求基于新 head 的证据与重新签收。
        github_client.update_pull_request_body(
            pr_context.number,
            reset_validation_checklist(pr_context.body),
        )
        github_client.comment_pr(
            pr_context.number,
            build_validation_reset_comment(
                head_sha=pr_context.head_sha,
                evidence_head=evidence_marker.head_sha,
            ),
        )
        _ensure_issue_validation_labels(
            issue=issue,
            config=config,
            github_client=github_client,
            target_passed=False,
        )
        return

    _ensure_issue_validation_labels(
        issue=issue,
        config=config,
        github_client=github_client,
        target_passed=True,
    )
    audit_marker = parse_latest_event_marker_for_phases(
        issue_comments, {"validation_passed", "validation_reset"}
    )
    already_audited = (
        audit_marker is not None
        and audit_marker.phase == "validation_passed"
        and audit_marker.head_sha == pr_context.head_sha
    )
    if not already_audited:
        github_client.comment_issue(
            issue.number,
            build_validation_passed_comment(
                head_sha=pr_context.head_sha,
                pr_url=pr_context.pr_url,
            ),
        )


def cleanup_closed_issue_evidence_branches(
    *,
    repo_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> None:
    """Delete remote evidence branches whose Issues are closed."""
    branch_prefix = config.validation.branch_prefix
    ls_remote_result = process_runner.run(
        [
            "git",
            "ls-remote",
            "--heads",
            config.git.remote,
            f"refs/heads/{branch_prefix}*",
        ],
        cwd=repo_path,
        check=False,
    )
    if ls_remote_result.return_code != 0:
        return
    branch_issue_pattern = re.compile(
        rf"refs/heads/({re.escape(branch_prefix)}issue-(\d+))$"
    )
    for ls_remote_line in ls_remote_result.stdout.splitlines():
        branch_match = branch_issue_pattern.search(ls_remote_line.strip())
        if not branch_match:
            continue
        branch_ref_name = branch_match.group(1)
        issue_number = int(branch_match.group(2))
        try:
            tracked_issue = github_client.get_issue(issue_number)
        except Exception as lookup_exc:  # noqa: BLE001 - cleanup is best effort.
            _logger.info(
                "Skipping evidence branch cleanup for #%d: %s",
                issue_number,
                lookup_exc,
            )
            continue
        if tracked_issue.state.upper() != "CLOSED":
            continue
        process_runner.run(
            ["git", "push", config.git.remote, "--delete", branch_ref_name],
            cwd=repo_path,
            check=False,
        )
        _logger.info(
            "Deleted evidence branch %s for closed Issue #%d.",
            branch_ref_name,
            issue_number,
        )


def process_validation_gate(
    *,
    repo_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    max_issues: int = 20,
) -> None:
    """Run the soft validation gate across review-stage Issues.

    每个轮询周期调用一次；单个 Issue 的失败不阻断其余 Issue 与清理。
    """
    if not config.validation.enabled:
        return
    review_issues = github_client.list_review_candidate_issues(
        [config.labels.review], max_issues
    )
    for review_issue in review_issues:
        try:
            _gate_single_issue(
                issue=review_issue,
                config=config,
                github_client=github_client,
            )
        except Exception as gate_exc:  # noqa: BLE001 - gate must not break polling.
            _logger.error(
                "Validation gate failed for Issue #%d: %s",
                review_issue.number,
                gate_exc,
            )
    try:
        cleanup_closed_issue_evidence_branches(
            repo_path=repo_path,
            config=config,
            github_client=github_client,
            process_runner=process_runner,
        )
    except Exception as cleanup_exc:  # noqa: BLE001 - cleanup is best effort.
        _logger.error("Evidence branch cleanup failed: %s", cleanup_exc)
