"""Structured evidence manifest parsing, validation and rendering.

本模块负责 Realistic Validation 的**结构化证据**能力：

- 解析并物化 ``iar:structured-evidence`` hidden marker。
- 读取 ``.iar/evidence/evidence.json`` manifest。
- 校验 manifest 字段完整性、item 覆盖、证据文件存在性与编号一致性。
- 计算证据文件 SHA-256，并按 checklist item 分组渲染 PR evidence comment。
- 提供执行 prompt 与 recovery prompt 中使用的 manifest 要求后缀。

所有文本 I/O 显式使用 ``encoding="utf-8"``；JSON / hash 仅使用标准库。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.models.agent_runner import AppConfig
from backend.core.use_cases.agent_runner_evidence_format import (
    IMAGE_EVIDENCE_SUFFIXES,
)

_STRUCTURED_EVIDENCE_MARKER_PATTERN = re.compile(
    r"<!--\s*iar:structured-evidence\s+"
    r"version=(?P<version>\d+)\s+"
    r'language="(?P<language>[^"]+)"\s*-->'
)
_EVIDENCE_ITEM_FILE_PATTERN = re.compile(r"^rv-(?P<item>\d+)[-.]", re.IGNORECASE)
_EVIDENCE_ITEM_SECTION_PATTERN = re.compile(
    r"\[\s*Item\s+(?P<item>\d+)(?P<sub>[a-z]?)\s*\]",
    re.IGNORECASE,
)
_PR_URL_PATTERN = re.compile(
    r"https?://[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)
_MAX_INLINE_EVIDENCE_CHARS = 3000


class ValidationEvidenceError(RuntimeError):
    """Raised when required Realistic Validation evidence is missing or invalid."""


@dataclass(frozen=True)
class EvidenceUpload:
    """Result of pushing evidence files to the orphan evidence branch."""

    branch: str
    commit_sha: str
    file_names: tuple[str, ...]


@dataclass(frozen=True)
class StructuredEvidenceMarker:
    """Parsed ``iar:structured-evidence`` hidden marker from an Issue body."""

    version: int
    language: str


@dataclass(frozen=True)
class EvidenceBlock:
    """A single checklist item's structured evidence block from the manifest.

    ``negative_control`` / ``expected_fail`` 承载"红→绿"判别力证据：能让该项
    变红的命令或注入故障,以及变红时的样子。当前为可选字段（向后兼容旧
    manifest）;门禁层按配置决定是否对高证据项强制要求。
    """

    item_number: int
    item_name: str
    command: str
    evidence_files: tuple[str, ...]
    output_summary: str
    explanation: str
    risks: str
    negative_control: str = ""
    expected_fail: str = ""


@dataclass(frozen=True)
class EvidenceManifest:
    """Top-level structured evidence manifest."""

    version: int
    language: str
    items: tuple[EvidenceBlock, ...]


@dataclass(frozen=True)
class EvidenceFileInfo:
    """Evidence file with runner-computed SHA-256."""

    file_name: str
    sha256: str


@dataclass(frozen=True)
class StructuredEvidenceItemReport:
    """Validated evidence block plus file hashes."""

    block: EvidenceBlock
    files: tuple[EvidenceFileInfo, ...]


@dataclass(frozen=True)
class StructuredEvidenceReport:
    """Full validation report ready for comment rendering."""

    language: str
    items: tuple[StructuredEvidenceItemReport, ...]


_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "title": "Realistic Validation Evidence",
        "branch": "证据分支",
        "head": "捕获代码版本",
        "language": "语言",
        "sign_off": "审阅证据后，在 PR 正文的 Realistic Validation 清单中逐项勾选。",
        "reproducible_command": "可复现命令",
        "evidence_files": "证据文件",
        "sha256": "SHA-256",
        "output_summary": "关键输出摘要",
        "explanation": "为什么能证明该检查点成立",
        "risks": "潜在风险 / 不适用说明",
        "open_file": "打开文件",
        "image_alt": "证据图片",
        "truncated": "[内容已截断；请在证据分支打开完整文件]",
        "unreadable": "[无法读取证据文件]",
    },
    "en-US": {
        "title": "Realistic Validation Evidence",
        "branch": "Evidence branch",
        "head": "Code head at capture time",
        "language": "Language",
        "sign_off": "Review the evidence below, then tick each item in the PR body Realistic Validation checklist.",
        "reproducible_command": "Reproducible command",
        "evidence_files": "Evidence files",
        "sha256": "SHA-256",
        "output_summary": "Key output summary",
        "explanation": "Why this satisfies the checkpoint",
        "risks": "Potential risks / not-applicable notes",
        "open_file": "Open file",
        "image_alt": "Evidence image",
        "truncated": "[evidence truncated; open the file on the evidence branch]",
        "unreadable": "[unreadable evidence file]",
    },
}


def _label(language: str, key: str) -> str:
    """Return a localized fixed label, falling back to English."""
    return _LABELS.get(language, _LABELS["en-US"]).get(key, _LABELS["en-US"][key])


def format_structured_evidence_marker(language: str) -> str:
    """Format the hidden structured evidence marker for an Issue body."""
    return f'<!-- iar:structured-evidence version=1 language="{language}" -->'


def parse_structured_evidence_marker(
    issue_body: str,
) -> StructuredEvidenceMarker | None:
    """Parse the latest ``iar:structured-evidence`` marker from an Issue body."""
    marker_match = None
    for marker_match in _STRUCTURED_EVIDENCE_MARKER_PATTERN.finditer(issue_body):
        pass
    if marker_match is None:
        return None
    return StructuredEvidenceMarker(
        version=int(marker_match.group("version")),
        language=marker_match.group("language"),
    )


def has_structured_evidence_marker(issue_body: str) -> bool:
    """Return True when the Issue body carries a structured evidence marker."""
    return parse_structured_evidence_marker(issue_body) is not None


def _evidence_dir_path(worktree_path: Path, config: AppConfig) -> Path:
    """Return the absolute evidence directory path inside the worktree."""
    return worktree_path / config.validation.evidence_dir


def _load_manifest_json(worktree_path: Path, config: AppConfig) -> dict[str, object]:
    """Load and parse ``evidence.json`` as a Python dict."""
    evidence_dir = _evidence_dir_path(worktree_path, config)
    manifest_path = evidence_dir / "evidence.json"
    if not manifest_path.is_file():
        raise ValidationEvidenceError(
            "Structured evidence is required for this Issue, but "
            f"`{config.validation.evidence_dir}/evidence.json` is missing. "
            "Create the manifest with one evidence block per Realistic Validation "
            "checklist item."
        )
    try:
        with manifest_path.open("r", encoding="utf-8") as manifest_file:
            manifest_data = json.load(manifest_file)
    except json.JSONDecodeError as decode_error:
        raise ValidationEvidenceError(
            f"`{config.validation.evidence_dir}/evidence.json` is not valid JSON: "
            f"{decode_error}"
        ) from decode_error
    if not isinstance(manifest_data, dict):
        raise ValidationEvidenceError(
            f"`{config.validation.evidence_dir}/evidence.json` must be a JSON object."
        )
    return manifest_data


def _parse_evidence_block(block_data: object, item_number: int) -> EvidenceBlock:
    """Parse a single evidence block and validate required fields."""
    if not isinstance(block_data, dict):
        raise ValidationEvidenceError(
            f"Item {item_number}: evidence block must be a JSON object."
        )

    item_name = _extract_nonempty_string(block_data, "item_name", item_number)
    command = _extract_nonempty_string(block_data, "command", item_number)
    output_summary = _extract_nonempty_string(block_data, "output_summary", item_number)
    explanation = _extract_nonempty_string(block_data, "explanation", item_number)
    risks = _extract_nonempty_string(block_data, "risks", item_number)
    evidence_files = _extract_evidence_files(block_data, item_number)
    negative_control = _extract_optional_string(block_data, "negative_control")
    expected_fail = _extract_optional_string(block_data, "expected_fail")

    return EvidenceBlock(
        item_number=item_number,
        item_name=item_name,
        command=command,
        evidence_files=evidence_files,
        output_summary=output_summary,
        explanation=explanation,
        risks=risks,
        negative_control=negative_control,
        expected_fail=expected_fail,
    )


def _extract_nonempty_string(
    block_data: dict[str, object], field_name: str, item_number: int
) -> str:
    """Extract a required non-empty string field from an evidence block."""
    value = block_data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationEvidenceError(
            f"Item {item_number}: missing or empty required field "
            f"`{field_name}` in evidence manifest."
        )
    return value.strip()


def _extract_optional_string(block_data: dict[str, object], field_name: str) -> str:
    """Extract an optional string field; return '' when absent, blank, or non-string."""
    value = block_data.get(field_name)
    if isinstance(value, str):
        return value.strip()
    return ""


def _extract_evidence_files(
    block_data: dict[str, object], item_number: int
) -> tuple[str, ...]:
    """Extract and validate the ``evidence_files`` list."""
    raw_files = block_data.get("evidence_files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValidationEvidenceError(
            f"Item {item_number}: `evidence_files` must be a non-empty list."
        )
    evidence_files: list[str] = []
    for file_name in raw_files:
        if not isinstance(file_name, str) or not file_name.strip():
            raise ValidationEvidenceError(
                f"Item {item_number}: `evidence_files` contains an empty "
                "or non-string entry."
            )
        evidence_files.append(file_name.strip())
    return tuple(evidence_files)


def load_evidence_manifest(worktree_path: Path, config: AppConfig) -> EvidenceManifest:
    """Load ``evidence.json`` and parse it into an ``EvidenceManifest``."""
    manifest_data = _load_manifest_json(worktree_path, config)

    version_value = manifest_data.get("version")
    if version_value != 1:
        raise ValidationEvidenceError(
            f"`{config.validation.evidence_dir}/evidence.json` version must be 1, "
            f"got {version_value!r}."
        )

    language = _extract_manifest_string(manifest_data, "language", config)

    raw_items = manifest_data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValidationEvidenceError(
            f"`{config.validation.evidence_dir}/evidence.json` must contain a "
            "non-empty `items` array."
        )

    parsed_blocks: list[EvidenceBlock] = []
    for raw_block in raw_items:
        block_item_number = _extract_manifest_item_number(raw_block, config)
        parsed_blocks.append(_parse_evidence_block(raw_block, block_item_number))

    return EvidenceManifest(
        version=1,
        language=language,
        items=tuple(parsed_blocks),
    )


def _extract_manifest_string(
    manifest_data: dict[str, object], field_name: str, config: AppConfig
) -> str:
    """Extract a required non-empty string field from the top-level manifest."""
    value = manifest_data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationEvidenceError(
            f"`{config.validation.evidence_dir}/evidence.json` missing or empty "
            f"top-level field `{field_name}`."
        )
    return value.strip()


def _extract_manifest_item_number(raw_block: object, config: AppConfig) -> int:
    """Extract ``item_number`` from a raw evidence block."""
    if not isinstance(raw_block, dict):
        raise ValidationEvidenceError(
            f"`{config.validation.evidence_dir}/evidence.json` contains a non-object "
            "entry in `items`."
        )
    item_number_value = raw_block.get("item_number")
    if not isinstance(item_number_value, int) or item_number_value < 1:
        raise ValidationEvidenceError(
            f"`{config.validation.evidence_dir}/evidence.json` has invalid "
            f"`item_number` {item_number_value!r}; must be a positive integer."
        )
    return item_number_value


def _compute_file_sha256(file_path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    file_hash = hashlib.sha256()
    file_hash.update(file_path.read_bytes())
    return file_hash.hexdigest()


def _validate_evidence_file(
    file_name: str,
    expected_item_number: int,
    evidence_dir: Path,
    config: AppConfig,
) -> EvidenceFileInfo:
    """Validate that an evidence file exists and matches the expected item number."""
    file_match = _EVIDENCE_ITEM_FILE_PATTERN.match(file_name)
    if file_match is None:
        raise ValidationEvidenceError(
            f"Item {expected_item_number}: evidence file `{file_name}` does not "
            f"match the required `rv-{expected_item_number}-*` or "
            f"`rv-{expected_item_number}.*` naming pattern."
        )
    actual_item_number = int(file_match.group("item"))
    if actual_item_number != expected_item_number:
        raise ValidationEvidenceError(
            f"Item {expected_item_number}: evidence file `{file_name}` belongs to "
            f"item {actual_item_number}, not item {expected_item_number}."
        )

    file_path = evidence_dir / file_name
    if not file_path.is_file():
        raise ValidationEvidenceError(
            f"Item {expected_item_number}: evidence file `{file_name}` is listed "
            f"in the manifest but does not exist in `{config.validation.evidence_dir}/`."
        )

    file_info = EvidenceFileInfo(
        file_name=file_name,
        sha256=_compute_file_sha256(file_path),
    )
    _validate_evidence_file_content(
        file_path=file_path,
        expected_item_number=expected_item_number,
        file_name=file_name,
    )
    return file_info


def _validate_evidence_file_content(
    file_path: Path,
    expected_item_number: int,
    file_name: str,
) -> None:
    """Detect cross-contamination between evidence files.

    An evidence file must only contain section headers belonging to its own
    checklist item. If a file named ``rv-1-*.txt`` also contains a section
    header such as ``[Item 2]`` or ``[Item 2c]``, the agent most likely
    leaked output from another item into this file (for example by using
    shell-wide ``exec`` redirection).
    """

    try:
        file_text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    found_items: set[int] = set()
    for section_match in _EVIDENCE_ITEM_SECTION_PATTERN.finditer(file_text):
        found_items.add(int(section_match.group("item")))

    if expected_item_number not in found_items:
        # The file does not even contain its own item marker. This can be
        # legitimate for very short captures, so only flag when another
        # item's marker is present.
        if found_items:
            other_items = sorted(found_items)
            raise ValidationEvidenceError(
                f"Item {expected_item_number}: evidence file `{file_name}` "
                f"contains section header(s) for item(s) {other_items}, "
                "but no header for its own item. Each evidence file must "
                "contain only the output of its own Realistic Validation item."
            )
        return

    foreign_items = sorted(
        item_number
        for item_number in found_items
        if item_number != expected_item_number
    )
    if foreign_items:
        raise ValidationEvidenceError(
            f"Item {expected_item_number}: evidence file `{file_name}` "
            f"contains section header(s) for foreign item(s) {foreign_items}. "
            "Each evidence file must contain only the output of its own "
            "Realistic Validation item. Avoid shell-wide stdout redirection "
            "(e.g. `exec > >(tee ...)`) that leaks output across files."
        )


def validate_evidence_manifest(
    issue_body: str,
    checklist_items: list[str],
    worktree_path: Path,
    config: AppConfig,
) -> StructuredEvidenceReport:
    """Validate the structured evidence manifest against the Issue checklist.

    Args:
        issue_body: Issue body used to locate the structured evidence marker.
        checklist_items: Realistic Validation checklist items from the Issue body.
        worktree_path: Worktree root where ``.iar/evidence/`` lives.
        config: Application configuration.

    Raises:
        ValidationEvidenceError: When the manifest is missing, malformed, or
            does not satisfy the checklist requirements.

    Returns:
        A ``StructuredEvidenceReport`` containing validated blocks and file hashes.
    """
    marker = parse_structured_evidence_marker(issue_body)
    if marker is None:
        raise ValidationEvidenceError(
            "Internal error: validate_evidence_manifest called on an Issue without "
            "an `iar:structured-evidence` marker."
        )

    manifest = load_evidence_manifest(worktree_path, config)
    if manifest.language != marker.language:
        raise ValidationEvidenceError(
            f"Manifest language `{manifest.language}` does not match Issue marker "
            f"language `{marker.language}`."
        )

    expected_item_numbers = set(range(1, len(checklist_items) + 1))
    actual_item_numbers = {block.item_number for block in manifest.items}

    missing_numbers = sorted(expected_item_numbers - actual_item_numbers)
    if missing_numbers:
        raise ValidationEvidenceError(
            "Structured evidence manifest is missing blocks for checklist "
            f"item(s): {', '.join(str(num) for num in missing_numbers)}."
        )

    unexpected_numbers = sorted(actual_item_numbers - expected_item_numbers)
    if unexpected_numbers:
        raise ValidationEvidenceError(
            "Structured evidence manifest contains unexpected item number(s): "
            f"{', '.join(str(num) for num in unexpected_numbers)}."
        )

    item_number_counts: dict[int, int] = {}
    for block in manifest.items:
        item_number_counts[block.item_number] = (
            item_number_counts.get(block.item_number, 0) + 1
        )
    duplicate_numbers = sorted(
        num for num, count in item_number_counts.items() if count > 1
    )
    if duplicate_numbers:
        raise ValidationEvidenceError(
            "Structured evidence manifest contains duplicate item number(s): "
            f"{', '.join(str(num) for num in duplicate_numbers)}."
        )

    evidence_dir = _evidence_dir_path(worktree_path, config)
    item_reports: list[StructuredEvidenceItemReport] = []
    for block in manifest.items:
        file_infos: list[EvidenceFileInfo] = []
        for file_name in block.evidence_files:
            file_infos.append(
                _validate_evidence_file(
                    file_name=file_name,
                    expected_item_number=block.item_number,
                    evidence_dir=evidence_dir,
                    config=config,
                )
            )
        item_reports.append(
            StructuredEvidenceItemReport(block=block, files=tuple(file_infos))
        )

    return StructuredEvidenceReport(
        language=manifest.language,
        items=tuple(item_reports),
    )


def build_evidence_blob_url(pr_url: str, branch: str, file_name: str) -> str | None:
    """Build the repository blob URL for an evidence file."""
    url_match = _PR_URL_PATTERN.search(pr_url)
    if not url_match:
        return None
    owner = url_match.group("owner")
    repo = url_match.group("repo")
    return f"https://github.com/{owner}/{repo}/blob/{branch}/{file_name}"


def _short_sha(sha256_hex: str) -> str:
    """Return a short SHA-256 prefix for display."""
    return sha256_hex[:12]


def _truncate_inline_evidence(file_text: str, language: str) -> str:
    """Limit inline-quoted evidence text in PR comments."""
    if len(file_text) <= _MAX_INLINE_EVIDENCE_CHARS:
        return file_text
    return file_text[:_MAX_INLINE_EVIDENCE_CHARS] + "\n" + _label(language, "truncated")


def _render_evidence_file_lines(
    file_info: EvidenceFileInfo,
    branch: str,
    pr_url: str,
    worktree_path: Path,
    config: AppConfig,
    language: str,
) -> list[str]:
    """Render a single evidence file entry with hash and optional inline content."""
    file_name = file_info.file_name
    file_suffix = Path(file_name).suffix.lower()
    blob_url = build_evidence_blob_url(pr_url, branch, file_name)
    lines: list[str] = [f"- `{file_name}`"]
    lines.append(f"  - {_label(language, 'sha256')}: `{_short_sha(file_info.sha256)}`")
    lines.append(f"  - {_label(language, 'sha256')} (full): `{file_info.sha256}`")

    if blob_url and file_suffix in IMAGE_EVIDENCE_SUFFIXES:
        lines.append(f"  - ![{_label(language, 'image_alt')}]({blob_url}?raw=true)")

    if file_suffix in {".txt", ".log", ".md", ".out"}:
        evidence_file_path = _evidence_dir_path(worktree_path, config) / file_name
        try:
            file_text = evidence_file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            file_text = _label(language, "unreadable")
        truncated_text = _truncate_inline_evidence(file_text.rstrip(), language)
        lines.append("  - ```text")
        for content_line in truncated_text.splitlines():
            lines.append(f"    {content_line}")
        lines.append("    ```")

    if blob_url:
        lines.append(f"  - [{_label(language, 'open_file')}]({blob_url})")

    return lines


def render_structured_evidence_comment(
    report: StructuredEvidenceReport,
    upload: EvidenceUpload,
    worktree_path: Path,
    config: AppConfig,
    pr_url: str,
    head_sha: str,
) -> str:
    """Render the structured PR evidence comment grouped by checklist item."""
    language = report.language
    marker = (
        f"<!-- iar:validation-evidence version=1 head={head_sha} "
        f"branch={upload.branch} count={len(upload.file_names)} -->"
    )
    comment_lines = [
        marker,
        "",
        f"## {_label(language, 'title')}",
        "",
        f"- {_label(language, 'branch')}: `{upload.branch}`",
        f"- {_label(language, 'head')}: `{head_sha}`",
        f"- {_label(language, 'language')}: `{language}`",
        "",
        _label(language, "sign_off"),
    ]

    for item_report in report.items:
        block = item_report.block
        comment_lines.extend(
            [
                "",
                f"### RV-{block.item_number} {block.item_name}",
                "",
                f"**{_label(language, 'reproducible_command')}**",
                f"`{block.command}`",
                "",
                f"**{_label(language, 'evidence_files')}**",
            ]
        )
        for file_info in item_report.files:
            comment_lines.extend(
                _render_evidence_file_lines(
                    file_info=file_info,
                    branch=upload.branch,
                    pr_url=pr_url,
                    worktree_path=worktree_path,
                    config=config,
                    language=language,
                )
            )
        comment_lines.extend(
            [
                "",
                f"**{_label(language, 'output_summary')}**",
                block.output_summary,
                "",
                f"**{_label(language, 'explanation')}**",
                block.explanation,
                "",
                f"**{_label(language, 'risks')}**",
                block.risks,
            ]
        )

    return "\n".join(comment_lines)


def build_structured_evidence_prompt_suffix(language: str) -> str:
    """Build the execution-prompt suffix that requires a structured manifest."""
    if language.startswith("zh"):
        return (
            "此外，你必须在 `{evidence_dir}/evidence.json` 中写入结构化证据 manifest，"
            "按 Realistic Validation checklist item 分组。每个证据块必须包含："
            "`item_number`（序号）、`item_name`（名称）、`command`（可复现命令）、"
            "`evidence_files`（关联证据文件列表）、`output_summary`（关键输出摘要）、"
            "`explanation`（为什么该证据能证明检查点成立）、`risks`（潜在风险或不适用说明）、"
            "`negative_control`（能让该项变红的命令或注入的故障）、`expected_fail`（变红时的样子）。"
            "每个检查点都要证明'这测试会失败'：先用 negative_control 让它变红、记录 expected_fail，"
            "再展示修复后变绿——只有绿、无法证明会红的证据视为无效。"
            'manifest 顶层必须声明 `version: 1` 和 `language: "{language}"`。'
            "所有证据文件必须命名为 `rv-<item_number>-<slug>.<ext>` 并放在 `{evidence_dir}/` 下。"
            "重要：每个证据文件必须只包含对应 item 的输出，禁止混入其他 item 的内容；"
            "不要用 `exec > >(tee -a ...)` 这类全局 stdout 重定向，它会让多个 item 的输出串到同一个文件里。"
        ).format(language=language, evidence_dir="{evidence_dir}")
    return (
        "Additionally, you must write a structured evidence manifest to "
        "`{evidence_dir}/evidence.json`, grouped by Realistic Validation checklist item. "
        "Each evidence block must include: `item_number`, `item_name`, `command`, "
        "`evidence_files`, `output_summary`, `explanation` (why the evidence satisfies "
        "the checkpoint), `risks` (potential risks or not-applicable notes), "
        "`negative_control` (a command or injected fault that makes this item go RED), "
        "and `expected_fail` (what red looks like). "
        "Every checkpoint must prove the test can fail: use negative_control to make it "
        "red and record expected_fail, then show it green after the fix — evidence that "
        "is only ever green, with no way to show it failing, is not accepted. "
        'The manifest top level must declare `version: 1` and `language: "{language}"`. '
        "All evidence files must be named `rv-<item_number>-<slug>.<ext>` and placed "
        "under `{evidence_dir}/`. "
        "Important: each evidence file must contain ONLY the output of its own item; "
        "never mix output from multiple items into one file. Avoid shell-wide stdout "
        "redirection such as `exec > >(tee -a ...)` which leaks output across files."
    ).format(language=language, evidence_dir="{evidence_dir}")
