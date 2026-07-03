"""Tests for structured Realistic Validation evidence manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    ValidationConfig,
)
from backend.core.use_cases.agent_runner_structured_evidence import (
    ArtifactSpec,
    EvidenceBlock,
    EvidenceManifest,
    EvidenceUpload,
    ValidationEvidenceError,
    build_structured_evidence_prompt_suffix,
    format_structured_evidence_marker,
    load_evidence_manifest,
    render_structured_evidence_comment,
    validate_evidence_manifest,
)
from backend.core.use_cases.agent_runner_validation import (
    build_evidence_comment,
    build_validation_prompt_line,
    ensure_validation_evidence_ready,
)


_ISSUE_BODY = """## Summary

Tracked task.

## Realistic Validation

The executing agent MUST run each item.

- [ ] **行为 A 真实验证**：通过 `demo run` 验证输出。
- [ ] **行为 B 真实验证**：通过 `demo serve` 验证页面。
"""


def _issue(body: str = _ISSUE_BODY, number: int = 42) -> IssueSummary:
    return IssueSummary(
        number=number,
        title="Demo",
        url=f"https://github.com/example/repo/issues/{number}",
        body=body,
        labels=("agent/review",),
    )


def _write_manifest(
    evidence_dir: Path,
    *,
    language: str = "zh-CN",
    with_item_2: bool = True,
    omit_command_for_item: int | None = None,
) -> None:
    items = [
        {
            "item_number": 1,
            "item_name": "行为 A 真实验证",
            "command": "uv run pytest tests/test_demo.py -k run -v",
            "evidence_files": ["rv-1-run.txt"],
            "output_summary": "demo run 输出 ok。",
            "explanation": "真实执行了 demo run。",
            "risks": "无外部依赖。",
            "negative_control": "改坏被测逻辑后重跑该用例",
            "expected_fail": "pytest 该用例 FAILED",
        }
    ]
    if with_item_2:
        item_2 = {
            "item_number": 2,
            "item_name": "行为 B 真实验证",
            "command": "uv run pytest tests/test_demo.py -k serve -v",
            "evidence_files": ["rv-2-serve.txt"],
            "output_summary": "demo serve 输出 ok。",
            "explanation": "真实执行了 demo serve。",
            "risks": "无外部依赖。",
            "negative_control": "停掉服务后访问",
            "expected_fail": "连接被拒绝",
        }
        if omit_command_for_item == 2:
            item_2.pop("command")
        items.append(item_2)
    if omit_command_for_item == 1:
        items[0].pop("command")

    manifest = {"version": 1, "language": language, "items": items}
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "evidence.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_evidence_files(evidence_dir: Path) -> dict[str, str]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "rv-1-run.txt": "demo run\nok\n",
        "rv-2-serve.txt": "demo serve\nok\n",
    }
    for name, content in files.items():
        (evidence_dir / name).write_text(content, encoding="utf-8")
    return files


def test_format_and_parse_marker() -> None:
    """Marker round-trips through the parser."""
    marker = format_structured_evidence_marker("zh-CN")
    assert 'language="zh-CN"' in marker
    assert "version=1" in marker


def test_build_structured_evidence_prompt_suffix_contains_schema() -> None:
    """The prompt suffix requires evidence.json and lists required fields."""
    suffix = build_structured_evidence_prompt_suffix("zh-CN")
    assert "evidence.json" in suffix
    assert "item_number" in suffix
    assert "explanation" in suffix
    assert "{evidence_dir}" in suffix
    formatted = suffix.format(evidence_dir=".iar/evidence")
    assert ".iar/evidence/evidence.json" in formatted


def test_build_structured_evidence_prompt_suffix_requires_integer_item_number() -> None:
    """The prompt explicitly requires item_number to be a bare positive integer."""
    zh_suffix = build_structured_evidence_prompt_suffix("zh-CN")
    en_suffix = build_structured_evidence_prompt_suffix("en-US")

    assert "正整数" in zh_suffix
    assert "rv-1" in zh_suffix
    assert "positive integer" in en_suffix
    assert "rv-1" in en_suffix


def test_load_evidence_manifest_accepts_rv_prefixed_item_number(
    tmp_path: Path,
) -> None:
    """String item numbers like 'rv-1' are coerced to integers for robustness."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "rv-1-run.txt").write_text("ok", encoding="utf-8")
    manifest = {
        "version": 1,
        "language": "zh-CN",
        "items": [
            {
                "item_number": "rv-1",
                "item_name": "行为 A 真实验证",
                "command": "uv run pytest tests/test_demo.py -k run -v",
                "evidence_files": ["rv-1-run.txt"],
                "output_summary": "demo run 输出 ok。",
                "explanation": "真实执行了 demo run。",
                "risks": "无外部依赖。",
                "negative_control": "改坏被测逻辑后重跑该用例",
                "expected_fail": "pytest 该用例 FAILED",
            }
        ],
    }
    (evidence_dir / "evidence.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result = load_evidence_manifest(tmp_path, AppConfig())

    assert len(result.items) == 1
    assert result.items[0].item_number == 1


def test_build_structured_evidence_prompt_suffix_warns_against_exec_redirection() -> None:
    """The prompt explicitly discourages shell exec redirection that leaks output."""
    zh_suffix = build_structured_evidence_prompt_suffix("zh-CN")
    en_suffix = build_structured_evidence_prompt_suffix("en-US")

    assert "exec >" in zh_suffix
    assert "tee" in zh_suffix
    assert "exec >" in en_suffix
    assert "tee" in en_suffix


def test_ensure_validation_evidence_ready_passes_with_complete_manifest(
    tmp_path: Path,
) -> None:
    """A complete manifest and matching evidence files satisfy the gate."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    _write_evidence_files(evidence_dir)
    _write_manifest(evidence_dir)
    body = _ISSUE_BODY + "\n" + format_structured_evidence_marker("zh-CN")

    ensure_validation_evidence_ready(_issue(body=body), tmp_path, AppConfig())


def test_ensure_validation_evidence_ready_rejects_missing_manifest(
    tmp_path: Path,
) -> None:
    """A structured Issue without evidence.json fails with a clear message."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    _write_evidence_files(evidence_dir)
    body = _ISSUE_BODY + "\n" + format_structured_evidence_marker("zh-CN")

    with pytest.raises(ValidationEvidenceError, match="evidence.json"):
        ensure_validation_evidence_ready(_issue(body=body), tmp_path, AppConfig())


def test_ensure_validation_evidence_ready_rejects_missing_required_field(
    tmp_path: Path,
) -> None:
    """A manifest missing a required field identifies the item and field."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    _write_evidence_files(evidence_dir)
    _write_manifest(evidence_dir, omit_command_for_item=2)
    body = _ISSUE_BODY + "\n" + format_structured_evidence_marker("zh-CN")

    with pytest.raises(ValidationEvidenceError) as exc_info:
        ensure_validation_evidence_ready(_issue(body=body), tmp_path, AppConfig())
    error_text = str(exc_info.value)
    assert "command" in error_text
    assert "Item 2" in error_text


def test_ensure_validation_evidence_ready_rejects_file_number_mismatch(
    tmp_path: Path,
) -> None:
    """Evidence files must match the item number they are listed under."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    _write_evidence_files(evidence_dir)
    _write_manifest(evidence_dir)
    manifest_path = evidence_dir / "evidence.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["items"][0]["evidence_files"] = ["rv-2-serve.txt"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    body = _ISSUE_BODY + "\n" + format_structured_evidence_marker("zh-CN")

    with pytest.raises(ValidationEvidenceError) as exc_info:
        ensure_validation_evidence_ready(_issue(body=body), tmp_path, AppConfig())
    error_text = str(exc_info.value)
    assert "rv-2-serve.txt" in error_text
    assert "item 1" in error_text.lower()


def test_ensure_validation_evidence_ready_rejects_foreign_item_headers(
    tmp_path: Path,
) -> None:
    """An evidence file must not contain section headers from other items."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    _write_evidence_files(evidence_dir)
    _write_manifest(evidence_dir)
    # rv-1-run.txt contains its own header plus a foreign [Item 2] header.
    (evidence_dir / "rv-1-run.txt").write_text(
        "[Item 1] run\nok\n[Item 2] leaked section\n",
        encoding="utf-8",
    )
    body = _ISSUE_BODY + "\n" + format_structured_evidence_marker("zh-CN")

    with pytest.raises(ValidationEvidenceError) as exc_info:
        ensure_validation_evidence_ready(_issue(body=body), tmp_path, AppConfig())
    error_text = str(exc_info.value)
    assert "rv-1-run.txt" in error_text
    assert "foreign item(s) [2]" in error_text


def test_ensure_validation_evidence_ready_rejects_file_with_only_foreign_headers(
    tmp_path: Path,
) -> None:
    """An evidence file containing only foreign headers is cross-contaminated."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    _write_evidence_files(evidence_dir)
    _write_manifest(evidence_dir)
    # rv-1-run.txt is supposed to hold item 1 but only has an [Item 2] header.
    (evidence_dir / "rv-1-run.txt").write_text(
        "[Item 2] this whole file came from item 2\n",
        encoding="utf-8",
    )
    body = _ISSUE_BODY + "\n" + format_structured_evidence_marker("zh-CN")

    with pytest.raises(ValidationEvidenceError) as exc_info:
        ensure_validation_evidence_ready(_issue(body=body), tmp_path, AppConfig())
    error_text = str(exc_info.value)
    assert "rv-1-run.txt" in error_text
    assert "item(s) [2]" in error_text


def test_load_evidence_manifest_requires_version_one(tmp_path: Path) -> None:
    """Only version 1 manifests are accepted."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "evidence.json").write_text(
        json.dumps({"version": 2, "language": "zh-CN", "items": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValidationEvidenceError, match="version must be 1"):
        load_evidence_manifest(tmp_path, AppConfig())


def test_build_evidence_comment_groups_by_item_in_chinese(tmp_path: Path) -> None:
    """Structured comment groups by RV item and uses Chinese labels."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    files = _write_evidence_files(evidence_dir)
    _write_manifest(evidence_dir)
    body = _ISSUE_BODY + "\n" + format_structured_evidence_marker("zh-CN")

    comment = build_evidence_comment(
        upload=EvidenceUpload(
            branch="iar-evidence/issue-42",
            commit_sha="commit1",
            file_names=tuple(files.keys()),
        ),
        worktree_path=tmp_path,
        config=AppConfig(),
        pr_url="https://github.com/example/repo/pull/7",
        head_sha="abc1234",
        issue_body=body,
    )

    assert "### RV-1 行为 A 真实验证" in comment
    assert "### RV-2 行为 B 真实验证" in comment
    assert "可复现命令" in comment
    assert "为什么能证明该检查点成立" in comment
    assert "潜在风险 / 不适用说明" in comment
    assert "- 语言: `zh-CN`" in comment

    expected_sha = hashlib.sha256(files["rv-1-run.txt"].encode("utf-8")).hexdigest()
    assert expected_sha[:12] in comment
    assert expected_sha in comment


def test_build_evidence_comment_renders_text_evidence_as_code_block(
    tmp_path: Path,
) -> None:
    """Text evidence is inlined as a properly indented fenced code block."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    files = _write_evidence_files(evidence_dir)
    _write_manifest(evidence_dir)
    body = _ISSUE_BODY + "\n" + format_structured_evidence_marker("zh-CN")

    comment = build_evidence_comment(
        upload=EvidenceUpload(
            branch="iar-evidence/issue-42",
            commit_sha="commit1",
            file_names=tuple(files.keys()),
        ),
        worktree_path=tmp_path,
        config=AppConfig(),
        pr_url="https://github.com/example/repo/pull/7",
        head_sha="abc1234",
        issue_body=body,
    )

    assert "  - ```text\n    demo run\n    ok\n    ```" in comment
    assert "  - demo run" not in comment


def test_build_evidence_comment_uses_english_labels(tmp_path: Path) -> None:
    """A marker with language en-US renders English fixed labels."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    files = _write_evidence_files(evidence_dir)
    _write_manifest(evidence_dir, language="en-US")
    body = _ISSUE_BODY + "\n" + format_structured_evidence_marker("en-US")

    comment = build_evidence_comment(
        upload=EvidenceUpload(
            branch="iar-evidence/issue-42",
            commit_sha="commit1",
            file_names=tuple(files.keys()),
        ),
        worktree_path=tmp_path,
        config=AppConfig(validation=ValidationConfig(language="en-US")),
        pr_url="https://github.com/example/repo/pull/7",
        head_sha="abc1234",
        issue_body=body,
    )

    assert "Reproducible command" in comment
    assert "Why this satisfies the checkpoint" in comment
    assert "Potential risks / not-applicable notes" in comment
    assert "Language: `en-US`" in comment


def test_build_evidence_comment_legacy_without_marker(tmp_path: Path) -> None:
    """Issues without the structured marker still render the legacy flat list."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    _write_evidence_files(evidence_dir)

    comment = build_evidence_comment(
        upload=EvidenceUpload(
            branch="iar-evidence/issue-42",
            commit_sha="commit1",
            file_names=("rv-1-run.txt", "rv-2-serve.txt"),
        ),
        worktree_path=tmp_path,
        config=AppConfig(),
        pr_url="https://github.com/example/repo/pull/7",
        head_sha="abc1234",
    )

    assert "### rv-1-run.txt" in comment
    assert "### rv-2-serve.txt" in comment
    assert "RV-1" not in comment


def test_build_validation_prompt_line_includes_manifest_suffix() -> None:
    """The execution prompt requires a structured manifest for marked Issues."""
    body = _ISSUE_BODY + "\n" + format_structured_evidence_marker("zh-CN")
    prompt_line = build_validation_prompt_line(_issue(body=body), AppConfig())

    assert "evidence.json" in prompt_line
    assert "manifest" in prompt_line


def test_render_structured_evidence_comment_sorts_items(tmp_path: Path) -> None:
    """Rendered comment lists items in ascending item-number order."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    files = _write_evidence_files(evidence_dir)
    _write_manifest(evidence_dir)

    report = validate_evidence_manifest(
        issue_body=_ISSUE_BODY + "\n" + format_structured_evidence_marker("zh-CN"),
        checklist_items=[
            "- [ ] 行为 A",
            "- [ ] 行为 B",
        ],
        worktree_path=tmp_path,
        config=AppConfig(),
    )
    comment = render_structured_evidence_comment(
        report=report,
        upload=EvidenceUpload(
            branch="iar-evidence/issue-42",
            commit_sha="commit1",
            file_names=tuple(files.keys()),
        ),
        worktree_path=tmp_path,
        config=AppConfig(),
        pr_url="https://github.com/example/repo/pull/7",
        head_sha="abc1234",
    )

    rv1_index = comment.index("### RV-1")
    rv2_index = comment.index("### RV-2")
    assert rv1_index < rv2_index


# ---------------------------------------------------------------------------
# FR-11a: expected_artifacts parsing + hard-layer validation
# ---------------------------------------------------------------------------


def _write_manifest_with_artifacts(
    evidence_dir: Path, expected_artifacts: list[dict] | None
) -> None:
    """Write a single-item manifest whose evidence block has expected_artifacts."""
    item: dict = {
        "item_number": 1,
        "item_name": "UI login screenshot",
        "command": "playwright test login.spec.ts",
        "evidence_files": ["rv-1-login.png"],
        "output_summary": "login succeeds",
        "explanation": "screenshot captured",
        "risks": "none",
        "negative_control": "hide login form",
        "expected_fail": "blank screenshot",
    }
    if expected_artifacts is not None:
        item["expected_artifacts"] = expected_artifacts
    manifest = {"version": 1, "language": "en-US", "items": [item]}
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "evidence.json").write_text(json.dumps(manifest), encoding="utf-8")


