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

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import AppConfig, IssueSummary
from backend.core.use_cases.agent_runner_structured_evidence import (
    EvidenceManifest,
    ValidationEvidenceError,
    has_structured_evidence_marker,
    load_evidence_manifest,
)
from backend.core.use_cases.agent_runner_validation import validation_required
from backend.core.use_cases.run_agent_once import (
    extract_agent_response_text,
    get_head_sha,
    run_agent_with_prompt_resilient,
)

_logger = logging.getLogger(__name__)

_VALID_RISKS: tuple[str, ...] = ("green", "yellow", "red")
_VERDICT_MARKER_PATTERN = re.compile(
    r"<!--\s*iar:verifier-verdict\s+risk=(?P<risk>green|yellow|red)\s*-->"
)
_NO_VERDICT_FINDINGS = "No parseable verifier verdict marker found; treating as blocked (red)."
_VERIFIER_RESPONSE_FILENAME = "verifier-response.txt"


@dataclass(frozen=True)
class ValidationVerdict:
    """Independent verifier's structured verdict.

    ``risk`` 取值 {``green``, ``yellow``, ``red``}:green/yellow 放行(yellow
    附警告评论、不阻断),red 阻断发布并打回 builder。``findings`` 是人读的
    发现说明(对抗验证中发现的缝隙 / 风险)。

    ``marker_found`` 区分"verifier 真的判了 red"与"verifier 没吐出 verdict
    marker、被 fail-safe 当成 red"。两者都阻断,但成因完全不同:后者是 verifier
    侧的协议/可靠性故障,不代表 builder 的改动有缺陷。
    """

    risk: str
    findings: str = ""
    marker_found: bool = True

    @property
    def passed(self) -> bool:
        """green 或 yellow 视为放行(yellow 仅警告,不阻断发布)。"""
        return self.risk in ("green", "yellow")

    @property
    def blocks(self) -> bool:
        """red 阻断发布,走 repair 循环打回 builder。"""
        return self.risk == "red"

    @property
    def missing_marker(self) -> bool:
        """verifier 没有产出可解析的 verdict marker(fail-safe 记为 red)。"""
        return not self.marker_found


def format_verifier_verdict_marker(risk: str) -> str:
    """Format the hidden ``iar:verifier-verdict`` marker for a PR/Issue comment."""
    if risk not in _VALID_RISKS:
        raise ValueError(f"invalid verifier risk: {risk!r}; expected one of {_VALID_RISKS}")
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
        # findings 仍带回原始输出以便排查,但 marker_found=False 才是"没判定"的
        # 唯一可靠信号——以前只靠 findings 是否等于哨兵字符串来区分,而调用方
        # 总会填入响应文本,于是哨兵永远走不到,红判与漏 marker 无法分辨。
        return ValidationVerdict(
            risk="red",
            findings=findings or _NO_VERDICT_FINDINGS,
            marker_found=False,
        )
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
            oracle_lines.append(f"    negative control (must go RED): {block.negative_control}")
        if block.expected_fail:
            oracle_lines.append(f"    expected failure: {block.expected_fail}")
        if block.evidence_files:
            files = ", ".join(block.evidence_files)
            oracle_lines.append(f"    evidence artifacts on disk: {files}")
        for spec in block.expected_artifacts:
            claim = f" (key claim: {spec.key_claim!r})" if spec.key_claim else ""
            oracle_lines.append(
                f"    expected artifact: {spec.path} mime={spec.mime}"
                f" min_size={spec.min_size} min_duration={spec.min_duration_seconds}"
                f"{claim}"
            )
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
            "Multimodal evidence: the manifest's `evidence artifacts` may include",
            "images (.png/.jpg), videos (.webm/.mp4), audio, or other non-text",
            "files. Handle them with whatever your model natively supports — read",
            "the image directly if you can, use `ffmpeg` to extract a frame from",
            "a video if you can, listen to audio if you can. If you cannot read",
            "the artifact yourself, fall back to shell metadata: `stat <path>` for",
            "size, `file --mime <path>` for type, `ffprobe <path>` for duration /",
            "resolution. Never trust the file's existence or size alone — a 0-byte",
            "screenshot or a video with no frames is not evidence. State in your",
            "findings which artifacts you actually inspected and how.",
            "",
            "When the oracle includes `key_claim` assertions on an artifact, apply",
            "the D-14c fairness rule: IF you are a multimodal model that CAN read",
            "the file directly, verify the key_claim and report what you actually",
            "saw (green if confirmed, red if contradicted). IF you are a TEXT-ONLY",
            "model that CANNOT read images/video/audio, state 'I am text-only and",
            "cannot visually verify the key_claim; only file metadata was checked'.",
            "Encourage a self-downgrade to yellow in that case — but NEVER return",
            "red solely because you cannot read a non-text artifact. Red is for",
            "proven breaks only, not model-capability gaps.",
            "",
            "Be a skeptic. If you cannot independently confirm it works, the verdict",
            "is RED — do not give the builder the benefit of the doubt.",
            "",
            "End your report with concrete findings (what you ran, what you observed,",
            "any gap between intent and behavior), then exactly one final line:",
            "<!-- iar:verifier-verdict risk=green -->   (or yellow, or red)",
        ]
    )


