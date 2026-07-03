#!/usr/bin/env python3
"""Pre-push gate: PRD-backed agent-runner branches must archive their PRD.

This hook inspects pushed refs whose local branch name matches the agent runner
convention (``issue-N`` or ``task/N-...``). For each such branch it fetches the
linked GitHub Issue body, extracts the canonical ``PRD path``, and verifies that
in the current branch the PRD lives under ``tasks/archive/`` and its Acceptance
Checklist is fully checked.

Issues without a PRD path are ignored. Non-agent-runner branches are ignored.
Ref deletions are ignored.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from backend.core.shared.prd_checklist import parse_prd_checklist


_AGENT_RUNNER_BRANCH_PATTERNS = (
    re.compile(r"^refs/heads/issue-(?P<number>\d+)$"),
    re.compile(r"^refs/heads/task/(?P<number>\d+)-.*$"),
)
_PRD_PATH_RE = re.compile(r"PRD path:\s*`([^`]+)`")
_ZERO_SHA = "0" * 40


def _repo_root() -> Path:
    """Return the repository root inferred from this file's location."""

    return Path(__file__).resolve().parents[1]


def _extract_issue_number(local_ref: str) -> int | None:
    """Parse an agent-runner branch name into its issue number."""

    for pattern in _AGENT_RUNNER_BRANCH_PATTERNS:
        match = pattern.match(local_ref)
        if match:
            return int(match.group("number"))
    return None


def _fetch_issue_body(issue_number: int) -> str | None:
    """Return the GitHub Issue body using ``gh issue view``.

    Returns ``None`` when the issue cannot be fetched (network, auth, or the
    issue does not exist). This is a conservative failure path: the caller
    should treat ``None`` as a hard gate failure.
    """

    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--json", "body"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"   gh issue view #{issue_number} failed:\n{exc.stderr}",
            file=sys.stderr,
        )
        return None
    except FileNotFoundError:
        print("   `gh` CLI not found; cannot verify PRD archive state.", file=sys.stderr)
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(
            f"   Could not parse `gh issue view #{issue_number}` output.",
            file=sys.stderr,
        )
        return None

    return data.get("body")


def _extract_prd_path(issue_body: str) -> str | None:
    """Extract the canonical PRD path from an Issue body."""

    match = _PRD_PATH_RE.search(issue_body)
    return match.group(1) if match else None


def _is_archived_prd_path(prd_relative_path: str) -> bool:
    """Return True when the relative PRD path is under ``tasks/archive/``."""

    parts = Path(prd_relative_path).parts
    return len(parts) >= 2 and parts[0] == "tasks" and parts[1] == "archive"


def _check_prd_state(prd_relative_path: str, repo_root: Path) -> tuple[bool, str]:
    """Verify the PRD exists, is archived, and its checklist is complete.

    Returns ``(ok, message)``. ``message`` is empty when ``ok`` is True.
    """

    if not _is_archived_prd_path(prd_relative_path):
        return False, f"PRD is not under tasks/archive/: {prd_relative_path}"

    prd_path = repo_root / prd_relative_path
    if not prd_path.exists():
        return False, f"Archived PRD not found in current branch: {prd_relative_path}"

    file_content = prd_path.read_text(encoding="utf-8")
    checklist_result = parse_prd_checklist(file_content)
    if not checklist_result.section_found:
        return False, f"Acceptance Checklist section missing in {prd_relative_path}"

    if checklist_result.unchecked_items:
        unchecked_summary = "\n".join(
            f"   - L{line}: {text}" for line, text in checklist_result.unchecked_items
        )
        return (
            False,
            f"Acceptance Checklist has unchecked items in {prd_relative_path}:\n"
            f"{unchecked_summary}",
        )

    return True, ""


def _read_push_refs() -> Iterable[tuple[str, str, str, str]]:
    """Yield (local_ref, local_sha, remote_ref, remote_sha) from stdin."""

    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        yield parts[0], parts[1], parts[2], parts[3]


def main() -> int:
    """Run the pre-push PRD archive gate.

    Returns 0 when all pushed agent-runner branches either have no PRD path or
    have their canonical PRD archived with a complete checklist. Returns 1
    otherwise.
    """

    repo_root = _repo_root()
    checked_any = False

    for local_ref, local_sha, _remote_ref, _remote_sha in _read_push_refs():
        if local_sha == _ZERO_SHA:
            continue

        issue_number = _extract_issue_number(local_ref)
        if issue_number is None:
            continue

        print(f"🔍 PRD archive gate: issue #{issue_number} ({local_ref})")
        issue_body = _fetch_issue_body(issue_number)
        if issue_body is None:
            print(
                f"❌ Cannot fetch issue #{issue_number}; refusing push.",
                file=sys.stderr,
            )
            return 1

        prd_relative_path = _extract_prd_path(issue_body)
        if prd_relative_path is None:
            print(f"   Issue #{issue_number} has no PRD path; gate skipped.")
            continue

        checked_any = True
        ok, message = _check_prd_state(prd_relative_path, repo_root)
        if not ok:
            print(
                f"❌ PRD archive gate failed for issue #{issue_number}:",
                file=sys.stderr,
            )
            print(message, file=sys.stderr)
            return 1

        print(f"   ✅ {prd_relative_path}")

    if checked_any:
        print("🎉 All pushed PRD-backed branches have archived PRDs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
