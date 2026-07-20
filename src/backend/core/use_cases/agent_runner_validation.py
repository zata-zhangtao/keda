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

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

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
    VISUAL_EVIDENCE_SUFFIXES,
    collect_evidence_coverage_problems,
    demanded_evidence_kinds as demanded_evidence_kinds,
    extract_evidence_format_markers as extract_evidence_format_markers,
)
from backend.core.use_cases.agent_runner_git import has_changes, list_changed_paths
from backend.core.use_cases.agent_runner_structured_evidence import (
    EvidenceUpload,
    ValidationEvidenceError,
    build_evidence_blob_url,
    build_structured_evidence_prompt_suffix,
    format_structured_evidence_marker,
    has_structured_evidence_marker,
    load_evidence_manifest,
    render_structured_evidence_comment,
    validate_evidence_artifacts,
    validate_evidence_manifest,
)

_logger = logging.getLogger(__name__)

_VALIDATION_SECTION_TITLE = "realistic validation"
_VALIDATION_SECTION_HEADER_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?" + re.escape(_VALIDATION_SECTION_TITLE),
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
_MISPLACED_EVIDENCE_HELPER_PREFIXES = (
    "scripts_evidence/",
    "scripts/evidence/",
    "scripts/evidence_helpers/",
)
_REUSABLE_RV_SCRIPT_PREFIX = "scripts/rv_evidence/"
_REUSABLE_RV_SCRIPT_PATH_PATTERN = re.compile(r"^scripts/rv_evidence/rv-\d+-[^/]+$")


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
    支持多级编号前缀（``7.6 Realistic Validation Plan``），标题文本以
    ``Realistic Validation`` 开头（大小写不敏感）即进入小节，遇到同级或
    更高级标题退出。围栏代码块（``` fenced）内的行按内容收集、不当作标题
    解析——否则 YAML 注释行（``# ...``）会被误判为标题而提前截断小节。
    """
    section_lines: list[str] = []
    section_heading_level = 0
    in_code_fence = False
    for line in markdown_text.splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith("```"):
            in_code_fence = not in_code_fence
            if section_heading_level:
                section_lines.append(line)
            continue
        if not in_code_fence:
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


def _extract_rv_oracle_entries(section_lines: list[str]) -> list[dict[str, object]]:
    """Deterministically parse the structured YAML oracle block.

    在 Realistic Validation 小节内定位第一个 ```yaml 围栏，``yaml.safe_load``
    后要求是一个非空的 mapping 列表且每项含 ``id`` 与 ``behavior``。无围栏、
    解析失败或结构不符时返回空列表，由调用方回退到旧式 checkbox 解析。
    本函数不引入 LLM，纯确定性解析。
    """
    fence_open = False
    yaml_lines: list[str] = []
    for line in section_lines:
        stripped_line = line.strip()
        if stripped_line.startswith("```"):
            if fence_open:
                break
            if stripped_line.lower().startswith("```yaml"):
                fence_open = True
            continue
        if fence_open:
            yaml_lines.append(line)
    if not yaml_lines:
        return []
    try:
        parsed_block = yaml.safe_load("\n".join(yaml_lines))
    except yaml.YAMLError:
        _logger.warning("RV oracle YAML block present but failed to parse; ignoring.")
        return []
    if not isinstance(parsed_block, list):
        return []
    oracle_entries: list[dict[str, object]] = []
    for entry in parsed_block:
        if isinstance(entry, dict) and entry.get("id") and entry.get("behavior"):
            oracle_entries.append(entry)
    return oracle_entries


