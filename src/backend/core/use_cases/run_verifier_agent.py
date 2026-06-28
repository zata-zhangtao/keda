"""Independent verifier agent — verdict protocol (PR#2 T3).

T3 在 builder 提交之后、开 PR 之前,由一个**独立 verifier agent**(尽量换一个
model、在 builder 提交点的干净 worktree)从需求意图独立复现真实入口并对抗性
证伪,产出结构化 verdict 喂给发布门禁。

本模块先承载 **verdict 协议**:模型、隐藏 marker、以及确定性解析。解析遵循
fail-safe——没有可解析的 verdict(verifier 没产出 / 产出畸形)一律按 ``red``
(阻断)处理,绝不让"无判定"静默放行。所有 marker 与 ``agent_runner_events``
的 ``iar:`` hidden marker 同型。

后续切片再加 ``build_verifier_prompt`` 与 ``run_verifier_agent``(agent 调用、
干净 worktree、编排接入),以及 daemon 的"人工签收 + verifier-passed"双门禁。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner
from backend.core.shared.models.agent_runner import IssueSummary
from backend.core.use_cases.agent_runner_structured_evidence import EvidenceManifest
from backend.core.use_cases.run_agent_once import (
    extract_agent_response_text,
    run_agent_with_prompt_resilient,
)

_VALID_RISKS: tuple[str, ...] = ("green", "yellow", "red")
_VERDICT_MARKER_PATTERN = re.compile(
    r"<!--\s*iar:verifier-verdict\s+risk=(?P<risk>green|yellow|red)\s*-->"
)
_NO_VERDICT_FINDINGS = (
    "No parseable verifier verdict marker found; treating as blocked (red)."
)


@dataclass(frozen=True)
class ValidationVerdict:
    """Independent verifier's structured verdict.

    ``risk`` 取值 {``green``, ``yellow``, ``red``}:green/yellow 放行(yellow
    附警告评论、不阻断),red 阻断发布并打回 builder。``findings`` 是人读的
    发现说明(对抗验证中发现的缝隙 / 风险)。
    """

    risk: str
    findings: str = ""

    @property
    def passed(self) -> bool:
        """green 或 yellow 视为放行(yellow 仅警告,不阻断发布)。"""
        return self.risk in ("green", "yellow")

    @property
    def blocks(self) -> bool:
        """red 阻断发布,走 repair 循环打回 builder。"""
        return self.risk == "red"


def format_verifier_verdict_marker(risk: str) -> str:
    """Format the hidden ``iar:verifier-verdict`` marker for a PR/Issue comment."""
    if risk not in _VALID_RISKS:
        raise ValueError(
            f"invalid verifier risk: {risk!r}; expected one of {_VALID_RISKS}"
        )
    return f"<!-- iar:verifier-verdict risk={risk} -->"


def parse_verifier_verdict(text: str, *, findings: str = "") -> ValidationVerdict:
    """Deterministically parse the latest verifier-verdict marker from ``text``.

    Fail-safe:没有可解析的 marker 一律返回 ``red``——绝不把"无判定"当成
    通过。纯确定性解析,不引入 LLM。当文本里有多个 marker(例如 repair 后
    重新裁决),取最后一个。

    Args:
        text: verifier agent 的输出 / PR 评论文本。
        findings: 可选的人读发现说明,原样带入 verdict。

    Returns:
        解析出的 ``ValidationVerdict``;无 / 畸形 marker 时为 red。
    """
    latest_match = None
    for latest_match in _VERDICT_MARKER_PATTERN.finditer(text):
        pass
    if latest_match is None:
        return ValidationVerdict(risk="red", findings=findings or _NO_VERDICT_FINDINGS)
    return ValidationVerdict(risk=latest_match.group("risk"), findings=findings)


def build_verifier_prompt(
    issue: IssueSummary,
    builder_sha: str,
    manifest: EvidenceManifest,
) -> str:
    """Assemble the independent-verifier prompt.

    给 verifier 注入需求意图(issue 正文)与验收 oracle(manifest 各项的真实
    入口命令 / 负控 / 期望失败),并下达"从意图独立出题、亲自跑真实入口、对抗
    证伪、跑负控、给 verdict"的指令;明令不许信 builder 的绿,确认不了即 red。
    """
    oracle_lines: list[str] = []
    for block in manifest.items:
        oracle_lines.append(f"- Item {block.item_number} ({block.item_name}):")
        oracle_lines.append(f"    real entry / command: {block.command}")
        if block.negative_control:
            oracle_lines.append(
                f"    negative control (must go RED): {block.negative_control}"
            )
        if block.expected_fail:
            oracle_lines.append(f"    expected failure: {block.expected_fail}")
    oracle_block = "\n".join(oracle_lines) or "(no structured oracle items)"

    return "\n".join(
        [
            f"You are an INDEPENDENT verifier for issue #{issue.number}: "
            f'"{issue.title}". A different agent (the builder) already implemented',
            f"it at commit {builder_sha} in this worktree. Your job is NOT to trust",
            "their work — it is to independently decide whether the feature actually",
            "does what was asked, and to try to prove it does NOT.",
            "",
            "What it is supposed to do (derive your OWN checks from this; do not just",
            "re-run the builder's tests):",
            issue.body.strip(),
            "",
            "The acceptance oracle it was meant to satisfy:",
            oracle_block,
            "",
            "Do this:",
            '1. From the intent above, independently decide what "working" means.',
            "   Do not assume the builder tested the right thing.",
            "2. Run the real entry point yourself in this worktree and observe the",
            "   result with your own eyes. Do not mock or stub the thing under test.",
            '3. Adversarially probe the gap between "tests pass" and "a real user runs',
            '   it and it works": edge cases, empty/invalid input, the real end-to-end',
            "   path, anything the happy-path tests would miss.",
            "4. Run each negative control to confirm the check actually goes RED when",
            "   the feature is broken — a test that cannot fail proves nothing.",
            "5. Verdict: green = you independently confirmed it (incl. adversarial",
            "   probes); yellow = main path works but a real, non-blocking gap/risk",
            "   (state it); red = does NOT match the intent, OR you found a break, OR",
            "   you could not independently confirm it.",
            "",
            "Be a skeptic. If you cannot independently confirm it works, the verdict",
            "is RED — do not give the builder the benefit of the doubt.",
            "",
            "End your report with concrete findings (what you ran, what you observed,",
            "any gap between intent and behavior), then exactly one final line:",
            "<!-- iar:verifier-verdict risk=green -->   (or yellow, or red)",
        ]
    )


def run_verifier_agent(
    issue: IssueSummary,
    worktree_path: Path,
    builder_sha: str,
    manifest: EvidenceManifest,
    verifier_agent: str,
    process_runner: IProcessRunner,
    *,
    timeout_seconds: int | None = None,
) -> ValidationVerdict:
    """Run the independent verifier agent and return its parsed verdict.

    在 builder 提交点的(干净)worktree、用 ``verifier_agent``(应不同于 builder)
    跑 :func:`build_verifier_prompt`,捕获输出并 :func:`parse_verifier_verdict`。
    解析为 fail-safe:无可解析 verdict 即 ``red``。本函数只负责"跑 + 解析",
    是否启用、选哪个 agent、red 后如何 repair,由编排层决定。

    Returns:
        ``ValidationVerdict``;verifier 的输出文本作为 ``findings`` 带回。
    """
    prompt = build_verifier_prompt(issue, builder_sha, manifest)
    result = run_agent_with_prompt_resilient(
        verifier_agent,
        prompt,
        worktree_path,
        process_runner,
        capture_output=True,
        timeout_seconds=timeout_seconds,
        issue=issue,
    )
    response_text = extract_agent_response_text(result)
    return parse_verifier_verdict(response_text, findings=response_text.strip()[:4000])