class _FakeProcessRunnerForArtifacts:
    """Process runner with controllable ``file --mime-type`` / ``ffprobe`` output."""

    def __init__(self, mime: str = "image/png", duration: str = "5.0") -> None:
        self._mime = mime
        self._duration = duration
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        command,
        *,
        cwd=None,
        check=True,
        timeout=None,
        inactivity_timeout=None,
        capture_output=True,
        input_text=None,
        label=None,
        output_sink=None,
    ) -> object:
        from backend.core.shared.models.agent_runner import CommandResult

        self.calls.append(tuple(command))
        if command[:2] == ("file", "--mime-type"):
            return CommandResult(tuple(command), 0, self._mime + "\n", "")
        if command[:1] == ("ffprobe",):
            return CommandResult(tuple(command), 0, self._duration + "\n", "")
        return CommandResult(tuple(command), 0, "", "")


def test_validate_evidence_artifact_rejects_zero_byte(tmp_path: Path) -> None:
    """Hard layer: 0-byte file is rejected (catches blank screenshots)."""
    from backend.core.use_cases.agent_runner_structured_evidence import (
        validate_evidence_artifact,
    )

    (tmp_path / "screenshot.png").write_bytes(b"")
    spec = ArtifactSpec(path="screenshot.png", mime="image/png")
    with pytest.raises(ValidationEvidenceError) as exc:
        validate_evidence_artifact(tmp_path, spec, _FakeProcessRunnerForArtifacts())
    assert "0-byte" in str(exc.value)


