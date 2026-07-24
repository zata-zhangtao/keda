"""Prompt, recovery, and PRD delivery helpers for the agent runner."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from backend.core.agent.memory import (
    RelevantMemory,
    format_skill_catalog,
    load_relevant_memory,
    match_skills_and_memory,
)
from backend.core.agent.memory.protocols import (
    ILongTermMemoryStore,
    ISkillStore,
)
from backend.core.shared.interfaces.agent_runner import IProcessRunner
from backend.core.shared.models.agent_runner import (
    CommandResult,
    IssueSummary,
    MemoryConfig,
    PromptConfig,
)
from backend.core.shared.prd_change_log import (
    extract_prd_change_log_entry_count,
    parse_prd_change_log,
)
from backend.core.shared.prd_checklist import parse_prd_checklist
from backend.core.use_cases.agent_runner_structured_evidence import (
    build_structured_evidence_prompt_suffix,
    has_structured_evidence_marker,
    parse_structured_evidence_marker,
)

_MAX_RECOVERY_OUTPUT_LENGTH = 12000

# Default ceiling for inlining the canonical PRD inside the implementation /
# recovery / continuation prompts. PRDs longer than this are truncated with a
# pointer to the full file. Exposed as a parameter so future PRDs (or repos)
# can raise or lower the ceiling without forking this helper.
_DEFAULT_PRD_INLINE_MAX_CHARS = 20000


class VerificationFailedError(RuntimeError):
    """Raised when configured verification commands do not pass."""

    def __init__(self, verification_results: list[CommandResult]) -> None:
        self.verification_results = verification_results
        super().__init__(format_verification_failure(verification_results))


class PrdDeliveryError(RuntimeError):
    """Raised when the canonical PRD is not ready for delivery."""


def extract_prd_path(issue_body: str) -> str | None:
    """Extract a canonical PRD path from an Issue body.

    Only matches ``PRD path:`` anchors that start a line, optionally as a
    Markdown list item. Inline occurrences (e.g. prose mentioning ``PRD path:``
    in backticks) are ignored so that feature descriptions do not shadow the
    canonical anchor. Captured paths that do not look like a relative file path
    (e.g. contain whitespace or no directory separator) are rejected, allowing
    a later well-formed anchor to be used instead.
    """
    for match in re.finditer(
        r"(?:^|\n)\s*(?:[-*]\s+)?PRD path:\s*`([^`]+)`",
        issue_body,
    ):
        prd_path = match.group(1).strip()
        if prd_path and "/" in prd_path and not any(char.isspace() for char in prd_path):
            return prd_path
    return None


_DEFAULT_EXECUTION_TEMPLATE = "\n".join(
    [
        "Complete GitHub Issue #{issue_number}: {issue_title}",
        "",
        "Issue URL: {issue_url}",
        "Worktree: {worktree_path}",
        "{prd_line}",
        "{validation_line}",
        "{memory_block}",
        "",
        "Issue body:",
        "{issue_body}",
        "",
        "Verification commands the runner will run before committing:",
        "{verification_commands_summary}",
        "",
        "Execution rules:",
        "- Read AGENTS.md and follow repository instructions.",
        "- Check project conventions (naming, dependency direction, file encoding, "
        "max line length, etc.) before requesting a commit.",
        "- Only modify files inside the current worktree.",
        "- Do not merge main, delete branches, push, or create PRs; the runner handles publishing.",
        "- Do not run `git commit`, `git reset`, `git checkout`, or any "
        "other command that mutates the git index; the runner handles staging and commits.",
        "- After finishing your changes, run the verification commands above "
        "locally if possible, then request a commit by writing "
        "`.agent-runner/commit-request.json` as JSON with `commit_message`.",
        "- If you run any verification command (e.g. `just test`) and then modify "
        "any tracked files afterwards, re-run that verification command before "
        "requesting a commit. The runner may re-verify the staged tree at commit time.",
        "- Do not touch production systems or real business data.",
        "- Implement the requested task with focused tests and docs updates.",
        "- Finish with a concise summary, tests run, and remaining risk.",
    ]
)


def _read_prd_text(prd_path: Path) -> str | None:
    """Return the canonical PRD file contents, or ``None`` if unreadable.

    Reads with ``encoding="utf-8"`` (project rule) and swallows
    :class:`OSError` so a transient filesystem error during prompt build does
    not crash the runner — the caller falls back to the pointer line in that
    case, which still tells the agent where the PRD lives.
    """
    try:
        return prd_path.read_text(encoding="utf-8")
    except OSError:
        return None


# 结构化 Change Log 的唯一可解析格式：每条记录是一个 ``###`` 标题，后跟六个
# bullet 字段。解析器 ``parse_prd_change_log`` 只识别这种结构；Markdown 表格行
# 会被数成 0 条。历史上 agent 把 Change Log 写成表格后，门禁反复判定“未追加
# Change Log 条目”，而 prompt 从未说明必须用 ``###`` + bullet，导致 recovery
# 每轮往表格里再补一行、永不收敛的死循环。prompt 与失败反馈共用本样例，确保
# agent 拿到的格式说明与门禁实际校验的格式严格一致。
_PRD_CHANGE_LOG_FORMAT_EXAMPLE = "\n".join(
    [
        "Change Log entries MUST use this exact Markdown structure — each entry is a "
        "`###` heading followed by six bullet fields. Markdown tables are NOT parsed "
        "and count as zero entries (this is the #1 cause of repeated delivery failures):",
        "",
        "## Change Log",
        "",
        "### <short title of this change>",
        "- Type: <scope / evidence / test / doc / ...>",
        "- Before: <prior wording or state>",
        "- After: <new wording or state>",
        "- Reason: <why the PRD changed>",
        "- Impact: <effect on deliverables and requirements>",
        "- Review: <review status>",
    ]
)


def _build_prd_closeout_instruction(prd_relative_path: str) -> str:
    """构建所有 Agent prompt 共用的 PRD 演进规则。"""
    return (
        "The PRD may evolve during implementation, but Change Log and Acceptance "
        "Checklist are separate: when changing the PRD, append a `## Change Log` "
        "entry with Type, Before, After, Reason, Impact, and Review. Only mark an "
        "Acceptance Checklist item after its stated behavior was actually executed "
        "and evidenced. Never weaken a user-visible, security, scope, or realistic "
        "validation requirement without recording the change and its review status. "
        "Do not move the PRD to `tasks/archive/`; the runner archives it after gates pass. "
        f"Canonical PRD: `{prd_relative_path}`.\n\n"
        f"{_PRD_CHANGE_LOG_FORMAT_EXAMPLE}"
    )


def _build_prd_context_block(
    issue: IssueSummary,
    worktree_path: Path,
    *,
    max_chars: int = _DEFAULT_PRD_INLINE_MAX_CHARS,
) -> str:
    """Render the PRD reference block for implementation/recovery/continuation prompts.

    When the Issue references a canonical PRD and the file exists inside
    ``worktree_path``, the full PRD text is inlined (up to ``max_chars``)
    so the agent runs with the complete specification in context instead of
    needing a separate read pass. When the file is missing or exceeds the
    ceiling, a pointer line is returned so the agent still knows where the
    PRD lives.

    Args:
        issue: The Issue being processed.
        worktree_path: Repository worktree the agent will operate in.
        max_chars: Maximum number of characters of PRD body to inline before
            falling back to a pointer line with the full path.
    """
    prd_relative_path = extract_prd_path(issue.body)
    if not prd_relative_path:
        return "If the Issue references a PRD, read it before editing."

    closeout_instruction = _build_prd_closeout_instruction(prd_relative_path)
    prd_path = worktree_path / prd_relative_path
    prd_text = _read_prd_text(prd_path)
    if prd_text is None:
        return f"Also read the canonical PRD at `{prd_relative_path}`. {closeout_instruction}"

    if len(prd_text) <= max_chars:
        inline_section = prd_text.rstrip()
    else:
        truncation_note = (
            f"[PRD body truncated to {max_chars} chars; "
            f"read the full canonical PRD at `{prd_relative_path}` for the "
            "remaining context.]"
        )
        inline_section = prd_text[:max_chars].rstrip() + "\n\n" + truncation_note

    return (
        f"The canonical PRD is inlined below from `{prd_relative_path}`. "
        f"{closeout_instruction}\n\n"
        "--- BEGIN PRD ---\n"
        f"{inline_section}\n"
        "--- END PRD ---"
    )


def build_prompt(
    issue: IssueSummary,
    worktree_path: Path,
    prompt_config: PromptConfig,
    phase: str = "execution",
    *,
    validation_line: str = "",
    verification_commands_summary: str = "",
    memory_config: MemoryConfig | None = None,
    relevant_memory: RelevantMemory | None = None,
    long_term_store: ILongTermMemoryStore | None = None,
    skill_store: ISkillStore | None = None,
) -> str:
    """Build the prompt sent to the local AI agent from a template.

    Args:
        issue: 当前处理的 Issue。
        worktree_path: agent 工作的 worktree 路径。
        prompt_config: prompt 模板配置。
        phase: 模板阶段名。
        validation_line: Realistic Validation 强制执行指令；不要求证据的
            Issue 传空字符串。仅当模板含 ``{validation_line}`` 占位符时生效。
        verification_commands_summary: 当前 runner 会执行的 verification
            命令列表说明；传给 agent 让它提前了解交付门禁。
        memory_config: Memory configuration; when provided and enabled the
            runner loads the relevant long-term memory + promoted skills
            and inlines them into the prompt as separate context sections.
        relevant_memory: Pre-loaded :class:`RelevantMemory`; when supplied
            the loader is skipped (used in tests and recursive prompts).
        long_term_store: Optional injected long-term store.
        skill_store: Optional injected skill store.
    """
    template = prompt_config.phases.get(phase, _DEFAULT_EXECUTION_TEMPLATE)
    prd_line = _build_prd_context_block(issue, worktree_path)
    memory_block = _build_memory_block(
        issue,
        worktree_path,
        memory_config=memory_config,
        relevant_memory=relevant_memory,
        long_term_store=long_term_store,
        skill_store=skill_store,
    )
    return template.format(
        issue_number=issue.number,
        issue_title=issue.title,
        issue_url=issue.url,
        worktree_path=worktree_path,
        issue_body=issue.body,
        prd_line=prd_line,
        validation_line=validation_line,
        verification_commands_summary=verification_commands_summary,
        memory_block=memory_block,
    )


def truncate_recovery_output(output_text: str) -> str:
    """Limit command output included in recovery prompts and failure comments."""
    if len(output_text) <= _MAX_RECOVERY_OUTPUT_LENGTH:
        return output_text
    return "\n".join(
        [
            "[output truncated; showing tail]",
            output_text[-_MAX_RECOVERY_OUTPUT_LENGTH:],
        ]
    )


def format_result_for_recovery(result: CommandResult) -> str:
    """Format one command result for a recovery prompt."""
    return "\n".join(
        [
            f"Command: `{shlex.join(result.command)}`",
            f"Exit code: {result.return_code}",
            "stdout:",
            "```text",
            truncate_recovery_output(result.stdout),
            "```",
            "stderr:",
            "```text",
            truncate_recovery_output(result.stderr),
            "```",
        ]
    )


def failed_verification_results(
    verification_results: list[CommandResult],
) -> list[CommandResult]:
    """Return failed command results from a verification run."""
    return [result for result in verification_results if result.return_code != 0]


def format_verification_failure(verification_results: list[CommandResult]) -> str:
    """Format configured verification failures for logs and Issue comments."""
    failed_results = failed_verification_results(verification_results)
    if not failed_results:
        return "Verification failed without a captured failing command."
    first_failed_result = failed_results[0]
    return "\n".join(
        [
            f"Command failed: {shlex.join(first_failed_result.command)}",
            f"Exit code: {first_failed_result.return_code}",
            "stdout:",
            truncate_recovery_output(first_failed_result.stdout),
            "stderr:",
            truncate_recovery_output(first_failed_result.stderr),
        ]
    )


def resolve_prd_archive_path(prd_relative_path: str) -> str | None:
    """Convert a pending PRD path to its archive counterpart.

    Returns None when the path is not under ``tasks/pending/``.
    """
    path = Path(prd_relative_path)
    if len(path.parts) >= 2 and path.parts[0] == "tasks" and path.parts[1] == "pending":
        return str(Path("tasks") / "archive" / path.name)
    return None


def _format_unchecked_items(
    unchecked_items: list[tuple[int, str]],
) -> str:
    """Format unchecked checklist items for error messages."""
    return "\n".join(f"  - L{line}: {text}" for line, text in unchecked_items)


def _validate_prd_checklist(
    file_content: str,
    prd_relative_path: str,
) -> None:
    """Validate that a PRD has a checklist section and no unchecked items.

    Args:
        file_content: PRD file text.
        prd_relative_path: Relative path used in error messages.

    Raises:
        PrdDeliveryError: When the checklist section is missing or has
            unchecked items.
    """
    checklist_result = parse_prd_checklist(file_content)
    if not checklist_result.section_found:
        raise PrdDeliveryError(f"Acceptance Checklist section missing in {prd_relative_path}")
    if checklist_result.unchecked_items:
        unchecked_summary = _format_unchecked_items(checklist_result.unchecked_items)
        raise PrdDeliveryError(
            f"Acceptance Checklist has unchecked items in {prd_relative_path}:\n{unchecked_summary}"
        )


def _validate_prd_change_log(
    *,
    file_content: str,
    baseline_content: str | None,
    prd_relative_path: str,
) -> None:
    """当本轮修改 canonical PRD 时，要求附带完整 Change Log。"""
    if baseline_content is None or file_content == baseline_content:
        return
    baseline_entry_count = extract_prd_change_log_entry_count(baseline_content)
    change_log_result = parse_prd_change_log(file_content)
    if not change_log_result.section_found:
        raise PrdDeliveryError(
            f"Canonical PRD changed without a Change Log section: {prd_relative_path}"
        )
    if change_log_result.entry_count == 0:
        raise PrdDeliveryError(
            f"Canonical PRD changed without a Change Log entry: {prd_relative_path} "
            "(a `## Change Log` section exists but no entry was parsed; entries must be "
            "`###` headings with bullet fields — Markdown table rows are not counted)"
        )
    if change_log_result.entry_count <= baseline_entry_count:
        raise PrdDeliveryError(
            f"Canonical PRD changed without appending a Change Log entry: {prd_relative_path}"
        )
    if change_log_result.incomplete_entry_fields:
        missing_by_entry = "; ".join(
            f"entry {entry_number}: {', '.join(missing_fields)}"
            for entry_number, missing_fields in change_log_result.incomplete_entry_fields.items()
        )
        raise PrdDeliveryError(
            f"Canonical PRD Change Log is incomplete in {prd_relative_path}: {missing_by_entry}"
        )


def ensure_prd_delivery_ready(
    issue: IssueSummary,
    worktree_path: Path,
    process_runner: IProcessRunner,
    *,
    prd_baseline_content: str | None = None,
) -> None:
    """Validate canonical PRD state and auto-archive if complete.

    Raises:
        PrdDeliveryError: When the PRD is not ready for delivery.
    """
    prd_relative_path = extract_prd_path(issue.body)
    if not prd_relative_path:
        return

    prd_path = worktree_path / prd_relative_path
    if prd_path.exists():
        file_content = prd_path.read_text(encoding="utf-8")
        _validate_prd_change_log(
            file_content=file_content,
            baseline_content=prd_baseline_content,
            prd_relative_path=prd_relative_path,
        )
        _validate_prd_checklist(file_content, prd_relative_path)

        archive_relative_path = resolve_prd_archive_path(prd_relative_path)
        if archive_relative_path:
            archive_path = worktree_path / archive_relative_path
            archive_dir = archive_path.parent
            if not archive_dir.exists():
                raise PrdDeliveryError(
                    "Archive directory does not exist: "
                    f"{archive_dir.relative_to(worktree_path).as_posix()}"
                )
            # The PRD may exist on disk but be absent from the git index — for
            # example when a PRD rewrite/regeneration step overwrote it without
            # re-staging, leaving it untracked or with its deletion staged.
            # ``git mv`` resolves its source through the index (not the
            # filesystem), so it aborts with "not under version control" in that
            # state. Stage the on-disk PRD first so the archive move succeeds
            # regardless of how the file reached the worktree.
            process_runner.run(
                ["git", "add", "--", str(prd_relative_path)],
                cwd=worktree_path,
            )
            process_runner.run(
                [
                    "git",
                    "mv",
                    str(prd_relative_path),
                    str(archive_relative_path),
                ],
                cwd=worktree_path,
            )
        return

    archive_relative_path = resolve_prd_archive_path(prd_relative_path)
    if archive_relative_path:
        archive_path = worktree_path / archive_relative_path
        if archive_path.exists():
            file_content = archive_path.read_text(encoding="utf-8")
            # Agent 违规自行 ``git mv`` 到 archive/ 时会走到这里。若不同样校验
            # Change Log，PRD 只要落进 archive 就绕过了变更审计门禁。对 runner
            # 在上一轮已合法归档、本轮内容相对 baseline 未变的场景，
            # ``_validate_prd_change_log`` 会因 ``file_content == baseline_content``
            # 或条目数仍高于 baseline 而放行，不会误伤。
            _validate_prd_change_log(
                file_content=file_content,
                baseline_content=prd_baseline_content,
                prd_relative_path=archive_relative_path,
            )
            _validate_prd_checklist(file_content, archive_relative_path)
            return

    raise PrdDeliveryError(f"Canonical PRD not found: {prd_relative_path}")


def assert_prd_archived_for_publish(
    issue: IssueSummary,
    worktree_path: Path,
) -> None:
    """Read-only PRD archive gate used immediately before ``git push``.

    Unlike :func:`ensure_prd_delivery_ready`, this helper does **not** move
    files; it only asserts that a canonical PRD (if present) is already under
    ``tasks/archive/`` and its Acceptance Checklist is complete. This is the
    final hard gate inside the runner before creating a PR.

    The Issue body may still reference the original ``tasks/pending/`` path,
    because ``git mv`` only moves the file and does not rewrite the Issue.
    This gate resolves the canonical archive path from the recorded path and
    verifies the file is actually there.

    Args:
        issue: The Issue about to be published.
        worktree_path: The agent worktree path.

    Raises:
        PrdDeliveryError: When the PRD exists but is not archived or still has
            unchecked items.
    """

    prd_relative_path = extract_prd_path(issue.body)
    if not prd_relative_path:
        return

    archive_relative_path = resolve_prd_archive_path(prd_relative_path)
    if archive_relative_path is None:
        path_parts = Path(prd_relative_path).parts
        if len(path_parts) >= 2 and path_parts[0] == "tasks" and path_parts[1] == "archive":
            archive_relative_path = prd_relative_path
        else:
            raise PrdDeliveryError(
                f"Canonical PRD must be archived before publishing: {prd_relative_path}"
            )

    archive_path = worktree_path / archive_relative_path
    if not archive_path.exists():
        raise PrdDeliveryError(f"Archived PRD not found in worktree: {archive_relative_path}")

    # If the Issue still points at the pending path, ensure the pending file is
    # gone so the archive copy is unambiguously the canonical one.
    if prd_relative_path != archive_relative_path:
        pending_path = worktree_path / prd_relative_path
        if pending_path.exists():
            raise PrdDeliveryError(f"PRD is still present at pending path: {prd_relative_path}")

    file_content = archive_path.read_text(encoding="utf-8")
    _validate_prd_checklist(file_content, archive_relative_path)


def format_prd_delivery_detail(message: str) -> str:
    """Build the recorded attempt detail for a PRD delivery failure.

    Keeps the specific failure ``message`` as the last line so the attempt
    history Detail column surfaces the real reason. The generic "update the
    PRD" instruction belongs only in the recovery prompt
    (:func:`format_prd_delivery_failure`), never in the diagnostic record.
    """
    return "\n".join(
        [
            "PRD delivery check failed.",
            message,
        ]
    )


def format_prd_delivery_failure(message: str) -> str:
    """Build the failure section for a PRD delivery recovery prompt."""
    return "\n".join(
        [
            format_prd_delivery_detail(message),
            "Complete the missing real work and evidence before marking its Acceptance "
            "Checklist item. If the PRD itself must change, append a structured Change Log "
            "entry; do not move the PRD to tasks/archive/.",
        ]
    )


def ensure_verification_passed(verification_results: list[CommandResult]) -> None:
    """Raise when any configured verification command failed."""
    if failed_verification_results(verification_results):
        raise VerificationFailedError(verification_results)


def build_fix_prompt(
    issue: IssueSummary,
    worktree_path: Path,
    *,
    verification_results: list[CommandResult],
    verification_commands_summary: str = "",
) -> str:
    """Build a focused prompt for the Fix Agent layer.

    The Fix Agent only repairs the current verification failure. It must not
    modify evidence files, PRD checklists, or ``.agent-runner/commit-request.json``
    unless those files are directly part of the failing verification output.

    Args:
        issue: The Issue being processed.
        worktree_path: Agent worktree path.
        verification_results: The failed verification results to repair.
        verification_commands_summary: Full list of verification commands the
            runner runs, so the Fix Agent knows the complete delivery gate.

    Returns:
        Prompt text for the Fix Agent.
    """
    failed_results = failed_verification_results(verification_results)
    failure_text = (
        "\n\n".join(format_result_for_recovery(result) for result in failed_results)
        if failed_results
        else "Verification failed without a captured failing command."
    )
    commands_section = ""
    if verification_commands_summary:
        commands_section = (
            "The runner will re-run the following verification commands after your "
            f"fix (in order; the first failure stops the chain):\n{verification_commands_summary}"
        )
    return "\n".join(
        [
            f"Fix the verification failure for GitHub Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            f"Worktree: {worktree_path}",
            "",
            "The runner detected the following verification failure:",
            failure_text,
            "",
            commands_section,
            "Fix rules:",
            "- Only modify files inside the current worktree.",
            "- Only fix the code or tests that caused the verification failure above.",
            "- Before requesting a commit, re-check project conventions "
            "(naming, dependency direction, file encoding, max line length, etc.).",
            "- Do not update evidence files, PRD Acceptance Checklists, or commit requests.",
            "- Do not switch branches, merge main, push, or create PRs.",
            "- Do not run `git add`, `git commit`, `git reset`, `git checkout`, or any "
            "other command that mutates the git index; the runner handles staging and commits.",
            "- If your fix requires re-running verification commands (e.g. `just test`) "
            "because you modified files after the previous run, re-run them before "
            "requesting a commit.",
            "- After fixing the failure, write or update "
            "`.agent-runner/commit-request.json` as JSON with `commit_message`.",
            "- Finish with a concise summary of the fix.",
        ]
    )


def _build_memory_block(
    issue: IssueSummary,
    worktree_path: Path,
    *,
    memory_config: MemoryConfig | None,
    relevant_memory: RelevantMemory | None,
    long_term_store: ILongTermMemoryStore | None = None,
    skill_store: ISkillStore | None = None,
) -> str:
    """Render the long-term memory + skill catalog block for a prompt.

    When ``memory_config`` is missing or disabled, or no relevant memory
    exists on disk, an empty string is returned. Otherwise the block lists
    long-term facts (one per line) and the skill catalog (name + path only,
    no full body) so the agent knows which skills are available and where
    to read them.

    When ``long_term_store`` / ``skill_store`` are not provided, the
    composition-root factory from ``infrastructure/memory`` is invoked
    lazily. This keeps the historical 3-argument call sites (tests) working
    while letting the use cases inject pre-built stores to avoid redundant
    disk I/O on the recovery path.
    """
    if (
        relevant_memory is None
        and memory_config is not None
        and memory_config.enabled
        and worktree_path is not None
    ):
        long_term_store, skill_store = _ensure_memory_stores(
            worktree_path, memory_config, long_term_store, skill_store
        )
        if long_term_store is not None and skill_store is not None:
            relevant_memory = load_relevant_memory(
                issue,
                worktree_path,
                memory_config,
                long_term_store=long_term_store,
                skill_store=skill_store,
            )
    if relevant_memory is None or relevant_memory.is_empty:
        return ""
    sections: list[str] = []
    if relevant_memory.long_term_facts:
        fact_lines = [
            f"- [{fact.category}/{fact.topic}] {fact.content}"
            for fact in relevant_memory.long_term_facts
        ]
        sections.append(
            "Project conventions / long-term memory (from .iar/memory/long_term/):\n"
            + "\n".join(fact_lines)
        )
    catalog = format_skill_catalog(
        relevant_memory.promoted_skills,
        header=("Available skills (read the file when relevant; do not inline the body):"),
    )
    if catalog:
        sections.append(catalog)
    return "\n\n".join(sections)


def _ensure_memory_stores(
    worktree_path: Path,
    memory_config: MemoryConfig,
    long_term_store: ILongTermMemoryStore | None,
    skill_store: ISkillStore | None,
) -> tuple[ILongTermMemoryStore | None, ISkillStore | None]:
    """Resolve long-term + skill stores for prompt injection.

    When the caller has already injected them, return as-is. Otherwise build
    them on demand via ``core/agent/memory/_composition.py`` which
    dynamically loads the ``infrastructure/`` implementations, preserving
    the strict ``core -> infrastructure`` ban. Returns ``(None, None)``
    when memory is disabled.
    """
    if not memory_config.enabled:
        return None, None
    if long_term_store is not None and skill_store is not None:
        return long_term_store, skill_store
    from backend.core.agent.memory._composition import (
        build_default_memory_services,
    )

    services = build_default_memory_services(worktree_path, memory_config)
    return services.long_term, services.skill


def build_recovery_prompt(
    issue: IssueSummary,
    worktree_path: Path,
    *,
    recovery_attempt: int,
    max_recovery_attempts: int,
    failure_summary: str,
    verification_results: list[CommandResult] | None = None,
    memory_config: MemoryConfig | None = None,
    failure_type: str | None = None,
    long_term_store: ILongTermMemoryStore | None = None,
    skill_store: ISkillStore | None = None,
) -> str:
    """Build a prompt that asks the agent to repair a failed attempt."""
    prd_path = extract_prd_path(issue.body)
    if prd_path:
        prd_closeout = _build_prd_closeout_instruction(prd_path)
        prd_context_block = _build_prd_context_block(issue, worktree_path)
        prd_line = f"Re-check the canonical PRD below. {prd_closeout}\n\n{prd_context_block}"
    else:
        prd_line = "If the Issue references a PRD, re-check it if it affects the fix."

    structured_evidence_line = ""
    if has_structured_evidence_marker(issue.body):
        marker = parse_structured_evidence_marker(issue.body)
        language = marker.language if marker is not None else "zh-CN"
        structured_evidence_line = (
            "This Issue requires a structured evidence manifest. "
            f"{build_structured_evidence_prompt_suffix(language).format(evidence_dir='.iar/evidence')} "
            "Fix the manifest and the referenced evidence files before requesting a commit."
        )

    verification_section = ""
    if verification_results:
        failed_results = failed_verification_results(verification_results)
        if failed_results:
            formatted_failures = "\n\n".join(
                format_result_for_recovery(result) for result in failed_results
            )
            verification_section = (
                "The runner detected the following verification failures "
                "(commands are run in order; the first failure stops the chain):\n\n"
                f"{formatted_failures}"
            )

    memory_section = ""
    if (
        memory_config is not None
        and memory_config.enabled
        and long_term_store is not None
        and skill_store is not None
    ):
        relevant = match_skills_and_memory(
            issue,
            failure_type or "verification_failed",
            worktree_path,
            memory_config,
            long_term_store=long_term_store,
            skill_store=skill_store,
        )
        memory_section = _build_memory_block(
            issue,
            worktree_path,
            memory_config=memory_config,
            relevant_memory=relevant,
            long_term_store=long_term_store,
            skill_store=skill_store,
        )
        if memory_section:
            memory_section = (
                "Relevant memory from past runs (apply when useful, "
                "do not parrot verbatim):\n" + memory_section
            )

    return "\n".join(
        [
            f"Repair GitHub Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            f"Worktree: {worktree_path}",
            f"Recovery attempt: {recovery_attempt}/{max_recovery_attempts}",
            prd_line,
            "",
            "The runner could not finish the previous attempt:",
            failure_summary,
            "",
            verification_section,
            structured_evidence_line,
            memory_section,
            "Recovery rules:",
            "- Inspect the current worktree and fix the failure.",
            "- Only modify files inside the current worktree.",
            "- Before requesting a commit, re-check project conventions "
            "(naming, dependency direction, file encoding, max line length, etc.).",
            "- Do not switch branches, merge main, push, or create PRs.",
            "- Do not run `git commit`, `git reset`, `git checkout`, or any "
            "other command that mutates the git index; the runner handles staging and commits.",
            "- If the failure involves a stale verification/test flag, re-run the relevant "
            "verification commands after any file changes and before requesting a commit.",
            "- After fixing the issue, write or update "
            "`.agent-runner/commit-request.json` as JSON with `commit_message`.",
            "- Finish with a concise summary, tests run, and remaining risk.",
        ]
    )


def build_progress_continuation_prompt(
    issue: IssueSummary,
    worktree_path: Path,
    *,
    failure_summary: str = "",
    verification_results: list[CommandResult] | None = None,
) -> str:
    """构造"在已提交进度上继续"的 prompt，用于跨 claim 续作。

    当上一次 claim 把部分进度提交成 checkpoint（或正常提交）后，下一次 claim
    不应从零开始。本 prompt 告知 agent 工作树已有既有提交，要先检视现状再补齐
    剩余工作，避免重复劳动或回退已完成内容。同时附带上一次失败的 verification
    上下文，让续作 agent 直接针对未通过的检查继续修复。
    """
    prd_path = extract_prd_path(issue.body)
    if prd_path:
        prd_closeout = _build_prd_closeout_instruction(prd_path)
        prd_context_block = _build_prd_context_block(issue, worktree_path)
        prd_line = (
            "The canonical PRD is inlined below. Use it and its Acceptance "
            f"Checklist to see which items are already done. {prd_closeout}\n\n"
            f"{prd_context_block}"
        )
    else:
        prd_line = "If the Issue references a PRD, read it to see the remaining work."

    failure_section = ""
    if failure_summary:
        failure_section = f"The previous attempt failed with:\n{failure_summary}\n"

    verification_section = ""
    if verification_results:
        failed_results = failed_verification_results(verification_results)
        if failed_results:
            formatted_failures = "\n\n".join(
                format_result_for_recovery(result) for result in failed_results
            )
            verification_section = (
                "The previous attempt failed the following verification checks "
                "(commands are run in order; the first failure stops the chain):\n\n"
                f"{formatted_failures}\n"
            )

    return "\n".join(
        [
            f"Continue GitHub Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            f"Worktree: {worktree_path}",
            "",
            "This worktree already contains committed progress from earlier runner "
            "attempts. Do not restart from scratch and do not revert existing "
            "commits. Inspect the current state first (`git log`, existing files, "
            "and the PRD Acceptance Checklist), then implement only what remains.",
            prd_line,
            "",
            failure_section,
            verification_section,
            "Execution rules:",
            "- Only modify files inside the current worktree.",
            "- Before requesting a commit, re-check project conventions "
            "(naming, dependency direction, file encoding, max line length, etc.).",
            "- Do not merge main, switch branches, push, or create PRs; "
            "the runner handles publishing.",
            "- Do not run `git add` or `git commit`; after finishing your changes, "
            "write `.agent-runner/commit-request.json` as JSON with `commit_message`.",
            "- Finish with a concise summary, tests run, and remaining risk.",
        ]
    )
