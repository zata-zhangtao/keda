"""rv-1-positive: short-term memory is written under repo_id + issue_number.

Reproduces the positive path of realistic-validation item rv-1: call
``_persist_short_term_memory`` against a real ``AppConfig`` whose
``memory.enabled`` is ``True`` and assert that
``.iar/memory/short_term/<repo_id>/<issue_number>/context.json`` is created
with a non-empty ``attempts`` list.
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

    worktree = Path(tempfile.mkdtemp(prefix="rv1-pos-"))
    try:
        config = AppConfig(
            memory=MemoryConfig(enabled=True),
        )
        issue = IssueSummary(
            number=124,
            title="rv-1 short-term memory persistence",
            url="https://example.com/issues/124",
            body="RV-1 positive control: verify recovery loop writes short-term memory.",
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
            "repo_id": "keda-main",
            "issue_number": 124,
        }
        if target.exists():
            data = json.loads(target.read_text(encoding="utf-8"))
            attempts = data.get("attempts", [])
            payload["attempts_count"] = len(attempts)
            payload["attempts"] = [
                {
                    "failure_type": a.get("failure_type"),
                    "detail": a.get("detail"),
                }
                for a in attempts
            ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if target.exists() and payload["attempts_count"] >= 1 else 1
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