def test_validate_evidence_artifact_rejects_size_below_min(tmp_path: Path) -> None:
    """Hard layer: file smaller than min_size is rejected."""
    from backend.core.use_cases.agent_runner_structured_evidence import (
        validate_evidence_artifact,
    )

    (tmp_path / "screenshot.png").write_bytes(b"x" * 100)
    spec = ArtifactSpec(path="screenshot.png", mime="image/png", min_size=50000)
    with pytest.raises(ValidationEvidenceError) as exc:
        validate_evidence_artifact(tmp_path, spec, _FakeProcessRunnerForArtifacts())
    assert "100 bytes" in str(exc.value)
    assert "50000" in str(exc.value)


def test_validate_evidence_artifact_rejects_mime_mismatch(tmp_path: Path) -> None:
    """Hard layer: actual mime ≠ declared mime is rejected."""
    from backend.core.use_cases.agent_runner_structured_evidence import (
        validate_evidence_artifact,
    )

    (tmp_path / "screenshot.png").write_bytes(b"x" * 100)
    spec = ArtifactSpec(path="screenshot.png", mime="image/png")
    runner = _FakeProcessRunnerForArtifacts(mime="text/plain")
    with pytest.raises(ValidationEvidenceError) as exc:
        validate_evidence_artifact(tmp_path, spec, runner)
    assert "image/png" in str(exc.value)
    assert "text/plain" in str(exc.value)


