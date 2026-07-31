"""Tests for the independent verifier verdict protocol (PR#2 T3)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    ValidationConfig,
)
from backend.core.use_cases.agent_runner_structured_evidence import (
    ArtifactSpec,
    EvidenceBlock,
    EvidenceManifest,
    ValidationEvidenceError,
    format_structured_evidence_marker,
)
from backend.core.use_cases.run_verifier_agent import (
    ValidationVerdict,
    build_verifier_prompt,
    format_verifier_verdict_marker,
    parse_verifier_verdict,
)
from tests.conftest import FakeProcessRunner


def _issue() -> IssueSummary:
    return IssueSummary(
        number=7,
        title="Dedupe users by email case-insensitively",
        url="https://github.com/example/repo/issues/7",
        body="Users with the same email in different case must be treated as one.",
        labels=("agent/review",),
    )


def _manifest() -> EvidenceManifest:
    block = EvidenceBlock(
        item_number=1,
        item_name="case-insensitive dedupe",
        command="demo dedupe --check",
        evidence_files=("rv-1.txt",),
        output_summary="ok",
        explanation="ran it",
        risks="none",
        negative_control="feed Mixed-Case duplicates",
        expected_fail="duplicates survive",
    )
    return EvidenceManifest(version=1, language="en-US", items=(block,))


def test_verdict_marker_roundtrip() -> None:
    """Each risk level round-trips through format → parse."""
    for risk in ("green", "yellow", "red"):
        marker = format_verifier_verdict_marker(risk)
        verdict = parse_verifier_verdict(f"some verifier report...\n{marker}\n")
        assert verdict.risk == risk


def test_green_and_yellow_pass_red_blocks() -> None:
    """green/yellow pass (yellow warns, does not block); red blocks."""
    green = parse_verifier_verdict(format_verifier_verdict_marker("green"))
    yellow = parse_verifier_verdict(format_verifier_verdict_marker("yellow"))
    red = parse_verifier_verdict(format_verifier_verdict_marker("red"))
    assert green.passed and not green.blocks
    assert yellow.passed and not yellow.blocks
    assert red.blocks and not red.passed


def test_missing_or_malformed_marker_fails_safe_to_red() -> None:
    """No verdict / malformed marker must NOT silently pass — fail safe to red."""
    for text in (
        "",
        "no marker here at all",
        "<!-- iar:verifier-verdict risk=bogus -->",
        "<!-- iar:verifier-verdict -->",
    ):
        verdict = parse_verifier_verdict(text)
        assert verdict.risk == "red"
        assert verdict.blocks


def test_latest_marker_wins() -> None:
    """When a repair re-runs the verifier, the latest verdict is authoritative."""
    text = (
        f"{format_verifier_verdict_marker('red')}\n"
        f"...repaired...\n{format_verifier_verdict_marker('green')}\n"
    )
    assert parse_verifier_verdict(text).risk == "green"


def test_findings_preserved() -> None:
    """Caller-supplied findings are carried into the verdict."""
    verdict = parse_verifier_verdict(
        format_verifier_verdict_marker("yellow"), findings="edge case X untested"
    )
    assert verdict.findings == "edge case X untested"


def test_format_rejects_invalid_risk() -> None:
    """Only the three known risk levels can be formatted."""
    with pytest.raises(ValueError):
        format_verifier_verdict_marker("orange")


def test_build_verifier_prompt_demands_independence_and_marker() -> None:
    """The prompt enforces independence, real-entry, negative control, and verdict."""
    prompt = build_verifier_prompt(_issue(), "abc1234", _manifest())
    assert "INDEPENDENT verifier" in prompt
    assert "do not just" in prompt.lower()
    assert "do not assume the builder tested the right thing" in prompt.lower()
    assert "negative control" in prompt.lower()
    assert "demo dedupe --check" in prompt  # real entry injected from the oracle
    assert "iar:verifier-verdict" in prompt  # how to emit the verdict
    assert "abc1234" in prompt  # builder commit
    assert _issue().title in prompt


def test_build_verifier_prompt_includes_evidence_artifacts() -> None:
    """evidence_files are listed so the verifier knows what artifacts to inspect."""
    prompt = build_verifier_prompt(_issue(), "abc1234", _manifest())
    assert "rv-1.txt" in prompt
    assert "evidence artifacts on disk" in prompt


def test_build_verifier_prompt_directs_multimodal_handling() -> None:
    """The prompt tells the verifier to use native multimodal reads, falling back to shell."""
    prompt = build_verifier_prompt(_issue(), "abc1234", _manifest())
    assert "Multimodal evidence" in prompt
    assert "ffmpeg" in prompt  # frame extraction hint
    assert "ffprobe" in prompt  # metadata fallback
    assert "0-byte" in prompt  # explicit warning against trusting size alone


def test_build_verifier_prompt_injects_key_claim_and_fairness_rule() -> None:
    """expected_artifacts + key_claim are injected; D-14c fairness rule is stated."""
    block = EvidenceBlock(
        item_number=1,
        item_name="UI login",
        command="playwright test login.spec.ts",
        evidence_files=("rv-1-login.png",),
        output_summary="ok",
        explanation="ran it",
        risks="none",
        negative_control="hide form",
        expected_fail="blank screenshot",
        expected_artifacts=(
            ArtifactSpec(
                path="rv-1-login.png",
                mime="image/png",
                min_size=50000,
                key_claim="Welcome, Alice",
            ),
        ),
    )
    manifest = EvidenceManifest(version=1, language="en-US", items=(block,))
    prompt = build_verifier_prompt(_issue(), "abc1234", manifest)
    assert "rv-1-login.png" in prompt
    assert "image/png" in prompt
    assert "Welcome, Alice" in prompt  # key_claim injected
    assert "D-14c" in prompt  # fairness rule referenced
    assert "text-only" in prompt.lower()  # text-only model guidance
    assert "red" in prompt.lower()  # red-for-breaks-only guidance


def test_run_verifier_agent_returns_parsed_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A green marker in the agent's output yields a green verdict."""
    from backend.core.use_cases import run_verifier_agent as rva

    captured: dict[str, str] = {}

    def _fake_resilient(agent_name, prompt, worktree_path, process_runner, **kwargs):
        captured["agent"] = agent_name
        captured["prompt"] = prompt
        return CommandResult(
            command=(agent_name,),
            return_code=0,
            stdout=f"report\n{format_verifier_verdict_marker('green')}\n",
            stderr="",
        )

    monkeypatch.setattr(rva, "run_agent_with_prompt_resilient", _fake_resilient)
    verdict = rva.run_verifier_agent(
        _issue(), tmp_path, "abc1234", _manifest(), "kimi", FakeProcessRunner()
    )
    assert verdict.risk == "green"
    assert captured["agent"] == "kimi"
    assert "INDEPENDENT verifier" in captured["prompt"]


