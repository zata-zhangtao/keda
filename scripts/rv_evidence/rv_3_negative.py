"""rv-3-negative: no draft is created when the agent had no successful attempt.

Reproduces the negative control of realistic-validation item rv-3: invoke
``_try_distill_skill_after_success`` with an empty ``attempt_results``
list (no successful attempt) and assert no draft is created.
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
        AgentCommitResult,
        AppConfig,
        IssueSummary,
        MemoryConfig,
    )
    from backend.core.use_cases.agent_runner_publication import (
        _try_distill_skill_after_success,
    )

    worktree = Path(tempfile.mkdtemp(prefix="rv3-neg-"))
    try:
        config = AppConfig(
            memory=MemoryConfig(
                enabled=True,
                base_dir=str(worktree / "memory"),
                skill_drafts_dir=str(worktree / "drafts"),
                promoted_skills_dirs=(str(worktree / "promoted"),),
                auto_promote=False,
            )
        )
        issue = IssueSummary(
            number=1,
            title="Fix lint F401 unused imports",
            url="https://example/1",
            body="b",
            labels=("area/lint",),
        )
        commit_result = AgentCommitResult(verification_results=[], attempt_results=[])

        _try_distill_skill_after_success(
            issue=issue,
            worktree_path=worktree,
            config=config,
            commit_result=commit_result,
        )

        drafts_dir = worktree / "drafts"
        drafts = list(drafts_dir.glob("*.md")) if drafts_dir.exists() else []
        payload = {
            "draft_count": len(drafts),
            "expected_fail": "no draft created when no successful attempts",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not drafts else 1
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