def test_validate_evidence_artifact_rejects_stale_mtime(tmp_path: Path) -> None:
    """Hard layer: mtime before `since` is rejected (stale-artifact guard)."""
    from datetime import datetime, timedelta, timezone

    from backend.core.use_cases.agent_runner_structured_evidence import (
        validate_evidence_artifact,
    )

    artifact = tmp_path / "screenshot.png"
    artifact.write_bytes(b"x" * 100)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    spec = ArtifactSpec(path="screenshot.png", mime="image/png")
    with pytest.raises(ValidationEvidenceError) as exc:
        validate_evidence_artifact(tmp_path, spec, _FakeProcessRunnerForArtifacts(), since=future)
    assert "stale" in str(exc.value).lower() or "mtime" in str(exc.value).lower()


def test_validate_evidence_artifact_rejects_short_duration(tmp_path: Path) -> None:
    """Hard layer: video shorter than min_duration is rejected."""
    from backend.core.use_cases.agent_runner_structured_evidence import (
        validate_evidence_artifact,
    )

    (tmp_path / "flow.webm").write_bytes(b"x" * 200)
    spec = ArtifactSpec(path="flow.webm", mime="video/webm", min_duration_seconds=3.0)
    runner = _FakeProcessRunnerForArtifacts(mime="video/webm", duration="1.5")
    with pytest.raises(ValidationEvidenceError) as exc:
        validate_evidence_artifact(tmp_path, spec, runner)
    assert "1.5" in str(exc.value)
    assert "3.0" in str(exc.value)