def test_run_verifier_agent_fails_safe_when_no_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An agent that emits no verdict marker fails safe to red (blocked)."""
    from backend.core.use_cases import run_verifier_agent as rva

    def _fake_resilient(agent_name, prompt, worktree_path, process_runner, **kwargs):
        return CommandResult((agent_name,), 0, "I had trouble running things.", "")

    monkeypatch.setattr(rva, "run_agent_with_prompt_resilient", _fake_resilient)
    verdict = rva.run_verifier_agent(
        _issue(), tmp_path, "abc1234", _manifest(), "kimi", FakeProcessRunner()
    )
    assert verdict.risk == "red"
    assert verdict.blocks


def _structured_issue() -> IssueSummary:
    body = (
        "## Summary\n\nTask.\n\n"
        f"{format_structured_evidence_marker('zh-CN')}\n\n"
        "## Realistic Validation\n\n- [ ] **行为 A**: via demo run\n"
    )
    return IssueSummary(
        number=7,
        title="Demo",
        url="https://github.com/example/repo/issues/7",
        body=body,
        labels=("agent/review",),
    )


def _patch_gate_deps(
    monkeypatch: pytest.MonkeyPatch, rva, verdict: ValidationVerdict, record: dict
) -> None:
    monkeypatch.setattr(rva, "load_evidence_manifest", lambda *a, **k: _manifest())
    monkeypatch.setattr(rva, "get_head_sha", lambda *a, **k: "abc1234")

    def _fake_run(*args, **kwargs):
        record["called"] = True
        return verdict

    monkeypatch.setattr(rva, "run_verifier_agent", _fake_run)


def test_run_verifier_gate_passes_on_green(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """green verdict → gate passes and the verifier was invoked."""
    from backend.core.use_cases import run_verifier_agent as rva

    record: dict = {}
    _patch_gate_deps(monkeypatch, rva, ValidationVerdict(risk="green"), record)
    config = AppConfig(validation=ValidationConfig(verifier_enabled=True))
    rva.run_verifier_gate(_structured_issue(), tmp_path, config, FakeProcessRunner(), "claude")
    assert record.get("called") is True


def test_run_verifier_gate_blocks_on_red(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """red verdict → raises ValidationEvidenceError (routes into recovery)."""
    from backend.core.use_cases import run_verifier_agent as rva

    record: dict = {}
    _patch_gate_deps(monkeypatch, rva, ValidationVerdict(risk="red", findings="broke X"), record)
    config = AppConfig(validation=ValidationConfig(verifier_enabled=True))
    with pytest.raises(ValidationEvidenceError) as exc_info:
        rva.run_verifier_gate(_structured_issue(), tmp_path, config, FakeProcessRunner(), "claude")
    assert "RED" in str(exc_info.value)
    assert "broke X" in str(exc_info.value)


def test_run_verifier_gate_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """verifier_enabled=False → gate is a no-op; the verifier is never invoked."""
    from backend.core.use_cases import run_verifier_agent as rva

    record: dict = {}
    _patch_gate_deps(monkeypatch, rva, ValidationVerdict(risk="red"), record)
    config = AppConfig(validation=ValidationConfig(verifier_enabled=False))
    rva.run_verifier_gate(_structured_issue(), tmp_path, config, FakeProcessRunner(), "claude")
    assert record.get("called") is None


def test_run_verifier_gate_noop_without_structured_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No structured-evidence marker → gate is a no-op even when enabled."""
    from backend.core.use_cases import run_verifier_agent as rva

    record: dict = {}
    _patch_gate_deps(monkeypatch, rva, ValidationVerdict(risk="red"), record)
    config = AppConfig(validation=ValidationConfig(verifier_enabled=True))
    issue = IssueSummary(
        number=7,
        title="Demo",
        url="u",
        body="## Realistic Validation\n\n- [ ] x\n",
        labels=(),
    )
    rva.run_verifier_gate(issue, tmp_path, config, FakeProcessRunner(), "claude")
    assert record.get("called") is None


