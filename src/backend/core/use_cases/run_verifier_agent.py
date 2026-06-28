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
