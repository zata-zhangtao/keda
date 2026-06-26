"""Prompt, recovery, and PRD delivery helpers for the agent runner."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner
from backend.core.shared.models.agent_runner import (
    CommandResult,
    IssueSummary,
    PromptConfig,
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
        if (
            prd_path
            and "/" in prd_path
            and not any(char.isspace() for char in prd_path)
        ):
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
        "- Do not merge main, delete branches, push, or create PRs; "
        "the runner handles publishing.",
        "- Do not run `git add` or `git commit`; the runner exposes "
        "a restricted commit proxy.",
        "- After finishing your changes, run the verification commands above "
        "locally if possible, then request a commit by writing "
        "`.agent-runner/commit-request.json` as JSON with `commit_message`.",
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


def _build_prd_closeout_instruction(prd_relative_path: str) -> str:
    """Return the canonical "update checklist, archive if complete" footer."""
    prd_path_obj = Path(prd_relative_path)
    archive_clause = ""
    if (
        len(prd_path_obj.parts) >= 2
        and prd_path_obj.parts[0] == "tasks"
        and prd_path_obj.parts[1] == "pending"
    ):
        archive_clause = (
            " If all Acceptance Checklist items are complete, move the PRD "
            "from `tasks/pending/` to `tasks/archive/`."
        )
    return (
        "Before requesting a commit, update the PRD's Acceptance Checklist "
        f"to reflect completed work.{archive_clause}"
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
        return (
            f"Also read the canonical PRD at `{prd_relative_path}`. "
            f"{closeout_instruction}"
        )

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
    """
    template = prompt_config.phases.get(phase, _DEFAULT_EXECUTION_TEMPLATE)
    prd_line = _build_prd_context_block(issue, worktree_path)
    return template.format(
        issue_number=issue.number,
        issue_title=issue.title,
        issue_url=issue.url,
        worktree_path=worktree_path,
        issue_body=issue.body,
        prd_line=prd_line,
        validation_line=validation_line,
        verification_commands_summary=verification_commands_summary,
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


def ensure_prd_delivery_ready(
    issue: IssueSummary,
    worktree_path: Path,
    process_runner: IProcessRunner,
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
        checklist_result = parse_prd_checklist(file_content)

        if not checklist_result.section_found:
            raise PrdDeliveryError(
                f"Acceptance Checklist section missing in {prd_relative_path}"
            )

        if checklist_result.unchecked_items:
            unchecked_summary = _format_unchecked_items(
                checklist_result.unchecked_items
            )
            raise PrdDeliveryError(
                f"Acceptance Checklist has unchecked items in {prd_relative_path}:\n"
                f"{unchecked_summary}"
            )

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
            checklist_result = parse_prd_checklist(file_content)
            if not checklist_result.section_found:
                raise PrdDeliveryError(
                    f"Acceptance Checklist section missing in {archive_relative_path}"
                )
            if checklist_result.unchecked_items:
                unchecked_summary = _format_unchecked_items(
                    checklist_result.unchecked_items
                )
                raise PrdDeliveryError(
                    f"Acceptance Checklist has unchecked items in {archive_relative_path}:\n"
                    f"{unchecked_summary}"
                )
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
        if (
            len(path_parts) >= 2
            and path_parts[0] == "tasks"
            and path_parts[1] == "archive"
        ):
            archive_relative_path = prd_relative_path
        else:
            raise PrdDeliveryError(
                f"Canonical PRD must be archived before publishing: {prd_relative_path}"
            )

    archive_path = worktree_path / archive_relative_path
    if not archive_path.exists():
        raise PrdDeliveryError(
            f"Archived PRD not found in worktree: {archive_relative_path}"
        )

    # If the Issue still points at the pending path, ensure the pending file is
    # gone so the archive copy is unambiguously the canonical one.
    if prd_relative_path != archive_relative_path:
        pending_path = worktree_path / prd_relative_path
        if pending_path.exists():
            raise PrdDeliveryError(
                f"PRD is still present at pending path: {prd_relative_path}"
            )

    file_content = archive_path.read_text(encoding="utf-8")
    checklist_result = parse_prd_checklist(file_content)
    if not checklist_result.section_found:
        raise PrdDeliveryError(
            f"Acceptance Checklist section missing in {archive_relative_path}"
        )

    if checklist_result.unchecked_items:
        unchecked_summary = _format_unchecked_items(checklist_result.unchecked_items)
        raise PrdDeliveryError(
            f"Acceptance Checklist has unchecked items in {archive_relative_path}:\n"
            f"{unchecked_summary}"
        )


def format_prd_delivery_failure(message: str) -> str:
    """Build the failure section for a PRD delivery recovery prompt."""
    return "\n".join(
        [
            "PRD delivery check failed.",
            message,
            "Update the canonical PRD: ensure all Acceptance Checklist items are checked, "
            "and move the PRD from tasks/pending/ to tasks/archive/ if complete.",
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
            "- Do not run `git add` or `git commit`; the runner handles commits.",
            "- After fixing the failure, write or update "
            "`.agent-runner/commit-request.json` as JSON with `commit_message`.",
            "- Finish with a concise summary of the fix.",
        ]
    )


def build_recovery_prompt(
    issue: IssueSummary,
    worktree_path: Path,
    *,
    recovery_attempt: int,
    max_recovery_attempts: int,
    failure_summary: str,
    verification_results: list[CommandResult] | None = None,
) -> str:
    """Build a prompt that asks the agent to repair a failed attempt."""
    prd_path = extract_prd_path(issue.body)
    if prd_path:
        prd_closeout = _build_prd_closeout_instruction(prd_path)
        prd_context_block = _build_prd_context_block(issue, worktree_path)
        prd_line = (
            "Re-check the canonical PRD below; update the Acceptance Checklist "
            f"to reflect the recovery work. {prd_closeout}\n\n{prd_context_block}"
        )
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
            "Recovery rules:",
            "- Inspect the current worktree and fix the failure.",
            "- Only modify files inside the current worktree.",
            "- Before requesting a commit, re-check project conventions "
            "(naming, dependency direction, file encoding, max line length, etc.).",
            "- Do not switch branches, merge main, push, or create PRs.",
            "- Do not run `git add` or `git commit`; the runner handles commits.",
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
        failure_section = "The previous attempt failed with:\n" f"{failure_summary}\n"

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