# ---------------------------------------------------------------------------
# apply_verifier_verdict_to_pr (FR-5/FR-6 post-PR side effects)
# ---------------------------------------------------------------------------


def test_apply_verifier_verdict_to_pr_green_sets_label() -> None:
    """green verdict → set the `validation/verifier-passed` label on the PR."""
    from backend.core.use_cases.run_verifier_agent import (
        apply_verifier_verdict_to_pr,
        ValidationVerdict,
    )

    class _RecordingClient:
        def __init__(self) -> None:
            self.labels: dict[int, tuple[str, ...]] = {}
            self.pr_comments: dict[int, str] = {}

        def edit_issue_labels(self, pr_number, *, add=(), remove=()) -> None:
            self.labels[pr_number] = tuple(add)

        def comment_pr(self, pr_number, body) -> None:  # noqa: ARG002
            self.pr_comments[pr_number] = body

    client = _RecordingClient()
    apply_verifier_verdict_to_pr(
        pr_url="https://github.com/o/r/pull/57",
        verdict=ValidationVerdict(risk="green"),
        issue_number=42,
        verifier_passed_label="validation/verifier-passed",
        github_client=client,  # type: ignore[arg-type]
    )
    assert client.labels.get(57) == ("validation/verifier-passed",)
    assert client.pr_comments == {}


