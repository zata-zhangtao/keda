"""rv-1-negative: short-term memory write is short-circuited when disabled.

Reproduces the negative control of realistic-validation item rv-1: call
``_persist_short_term_memory`` with ``memory.enabled=False`` and assert that
no ``context.json`` is created under the worktree's ``.iar/memory/`` tree.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root))

    from backend.core.shared.models.agent_runner import (
        AppConfig,
        AttemptResult,
        FailureType,
        IssueSummary,
        MemoryConfig,
    )
    from backend.core.use_cases.run_agent_once import _persist_short_term_memory

    worktree = Path(tempfile.mkdtemp(prefix="rv1-neg-"))
    try:
        config = AppConfig(memory=MemoryConfig(enabled=False))
        issue = IssueSummary(
            number=124,
            title="rv-1 short-term memory disabled",
            url="https://example.com/issues/124",
            body="RV-1 negative control: memory disabled must skip the write.",
            labels=("agent/ready",),
        )
        attempt = AttemptResult(
            attempt_number=1,
            failure_type=FailureType.VERIFICATION_FAILED,
            recovered=False,
            detail="ruff failed: F401 unused import",
            agent="claude",
        )

        _persist_short_term_memory(
            config=config,
            issue=issue,
            worktree_path=worktree,
            attempt=attempt,
            repo_id="keda-main",
        )

        target = (
            worktree
            / ".iar"
            / "memory"
            / "short_term"
            / "keda-main"
            / "124"
            / "context.json"
        )
        payload = {
            "target_exists": target.exists(),
            "expected_fail": "no context.json was written when memory is disabled",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not target.exists() else 1
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
