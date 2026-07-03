"""Tests for the independent verifier verdict protocol (PR#2 T3)."""

from __future__ import annotations

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