def test_apply_verifier_verdict_to_pr_yellow_posts_warning_comment() -> None:
    """yellow verdict → post a PR warning comment with the verifier findings."""
    from backend.core.use_cases.run_verifier_agent import (
        apply_verifier_verdict_to_pr,
        build_verifier_yellow_comment,
        ValidationVerdict,
    )

    class _RecordingClient:
        def __init__(self) -> None:
            self.labels: dict[int, tuple[str, ...]] = {}
            self.pr_comments: dict[int, str] = {}

        def edit_issue_labels(self, pr_number, *, add=(), remove=()) -> None:
            self.labels[pr_number] = tuple(add)

        def comment_pr(self, pr_number, body) -> None:
            self.pr_comments[pr_number] = body

    client = _RecordingClient()
    verdict = ValidationVerdict(risk="yellow", findings="edge case X observed")
    apply_verifier_verdict_to_pr(
        pr_url="https://github.com/o/r/pull/57",
        verdict=verdict,
        issue_number=42,
        verifier_passed_label="validation/verifier-passed",
        github_client=client,  # type: ignore[arg-type]
    )
    assert client.labels == {}
    assert client.pr_comments.get(57) == build_verifier_yellow_comment(verdict, 42)
    assert "YELLOW" in client.pr_comments[57]
    assert "edge case X observed" in client.pr_comments[57]


def test_apply_verifier_verdict_to_pr_none_is_noop() -> None:
    """None verdict (verifier disabled/skipped) → no label, no comment."""
    from backend.core.use_cases.run_verifier_agent import (
        apply_verifier_verdict_to_pr,
    )

    class _RecordingClient:
        def __init__(self) -> None:
            self.labels: dict[int, tuple[str, ...]] = {}
            self.pr_comments: dict[int, str] = {}

        def edit_issue_labels(self, pr_number, *, add=(), remove=()) -> None:
            self.labels[pr_number] = tuple(add)

        def comment_pr(self, pr_number, body) -> None:
            self.pr_comments[pr_number] = body

    client = _RecordingClient()
    apply_verifier_verdict_to_pr(
        pr_url="https://github.com/o/r/pull/57",
        verdict=None,
        issue_number=42,
        verifier_passed_label="validation/verifier-passed",
        github_client=client,  # type: ignore[arg-type]
    )
    assert client.labels == {}
    assert client.pr_comments == {}


