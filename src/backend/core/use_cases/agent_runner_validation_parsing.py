"""Realistic Validation 的 Markdown 解析与 prompt 文本构造。

从 :mod:`agent_runner_validation` 拆出的第一块（"validators"）：把 PRD / Issue
body 里的 ``Realistic Validation`` 清单、``Validation Waiver`` 与
``Evidence Format Waiver`` 声明解析出来，并据此判定该 Issue 是否要求验证、
以及生成注入给 Agent 的执行指令。

这里只做**文本进、文本出**——不碰文件系统、不跑子进程、不读证据目录。证据
目录的隔离与强制在 :mod:`agent_runner_validation` 本体，PR body 勾选清单在
:mod:`agent_runner_validation_checklist`。
"""

from __future__ import annotations

import logging
import re

import yaml

from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
)
from backend.core.use_cases.agent_runner_structured_evidence import (
    build_structured_evidence_prompt_suffix,
    format_structured_evidence_marker,
    has_structured_evidence_marker,
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

EVIDENCE_ORACLE_SUBDIR = "scripts"
"""证据目录下存放 RV oracle 脚本的子目录名。

定义在这里而不是 :mod:`agent_runner_validation`,是因为 prompt 文本要用它,
而依赖方向只能是 validation -> parsing;放反了会成环。
"""

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
        f"EVERY RV script — evidence capture, temporary setup, and reproducible "
        f"oracles referenced by an `evidence.json` command alike — belongs under "
        f"`{config.validation.evidence_dir}/{EVIDENCE_ORACLE_SUBDIR}/`. There is no "
        "exception: no RV script may enter the code diff, whatever the PRD asks for. "
        "These scripts are uploaded to the evidence branch for reviewers, so they "
        "must not contain secrets either. Before requesting a commit, inspect `git "
        "diff --name-only` and remove every RV script from the change set."
    ]
    if has_structured_evidence_marker(issue.body):
        structured_suffix = build_structured_evidence_prompt_suffix(config.validation.language)
        prompt_parts.append(structured_suffix.format(evidence_dir=config.validation.evidence_dir))
    return " ".join(prompt_parts)