def extract_realistic_validation_items(markdown_text: str) -> list[str]:
    """Extract validation checklist items from the Realistic Validation section.

    优先解析结构化 YAML oracle 块（每项 ``id`` + ``behavior``），映射为规范化
    复选框 ``- [ ] <id>: <behavior>``；无 oracle 块时回退解析旧式 ``- [ ]``
    checkbox 行。勾选状态一律规范化为未勾选，因为清单代表的是*待人工确认*项。

    Args:
        markdown_text: PRD 全文或 Issue body。

    Returns:
        规范化后的 Markdown 复选框行列表；无小节或无条目时为空列表。
    """
    section_lines = _iterate_validation_section_lines(markdown_text)
    oracle_entries = _extract_rv_oracle_entries(section_lines)
    if oracle_entries:
        return [f"- [ ] {entry['id']}: {entry['behavior']}" for entry in oracle_entries]
    checklist_items: list[str] = []
    for section_line in section_lines:
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
        enforcement_text = "The runner refuses to publish when the evidence directory is empty. "
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
        "version control and never capture secrets in them. "
        f"Keep scripts used only to capture evidence or provide temporary RV setup "
        f"under `{config.validation.evidence_dir}/scripts/`; they must not enter "
        "the code diff. A reusable script may be committed only when the PRD "
        "requires it for a reproducible RV command, and then it must live under "
        "`scripts/rv_evidence/` with an `rv-<item-number>-<slug>` name. Do not "
        "create `scripts_evidence/`, `scripts/evidence/`, or "
        "`scripts/evidence_helpers/`. Before requesting a commit, inspect `git "
        "diff --name-only` and remove temporary evidence helpers."
    ]
    if has_structured_evidence_marker(issue.body):
        structured_suffix = build_structured_evidence_prompt_suffix(config.validation.language)
        prompt_parts.append(structured_suffix.format(evidence_dir=config.validation.evidence_dir))
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
    """Idempotently exclude the evidence dir and RV cache via git ``info/exclude``.

    除证据目录外,同样排除 RV 复跑缓存文件(:func:`_rv_reexec_cache_relpath`),
    避免它让工作区显示为脏或泄漏进代码 diff。

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
            "Resolved info/exclude path is a directory (%s); skipping evidence exclusion.",
            exclude_path,
        )
        return
    evidence_line = f"/{config.validation.evidence_dir.strip('/')}/"
    cache_line = f"/{_rv_reexec_cache_relpath(config)}"
    desired_lines = [evidence_line, cache_line]
    existing_text = ""
    if exclude_path.exists():
        existing_text = exclude_path.read_text(encoding="utf-8")
    existing_lines = existing_text.splitlines()
    missing_lines = [line for line in desired_lines if line not in existing_lines]
    if not missing_lines:
        return
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    appended_text = existing_text
    if appended_text and not appended_text.endswith("\n"):
        appended_text += "\n"
    appended_text += "".join(f"{line}\n" for line in missing_lines)
    exclude_path.write_text(appended_text, encoding="utf-8")


def _path_touches_frontend(changed_path: str, frontend_paths: tuple[str, ...]) -> bool:
    """判断变更路径是否落在任一前端目录前缀下（按路径段匹配，非裸子串）。

    Args:
        changed_path (str): git status 报出的单个仓库相对路径。
        frontend_paths (tuple[str, ...]): 前端目录前缀列表。

    Returns:
        bool: 命中任一前端前缀返回 True。
    """
    normalized_path = changed_path.strip()
    for raw_prefix in frontend_paths:
        prefix = raw_prefix.strip().strip("/")
        if prefix and (normalized_path == prefix or normalized_path.startswith(prefix + "/")):
            return True
    return False


def ensure_frontend_visual_evidence(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner | None = None,
) -> None:
    """前端改动强制真实视觉证据的 fail-closed 门禁。

    当 worktree 的 git 变更命中 ``config.validation.frontend_paths`` 中任一
    目录前缀时，证据目录（第一层）必须至少有一个视觉证据文件（图片/视频，
    见 ``VISUAL_EVIDENCE_SUFFIXES``），否则抛 ``ValidationEvidenceError``，
    由既有 recovery 循环接管。

    判定依据是"改了什么"（git diff）而非清单文本关键字，因此覆盖"前端 RV
    条目文本不含'截图'导致逐项检查漏判"的盲区；本门禁独立于
    ``verifier_enabled``。``process_runner`` 为 ``None`` 时（旧调用方未接线）
    跳过，避免破坏兼容。

    Args:
        issue (IssueSummary): 当前处理的 Issue。
        worktree_path (Path): worktree 根目录。
        config (AppConfig): 运行配置。
        process_runner (IProcessRunner | None): 命令执行端口；None 时跳过。

    Raises:
        ValidationEvidenceError: 前端改动但证据目录缺少视觉证据文件。
    """
    if process_runner is None:
        return
    if not config.validation.frontend_visual_evidence_required:
        return
    if not validation_required(issue.body, config):
        return
    frontend_paths = tuple(config.validation.frontend_paths)
    if not frontend_paths:
        return
    changed_paths = list_changed_paths(worktree_path, process_runner)
    touched_frontend_paths = [
        changed_path
        for changed_path in changed_paths
        if _path_touches_frontend(changed_path, frontend_paths)
    ]
    if not touched_frontend_paths:
        return
    evidence_files = list_evidence_files(worktree_path, config)
    if any(
        evidence_file.suffix.lower() in VISUAL_EVIDENCE_SUFFIXES for evidence_file in evidence_files
    ):
        return
    accepted_suffixes_text = "/".join(sorted(VISUAL_EVIDENCE_SUFFIXES))
    touched_preview = ", ".join(sorted(touched_frontend_paths)[:5])
    raise ValidationEvidenceError(
        "Frontend changes were made but no visual evidence "
        f"({accepted_suffixes_text}) exists in "
        f"`{config.validation.evidence_dir}/`. Changed frontend paths: "
        f"{touched_preview}. Run the target repo's UI/e2e entry point and save "
        "at least one real screenshot or screen recording into the evidence "
        "directory; a text log does not prove a UI change."
    )


def ensure_validation_evidence_ready(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner | None = None,
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

    当 ``config.validation.artifact_health_enabled`` 为真且 ``process_runner``
    被提供时,额外对 manifest 声明的 ``expected_artifacts`` 跑硬层健全性卡点
    (mime/size/duration/mtime),FR-11a。

    Raises:
        ValidationEvidenceError: 要求验证但证据缺失或与清单不匹配。
    """
    if not validation_required(issue.body, config):
        return
    # 前端改动强制视觉证据（fail-closed，按 diff 判定，独立于 verifier）。
    ensure_frontend_visual_evidence(issue, worktree_path, config, process_runner)
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
        # FR-11a: artifact health hard layer (machine-checkable assertions).
        # Skip when process_runner is None (caller did not wire it) to keep the
        # legacy non-structured callers working.
        if config.validation.artifact_health_enabled and process_runner is not None:
            manifest = load_evidence_manifest(worktree_path, config)
            validate_evidence_artifacts(
                manifest,
                worktree_path,
                config,
                process_runner,
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
        problems_text = "\n".join(f"- {coverage_problem}" for coverage_problem in coverage_problems)
        raise ValidationEvidenceError(
            "Realistic Validation evidence does not match the checklist:\n"
            f"{problems_text}\n"
            "Each checklist item needs its own evidence file numbered "
            "`rv-<item-number>-<slug>.<ext>`, in the file format the item "
            "names (screenshot → image, pdf → .pdf, txt → .txt, and so on). "
            "Execute every item through the real entry point it describes — "
            "fakes, mocks, or TestClient substitutes do not satisfy the item."
        )


def _rv_reexec_cache_relpath(config: AppConfig) -> str:
    """Worktree-relative path of the RV re-execution cache file.

    Placed beside the evidence dir but outside it, so RV scripts that wipe
    their own ``rv-*`` evidence on each run never clear the cache.
    """
    evidence_dir = Path(config.validation.evidence_dir.strip("/"))
    parent = evidence_dir.parent
    base = parent if str(parent) not in (".", "") else Path(".iar")
    return (base / "rv_reexec_cache.json").as_posix()


def _rv_reexec_cache_path(worktree_path: Path, config: AppConfig) -> Path:
    """Absolute path of the RV re-execution cache inside ``worktree_path``."""
    return worktree_path / _rv_reexec_cache_relpath(config)


def _clean_tree_fingerprint(worktree_path: Path, process_runner: IProcessRunner) -> str | None:
    """Return ``HEAD`` 的 tree SHA(工作区干净时),否则 ``None``。

    tree SHA 是已提交代码的纯内容指纹(不含提交时间/作者/message)。工作区
    一旦脏——有未提交的已跟踪改动,或非排除的 untracked 文件——返回 ``None``,
    让调用方照常复跑而非信任过期的通过结果;v1 只对完全已提交的状态做缓存。
    """
    if has_changes(worktree_path, process_runner):
        return None
    result = process_runner.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=worktree_path,
        check=False,
    )
    tree_sha = result.stdout.strip()
    if result.return_code != 0 or not tree_sha:
        return None
    return tree_sha