def test_run_verifier_gate_returns_verdict_on_green(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """green verdict flows back to the caller so it can set the PR label."""
    from backend.core.use_cases import run_verifier_agent as rva

    record: dict = {}
    _patch_gate_deps(monkeypatch, rva, ValidationVerdict(risk="green"), record)
    config = AppConfig(validation=ValidationConfig(verifier_enabled=True))
    verdict = rva.run_verifier_gate(
        _structured_issue(), tmp_path, config, FakeProcessRunner(), "claude"
    )
    assert verdict is not None and verdict.risk == "green"


def test_missing_marker_is_distinguishable_even_with_findings_text() -> None:
    """漏 marker 必须能与"真判 red"区分,且不能依赖 findings 是否为空。

    调用方总会把响应文本填进 findings,所以哨兵字符串永远走不到——只要模型
    吐了任何文字但漏了最后那行 marker,红判与漏判就无法分辨(实证:
    freshai Issue #111)。marker_found 才是可靠信号。
    """
    silent = parse_verifier_verdict("no marker here", findings="I ran some things.")
    judged = parse_verifier_verdict(
        f"real finding\n{format_verifier_verdict_marker('red')}", findings="login breaks"
    )

    assert silent.risk == "red" and silent.blocks
    assert silent.missing_marker is True
    assert silent.findings == "I ran some things."  # 原始输出仍带回，供排查
    assert judged.risk == "red" and judged.blocks
    assert judged.missing_marker is False


def test_run_verifier_agent_saves_raw_response_for_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """verifier 以 capture_output 运行,原始响应必须落盘,否则事后无从查证。"""
    from backend.core.use_cases import run_verifier_agent as rva

    def _fake_resilient(agent_name, prompt, worktree_path, process_runner, **kwargs):
        return CommandResult((agent_name,), 0, "I could not run the stack.", "")

    monkeypatch.setattr(rva, "run_agent_with_prompt_resilient", _fake_resilient)
    response_log_path = tmp_path / ".iar" / "evidence" / "verifier-response.txt"

    verdict = rva.run_verifier_agent(
        _issue(),
        tmp_path,
        "abc1234",
        _manifest(),
        "kimi",
        FakeProcessRunner(),
        response_log_path=response_log_path,
    )

    assert verdict.missing_marker is True
    saved = response_log_path.read_text(encoding="utf-8")
    assert "# verifier agent: kimi" in saved
    assert "# parsed risk: red" in saved
    assert "# verdict marker found: no" in saved
    assert "I could not run the stack." in saved


def test_run_verifier_agent_response_log_failure_does_not_break_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """写盘失败只能降级为告警,不能影响 verdict 本身。"""
    from backend.core.use_cases import run_verifier_agent as rva

    def _fake_resilient(agent_name, prompt, worktree_path, process_runner, **kwargs):
        return CommandResult((agent_name,), 0, f"ok\n{format_verifier_verdict_marker('green')}", "")

    monkeypatch.setattr(rva, "run_agent_with_prompt_resilient", _fake_resilient)
    # 用一个已存在的**文件**当父目录，迫使 mkdir/write 失败。
    blocking_file = tmp_path / "not-a-dir"
    blocking_file.write_text("x", encoding="utf-8")

    verdict = rva.run_verifier_agent(
        _issue(),
        tmp_path,
        "abc1234",
        _manifest(),
        "kimi",
        FakeProcessRunner(),
        response_log_path=blocking_file / "verifier-response.txt",
    )

    assert verdict.risk == "green"


def test_run_verifier_agent_saves_partial_output_when_timed_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """超时被杀时,verifier 已经产出的部分输出必须落盘。

    以前这条路径直接抛 TimeoutExpired,写盘发生在返回之后,于是"跑了半小时被杀"
    在磁盘上什么都不留,Issue 评论里只剩一行 timed out。
    """
    from backend.core.use_cases import run_verifier_agent as rva

    def _fake_resilient(agent_name, prompt, worktree_path, process_runner, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=[agent_name, "--prompt", prompt],
            timeout=1800,
            output="I checked rv-1 and rv-2, then started the rv-3 negative control",
            stderr="",
        )

    monkeypatch.setattr(rva, "run_agent_with_prompt_resilient", _fake_resilient)
    response_log_path = tmp_path / ".iar" / "evidence" / "verifier-response.txt"

    with pytest.raises(subprocess.TimeoutExpired):
        rva.run_verifier_agent(
            _issue(),
            tmp_path,
            "abc1234",
            _manifest(),
            "kimi",
            FakeProcessRunner(),
            timeout_seconds=1800,
            response_log_path=response_log_path,
        )

    saved = response_log_path.read_text(encoding="utf-8")
    assert "TIMED OUT" in saved
    assert "# timeout seconds: 1800" in saved
    assert "I checked rv-1 and rv-2, then started the rv-3 negative control" in saved


def test_run_verifier_agent_passes_both_timeouts_to_the_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """墙钟与静默期两条线都要传下去,否则慢与卡死无法区分。"""
    from backend.core.use_cases import run_verifier_agent as rva

    captured_kwargs: dict = {}

    def _fake_resilient(agent_name, prompt, worktree_path, process_runner, **kwargs):
        captured_kwargs.update(kwargs)
        return CommandResult((agent_name,), 0, format_verifier_verdict_marker("green"), "")

    monkeypatch.setattr(rva, "run_agent_with_prompt_resilient", _fake_resilient)

    rva.run_verifier_agent(
        _issue(),
        tmp_path,
        "abc1234",
        _manifest(),
        "kimi",
        FakeProcessRunner(),
        timeout_seconds=7200,
        inactivity_timeout_seconds=1200,
    )

    assert captured_kwargs["timeout_seconds"] == 7200
    assert captured_kwargs["inactivity_timeout_seconds"] == 1200


def test_run_verifier_gate_passes_configured_timeouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """两个超时值都必须来自配置,不能在代码里写死。"""
    from backend.core.use_cases import run_verifier_agent as rva

    captured_kwargs: dict = {}

    monkeypatch.setattr(rva, "load_evidence_manifest", lambda *a, **k: _manifest())
    monkeypatch.setattr(rva, "get_head_sha", lambda *a, **k: "abc1234")

    def _fake_run(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return ValidationVerdict(risk="green")

    monkeypatch.setattr(rva, "run_verifier_agent", _fake_run)
    config = AppConfig(
        validation=ValidationConfig(
            verifier_enabled=True,
            verifier_timeout_seconds=7200,
            verifier_inactivity_timeout_seconds=900,
        )
    )

    rva.run_verifier_gate(_structured_issue(), tmp_path, config, FakeProcessRunner(), "claude")

    assert captured_kwargs["timeout_seconds"] == 7200
    assert captured_kwargs["inactivity_timeout_seconds"] == 900


def _write_builder_evidence(tmp_path: Path) -> Path:
    """在 worktree 里放一份 builder 的最终树证据,返回证据目录。"""
    evidence_dir = tmp_path / ".iar" / "evidence"
    (evidence_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (evidence_dir / "rv-1.txt").write_text("2 passed in 4.44s\n", encoding="utf-8")
    (evidence_dir / "scripts" / "capture_rv-1.sh").write_text("pytest -q\n", encoding="utf-8")
    return evidence_dir


def test_run_verifier_gate_restores_evidence_the_verifier_overwrote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """verifier 复跑 capture 脚本会覆盖 builder 证据,门禁必须把它恢复回去。

    verifier 跑 negative control 时故意把代码改红,产出的红色输出会盖掉已经通过
    门禁的 rv-*.txt;不恢复的话发布给人审的就是这份自相矛盾的证据。
    """
    from backend.core.use_cases import run_verifier_agent as rva

    evidence_dir = _write_builder_evidence(tmp_path)
    monkeypatch.setattr(rva, "load_evidence_manifest", lambda *a, **k: _manifest())
    monkeypatch.setattr(rva, "get_head_sha", lambda *a, **k: "abc1234")

    def _verifier_that_overwrites_evidence(*args, **kwargs):
        (evidence_dir / "rv-1.txt").write_text("2 failed in 4.44s\n", encoding="utf-8")
        (evidence_dir / "verifier-response.txt").write_text("my verdict", encoding="utf-8")
        return ValidationVerdict(risk="green")

    monkeypatch.setattr(rva, "run_verifier_agent", _verifier_that_overwrites_evidence)
    config = AppConfig(validation=ValidationConfig(verifier_enabled=True))

    rva.run_verifier_gate(_structured_issue(), tmp_path, config, FakeProcessRunner(), "claude")

    assert (evidence_dir / "rv-1.txt").read_text(encoding="utf-8") == "2 passed in 4.44s\n"
    # verifier 自己的响应日志是它的产物，不能被恢复覆盖掉。
    assert (evidence_dir / "verifier-response.txt").read_text(encoding="utf-8") == "my verdict"


def test_run_verifier_gate_restores_evidence_when_verifier_times_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """超时被杀是污染最严重的场景:证据停在最后一个 negative control 的破坏态。"""
    from backend.core.use_cases import run_verifier_agent as rva

    evidence_dir = _write_builder_evidence(tmp_path)
    monkeypatch.setattr(rva, "load_evidence_manifest", lambda *a, **k: _manifest())
    monkeypatch.setattr(rva, "get_head_sha", lambda *a, **k: "abc1234")

    def _verifier_killed_mid_negative_control(*args, **kwargs):
        (evidence_dir / "rv-1.txt").write_text("2 failed in 4.44s\n", encoding="utf-8")
        raise subprocess.TimeoutExpired(cmd=["kimi"], timeout=1800)

    monkeypatch.setattr(rva, "run_verifier_agent", _verifier_killed_mid_negative_control)
    config = AppConfig(validation=ValidationConfig(verifier_enabled=True))

    with pytest.raises(subprocess.TimeoutExpired):
        rva.run_verifier_gate(_structured_issue(), tmp_path, config, FakeProcessRunner(), "claude")

    assert (evidence_dir / "rv-1.txt").read_text(encoding="utf-8") == "2 passed in 4.44s\n"


def test_run_verifier_gate_keeps_files_the_verifier_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """只恢复被改写/删除的文件;verifier 新建的文件保留,删别人刚写的更危险。"""
    from backend.core.use_cases import run_verifier_agent as rva

    evidence_dir = _write_builder_evidence(tmp_path)
    monkeypatch.setattr(rva, "load_evidence_manifest", lambda *a, **k: _manifest())
    monkeypatch.setattr(rva, "get_head_sha", lambda *a, **k: "abc1234")

    def _verifier_that_adds_a_file(*args, **kwargs):
        (evidence_dir / "verifier-notes.txt").write_text("probe log", encoding="utf-8")
        (evidence_dir / "rv-1.txt").unlink()
        return ValidationVerdict(risk="green")

    monkeypatch.setattr(rva, "run_verifier_agent", _verifier_that_adds_a_file)
    config = AppConfig(validation=ValidationConfig(verifier_enabled=True))

    rva.run_verifier_gate(_structured_issue(), tmp_path, config, FakeProcessRunner(), "claude")

    # 被删掉的 builder 证据要回来，verifier 自己的新文件留着。
    assert (evidence_dir / "rv-1.txt").read_text(encoding="utf-8") == "2 passed in 4.44s\n"
    assert (evidence_dir / "verifier-notes.txt").is_file()


def test_run_verifier_gate_missing_marker_message_does_not_blame_the_builder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """漏 marker 的阻断消息不得指使 builder 去修不存在的发现。

    这条消息同时是 attempt Detail 与 recovery prompt;以前两种成因共用
    "Fix what the verifier found",白烧过 attempt。
    """
    from backend.core.use_cases import run_verifier_agent as rva

    record: dict = {}
    _patch_gate_deps(
        monkeypatch,
        rva,
        ValidationVerdict(risk="red", findings="I had trouble.", marker_found=False),
        record,
    )
    config = AppConfig(validation=ValidationConfig(verifier_enabled=True))

    with pytest.raises(ValidationEvidenceError) as exc_info:
        rva.run_verifier_gate(_structured_issue(), tmp_path, config, FakeProcessRunner(), "claude")

    message = str(exc_info.value)
    assert "NO verdict marker" in message
    assert "verifier-side protocol failure" in message
    assert "do not invent fixes" in message
    assert "verifier-response.txt" in message
    assert "Fix what the verifier found" not in message