def test_validate_evidence_artifact_passes_when_all_hard_checks_pass(
    tmp_path: Path,
) -> None:
    """Happy path: existing non-empty file + matching mime + size/duration pass."""
    from backend.core.use_cases.agent_runner_structured_evidence import (
        validate_evidence_artifact,
    )

    (tmp_path / "shot.png").write_bytes(b"x" * 60000)
    spec = ArtifactSpec(path="shot.png", mime="image/png", min_size=50000, key_claim="Welcome")
    validate_evidence_artifact(tmp_path, spec, _FakeProcessRunnerForArtifacts())


def test_validate_evidence_artifacts_opt_out_disables_check(tmp_path: Path) -> None:
    """config.validation.artifact_health_enabled=False → no checks."""
    from backend.core.use_cases.agent_runner_structured_evidence import (
        validate_evidence_artifacts,
    )

    manifest = EvidenceManifest(
        version=1,
        language="en-US",
        items=(
            EvidenceBlock(
                item_number=1,
                item_name="x",
                command="c",
                evidence_files=("rv-1.png",),
                output_summary="s",
                explanation="e",
                risks="r",
                negative_control="nc",
                expected_fail="ef",
                expected_artifacts=(ArtifactSpec(path="missing.png", mime="image/png"),),
            ),
        ),
    )
    config = AppConfig(validation=ValidationConfig(artifact_health_enabled=False))
    # Should NOT raise even though missing.png does not exist.
    validate_evidence_artifacts(manifest, tmp_path, config, _FakeProcessRunnerForArtifacts())


def test_validate_evidence_manifest_parses_expected_artifacts(tmp_path: Path) -> None:
    """Manifest with expected_artifacts parses into ArtifactSpec objects."""
    evidence_dir = tmp_path / ".iar" / "evidence"
    _write_manifest_with_artifacts(
        evidence_dir,
        [
            {
                "path": "rv-1-login.png",
                "mime": "image/png",
                "min_size": 50000,
                "key_claim": "Welcome, Alice",
            }
        ],
    )
    manifest = load_evidence_manifest(tmp_path, AppConfig())
    assert len(manifest.items) == 1
    specs = manifest.items[0].expected_artifacts
    assert len(specs) == 1
    assert specs[0].path == "rv-1-login.png"
    assert specs[0].mime == "image/png"
    assert specs[0].min_size == 50000
    assert specs[0].key_claim == "Welcome, Alice"