def _rv_reexec_cache_key(tree_fingerprint: str, item_number: int, command: str) -> str:
    """Cache key 绑定"某命令在某代码树上、对某 item 已通过"。

    键里含命令的哈希:在(gitignore 的)manifest 里改命令不会改 tree SHA,
    但会改命令哈希 → 缓存未命中 → 照常复跑,不会用旧命令的结果蒙混。
    """
    command_digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
    return f"{tree_fingerprint}|{item_number}|{command_digest}"


def _load_rv_reexec_cache(cache_path: Path) -> dict[str, str]:
    """Load the RV re-exec cache entries; tolerate a missing/corrupt file."""
    if not cache_path.exists():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return entries if isinstance(entries, dict) else {}


def _save_rv_reexec_cache(cache_path: Path, entries: dict[str, str]) -> None:
    """Persist the RV re-exec cache entries as json."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ensure_validation_commands_pass(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> None:
    """Re-run each structured-evidence item's command and require it to pass.

    keda 以自己复跑的退出码为准,而不是只信 agent 写的证据文件——这样
    "测试通过但功能其实坏了 / agent 没真跑" 无法蒙混过关。仅对带
    ``iar:structured-evidence`` marker、要求验证、且开启 ``reexecute_commands``
    的 Issue 生效。命令经 ``bash -lc`` 在 worktree 内执行并带超时;非零退出
    或超时即判失败,抛 ``ValidationEvidenceError`` 进入既有 recovery 循环。

    当 ``reexecute_cache_enabled`` 开启且工作区干净时,按 ``HEAD`` 的 tree SHA
    指纹缓存"该 item 的该命令已通过":同一份已提交代码再次进入(如
    blocked-continue、换 agent、重新 claim)直接跳过复跑,避免重复跑 e2e。
    工作区一旦脏(有未提交改动)即不读不写缓存、照常复跑。证据文件是否齐全
    仍由 ``ensure_validation_evidence_ready`` 单独把关,缓存命中不绕过它。

    Raises:
        ValidationEvidenceError: 任一命令被 keda 复跑后未通过或超时。
    """
    if not config.validation.reexecute_commands:
        return
    if not validation_required(issue.body, config):
        return
    if not has_structured_evidence_marker(issue.body):
        return

    manifest = load_evidence_manifest(worktree_path, config)
    timeout_seconds = config.validation.reexecute_timeout_seconds

    tree_fingerprint = (
        _clean_tree_fingerprint(worktree_path, process_runner)
        if config.validation.reexecute_cache_enabled
        else None
    )
    cache_path = _rv_reexec_cache_path(worktree_path, config)
    cache_entries = _load_rv_reexec_cache(cache_path) if tree_fingerprint else {}
    newly_passed: dict[str, str] = {}

    for block in manifest.items:
        cache_key = (
            _rv_reexec_cache_key(tree_fingerprint, block.item_number, block.command)
            if tree_fingerprint
            else None
        )
        if cache_key is not None and cache_key in cache_entries:
            _logger.info(
                "Realistic Validation item %s: skipping re-execution; command "
                "already passed at tree %s.",
                block.item_number,
                tree_fingerprint,
            )
            continue
        try:
            result = process_runner.run(
                ["bash", "-lc", block.command],
                cwd=worktree_path,
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
                label=f"rv-reexec-{block.item_number}",
            )
        except subprocess.TimeoutExpired as timeout_error:
            raise ValidationEvidenceError(
                f"Realistic Validation item {block.item_number} timed out when keda "
                f"re-ran its command (>{timeout_seconds}s): `{block.command}`. The "
                "reproducible command must be a self-terminating check that probes the "
                "real entry point and exits, not a long-running server. Set "
                "`validation.reexecute_commands=false` to opt out."
            ) from timeout_error
        if result.return_code != 0:
            raise ValidationEvidenceError(
                f"Realistic Validation item {block.item_number} failed when keda "
                f"re-ran its command: `{block.command}` exited {result.return_code}. "
                "keda re-executes RV commands to confirm they actually pass — the "
                "agent's evidence file alone is not trusted. Fix the behavior so the "
                "command passes (or correct the command). Set "
                "`validation.reexecute_commands=false` to opt out."
            )
        if cache_key is not None:
            newly_passed[cache_key] = datetime.now(timezone.utc).isoformat()

    if newly_passed:
        cache_entries.update(newly_passed)
        _save_rv_reexec_cache(cache_path, cache_entries)


def format_validation_evidence_detail(message: str) -> str:
    """Build the recorded attempt detail for a validation-evidence failure.

    Keeps the specific failure ``message`` as the last line so the attempt
    history Detail column surfaces the real reason — the table summarizer
    (``_summarize_attempt_detail``) keeps the last informative line. The
    generic "run it for real" instruction belongs only in the recovery prompt
    (:func:`format_validation_evidence_failure`), never in the diagnostic
    record, where appending it as the last line would mask the actual cause.
    """
    return "\n".join(
        [
            "Realistic Validation evidence check failed.",
            message,
        ]
    )


def format_validation_evidence_failure(message: str, evidence_dir: str = ".iar/evidence") -> str:
    """Build the failure section for an evidence recovery prompt."""
    return "\n".join(
        [
            format_validation_evidence_detail(message),
            "Run the validation plan for real and write the evidence files; "
            "do not fabricate evidence and do not capture secrets. Keep scripts used "
            f"only for evidence capture or temporary RV setup under `{evidence_dir}/scripts/` "
            "and out of the code diff. Only PRD-required reusable RV scripts may be "
            "committed, under `scripts/rv_evidence/` named `rv-<item-number>-<slug>`; "
            "never create `scripts_evidence/`, `scripts/evidence/`, or "
            "`scripts/evidence_helpers/`.",
        ]
    )


def ensure_no_misplaced_evidence_helpers(
    worktree_path: Path,
    process_runner: IProcessRunner,
) -> None:
    """拒绝把取证辅助脚本放入未授权的受版本控制路径。

    复杂且需要复跑的 RV 命令可以作为交付物保留在
    ``scripts/rv_evidence/``。除此之外，证据采集与临时 setup 脚本必须留在
    worktree-local evidence directory；本检查拦截几种曾被 Agent 误建的路径，
    让它们进入既有 recovery 循环而不是污染待发布的变更。

    Args:
        worktree_path: 当前 Agent worktree 根目录。
        process_runner: 用于读取 worktree 变更的进程执行器。

    Raises:
        ValidationEvidenceError: 发现未授权的取证辅助脚本路径时抛出。
    """
    changed_paths = list_changed_paths(worktree_path, process_runner)
    misplaced_paths: list[str] = []
    for changed_path in changed_paths:
        if changed_path.startswith(_MISPLACED_EVIDENCE_HELPER_PREFIXES):
            misplaced_paths.append(changed_path)
            continue
        if not changed_path.startswith(_REUSABLE_RV_SCRIPT_PREFIX):
            continue
        if _REUSABLE_RV_SCRIPT_PATH_PATTERN.fullmatch(changed_path):
            continue
        candidate_path = worktree_path / changed_path
        if changed_path.endswith("/") and candidate_path.is_dir():
            invalid_script_paths = [
                str(script_path.relative_to(worktree_path))
                for script_path in candidate_path.rglob("*")
                if script_path.is_file()
                and not _REUSABLE_RV_SCRIPT_PATH_PATTERN.fullmatch(
                    str(script_path.relative_to(worktree_path))
                )
            ]
            misplaced_paths.extend(invalid_script_paths)
            continue
        misplaced_paths.append(changed_path)
    if not misplaced_paths:
        return
    misplaced_paths_text = ", ".join(sorted(set(misplaced_paths)))
    raise ValidationEvidenceError(
        "Validation-only helper scripts are in unsupported tracked paths or use "
        "an invalid reusable RV script name: "
        f"{misplaced_paths_text}. Move temporary helpers to `.iar/evidence/scripts/` "
        "and keep them out of the diff. A PRD-required reusable RV script belongs "
        "under `scripts/rv_evidence/` named `rv-<item-number>-<slug>` instead."
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
                file_text = evidence_file_path.read_text(encoding="utf-8", errors="replace")
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
    pr_context: PullRequestContext | None = github_client.get_pull_request_context(pr_branch)
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
        # FR-5/FR-6: head 漂移使旧 verifier verdict 失效——verifier 当初跑的是
        # 旧 SHA,新 commit 没被独立验证过。清掉 validation/verifier-passed,
        # 防止 autopilot 合并队列误判"新 commit 也过了 verifier"。verifier 只在
        # pre-PR 跑,daemon 不重跑;label 保持清除直到 issue 重新走 builder→verifier。
        if config.labels.verifier_passed in issue.labels:
            github_client.edit_issue_labels(issue.number, remove=[config.labels.verifier_passed])
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
    branch_issue_pattern = re.compile(rf"refs/heads/({re.escape(branch_prefix)}issue-(\d+))$")
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
    review_issues = github_client.list_review_candidate_issues([config.labels.review], max_issues)
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