def _write_verifier_response_log(
    response_log_path: Path,
    header_lines: list[str],
    response_text: str,
) -> Path | None:
    """把 verifier 的原始响应落盘,便于事后分辨"真红"与"漏 marker"。

    verifier 以 ``capture_output`` 运行,输出不进 stdout 也不逐行落日志;一旦
    判定阻断,操作者事后无从查证它到底说了什么。写盘失败绝不能影响门禁本身。

    Returns:
        实际写入的路径;写入失败时返回 ``None``。
    """
    try:
        response_log_path.parent.mkdir(parents=True, exist_ok=True)
        response_log_path.write_text(
            "\n".join([*header_lines, "", response_text]),
            encoding="utf-8",
        )
    except OSError as write_error:
        _logger.warning(
            "Could not save verifier response to %s: %s",
            response_log_path,
            write_error,
        )
        return None
    return response_log_path


def run_verifier_agent(
    issue: IssueSummary,
    worktree_path: Path,
    builder_sha: str,
    manifest: EvidenceManifest,
    verifier_agent: str,
    process_runner: IProcessRunner,
    *,
    timeout_seconds: int | None = None,
    response_log_path: Path | None = None,
) -> ValidationVerdict:
    """Run the independent verifier agent and return its parsed verdict.

    在 builder 提交点的(干净)worktree、用 ``verifier_agent``(应不同于 builder)
    跑 :func:`build_verifier_prompt`,捕获输出并 :func:`parse_verifier_verdict`。
    解析为 fail-safe:无可解析 verdict 即 ``red``。本函数只负责"跑 + 解析",
    是否启用、选哪个 agent、red 后如何 repair,由编排层决定。

    Args:
        response_log_path: 可选,把原始响应写到这里供事后排查(见
            :func:`_write_verifier_response_log`)。

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
    verdict = parse_verifier_verdict(response_text, findings=response_text.strip()[:4000])

    saved_log_path: Path | None = None
    if response_log_path is not None:
        saved_log_path = _write_verifier_response_log(
            response_log_path,
            [
                "# Independent verifier raw response",
                f"# issue: #{issue.number}",
                f"# verifier agent: {verifier_agent}",
                f"# builder sha: {builder_sha}",
                f"# parsed risk: {verdict.risk}",
                f"# verdict marker found: {'no' if verdict.missing_marker else 'yes'}",
                f"# response chars: {len(response_text)}",
            ],
            response_text,
        )
    if verdict.missing_marker:
        _logger.error(
            "Independent verifier (agent %r) emitted NO verdict marker for Issue #%d; "
            "fail-safe blocking as red. Raw response was %d chars%s — check whether it "
            "actually judged the change or just failed to follow the output protocol.",
            verifier_agent,
            issue.number,
            len(response_text),
            f", saved to {saved_log_path}" if saved_log_path else " (not saved)",
        )
    else:
        _logger.info(
            "Independent verifier (agent %r) returned %s for Issue #%d (%d chars%s).",
            verifier_agent,
            verdict.risk.upper(),
            issue.number,
            len(response_text),
            f", saved to {saved_log_path}" if saved_log_path else "",
        )
    return verdict


def _choose_verifier_agent(config: AppConfig, builder_agent: str) -> str:
    """Pick an agent for the verifier, preferring one different from the builder.

    ``verifier_agent`` 配成具体 agent 则用它;``auto`` 时从 fallback 链里挑
    第一个 ≠ builder 的(独立性来自换 model);都没有再退回 builder。
    """
    configured = config.validation.verifier_agent
    if configured and configured != "auto":
        return configured
    for candidate in config.runner.agent_fallback_order:
        if candidate != builder_agent:
            return candidate
    return builder_agent


def _format_verifier_block_message(
    *,
    verdict: ValidationVerdict,
    verifier_agent: str,
    issue_number: int,
    response_log_path: Path,
) -> str:
    """构建阻断消息:把"真判 red"与"没吐 verdict"讲成两件不同的事。

    这条消息既进 attempt 历史的 Detail 列,也进 builder 的 recovery prompt。以前
    两种成因共用一句"Fix what the verifier found",于是 verifier 只是漏了 marker
    时,builder 会被指使去修一个根本不存在的发现——白烧一轮 attempt(实证:
    freshai Issue #111,verifier 用的 agent 未产出 marker)。
    """
    if verdict.missing_marker:
        return (
            f"Independent verifier (agent '{verifier_agent}') produced NO verdict marker "
            f"for issue #{issue_number}, so the fail-safe protocol blocks it like RED. "
            "This is a verifier-side protocol failure, NOT a proven defect in the change: "
            "do not invent fixes for findings that do not exist. Its raw output was saved "
            f"to `{response_log_path}` — read it first. If it contains no actual finding, "
            "leave the implementation and evidence as they are; if the verifier agent keeps "
            "failing to emit the marker, that is a runner/agent problem to escalate rather "
            "than a code problem. Raw output head:\n"
            f"{verdict.findings}"
        )
    return (
        f"Independent verifier (agent '{verifier_agent}') returned RED for "
        f"issue #{issue_number}: it could not independently confirm the change "
        "does what the issue asks. Findings:\n"
        f"{verdict.findings}\n"
        "Fix what the verifier found, or correct the Realistic Validation "
        "oracle if the check itself is wrong."
    )


def run_verifier_gate(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    builder_agent: str,
) -> ValidationVerdict | None:
    """Pre-PR independent-verifier gate (PR#2 T3 integration).

    在 builder 通过证据门禁后、开 PR 之前运行:换一个 agent 对带结构化证据的
    issue 做独立对抗复验。

    - **red** → 抛 ``ValidationEvidenceError``,落进 builder 既有的 recovery
      循环(verifier findings 当 repair 反馈,自动重做、bounded;耗尽才升级给
      人)。因为是 pre-PR 门禁,所以"PR 存在 ⟹ verifier 已通过",无需额外的
      daemon 双门禁。
    - **yellow** → 记警告并放行,返回 verdict 供调用方在开 PR 后贴警告评论。
    - **green** → 放行,返回 verdict 供调用方在开 PR 后置 ``validation/verifier-passed`` label。

    默认开(``verifier_enabled``)。仅对带 ``iar:structured-evidence`` marker、且要求验证的 issue 生效。

    Returns:
        ``ValidationVerdict`` 当 verifier 实际运行(verdict 非 red 时返回,red
        时抛异常);``None`` 当 verifier 未启用 / issue 不要求验证 / 无结构化证据
        marker(调用方据此决定是否在 PR 上做 label/评论副作用)。

    Raises:
        ValidationEvidenceError: verifier 判定 red(经 recovery 自动打回 builder)。
    """
    if not config.validation.verifier_enabled:
        return None
    if not validation_required(issue.body, config):
        return None
    if not has_structured_evidence_marker(issue.body):
        return None

    manifest = load_evidence_manifest(worktree_path, config)
    verifier_agent = _choose_verifier_agent(config, builder_agent)
    builder_sha = get_head_sha(worktree_path, process_runner)
    response_log_path = worktree_path / config.validation.evidence_dir / _VERIFIER_RESPONSE_FILENAME
    verdict = run_verifier_agent(
        issue,
        worktree_path,
        builder_sha,
        manifest,
        verifier_agent,
        process_runner,
        timeout_seconds=config.validation.verifier_timeout_seconds,
        response_log_path=response_log_path,
    )
    if verdict.blocks:
        raise ValidationEvidenceError(
            _format_verifier_block_message(
                verdict=verdict,
                verifier_agent=verifier_agent,
                issue_number=issue.number,
                response_log_path=response_log_path,
            )
        )
    if verdict.risk == "yellow":
        _logger.warning(
            "Independent verifier returned YELLOW for issue #%d: %s",
            issue.number,
            verdict.findings,
        )
    return verdict


_PR_URL_NUMBER_PATTERN = re.compile(r"/pull/(?P<number>\d+)")


def _extract_pr_number(pr_url: str) -> int | None:
    """Extract the PR number from a GitHub PR URL.

    Returns ``None`` when the URL does not contain a parseable PR number,
    so callers can skip the post-PR verifier side effects gracefully.
    """
    match = _PR_URL_NUMBER_PATTERN.search(pr_url)
    if match is None:
        return None
    return int(match.group("number"))


def build_verifier_yellow_comment(verdict: ValidationVerdict, issue_number: int) -> str:
    """Build the PR warning comment for a yellow verifier verdict.

    ``yellow`` 不阻断发布,但要在 PR 上贴一条可见的警告评论,把 verifier 的
    findings 交给人审者——避免"verifier 提了风险但没人看到"。评论带 hidden
    marker,后续可幂等更新或清除。
    """
    return "\n".join(
        [
            "<!-- iar:verifier-warning risk=yellow -->",
            "",
            "## ⚠️ Independent verifier returned YELLOW",
            "",
            f"The independent verifier ran for issue #{issue_number} and found a "
            "non-blocking risk. Review the findings below before merging:",
            "",
            "<details>",
            "<summary>Verifier findings</summary>",
            "",
            verdict.findings.strip() or "(no findings emitted)",
            "",
            "</details>",
        ]
    )


def apply_verifier_verdict_to_pr(
    pr_url: str,
    verdict: ValidationVerdict | None,
    issue_number: int,
    *,
    verifier_passed_label: str,
    github_client: IGitHubClient,
) -> None:
    """Apply the verifier verdict to the PR after it is created.

    pre-PR verifier 跑完后,verdict 的副作用在 PR 创建后落地:

    - ``green`` → 置 ``validation/verifier-passed`` label,为后续 autopilot
      合并队列提供显式状态位。
    - ``yellow`` → 贴警告评论(findings 给人审者看),不阻断发布。
    - ``None`` (verifier 未启用 / issue 不要求验证) → 无操作。
    - ``red`` 不会到这里(pre-PR 阶段已抛 ``ValidationEvidenceError`` 进 recovery)。

    PR number 从 ``pr_url`` 解析;解析失败时记日志并跳过,不阻断发布。
    """
    if verdict is None:
        return
    pr_number = _extract_pr_number(pr_url)
    if pr_number is None:
        _logger.warning(
            "Could not parse PR number from URL %r; skipping verifier verdict "
            "side effects for issue #%d.",
            pr_url,
            issue_number,
        )
        return
    if verdict.risk == "green":
        github_client.edit_issue_labels(pr_number, add=(verifier_passed_label,))
        _logger.info(
            "Verifier GREEN for issue #%d: set label %r on PR #%d.",
            issue_number,
            verifier_passed_label,
            pr_number,
        )
    elif verdict.risk == "yellow":
        github_client.comment_pr(pr_number, build_verifier_yellow_comment(verdict, issue_number))
        _logger.info(
            "Verifier YELLOW for issue #%d: posted warning comment on PR #%d.",
            issue_number,
            pr_number,
        )
