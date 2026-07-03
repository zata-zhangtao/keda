"""rv-5-positive: memory disabled short-circuits all memory writes.

Reproduces the positive path of realistic-validation item rv-5: with
``memory.enabled=False`` both ``_persist_short_term_memory`` and
``_try_distill_skill_after_success`` must not create any files under
``.iar/memory/`` or ``.iar/skills/drafts/``.
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
        AttemptResult,
        CommandResult,
        FailureType,
        IssueSummary,
        MemoryConfig,
    )
    from backend.core.use_cases.agent_runner_publication import (
        _try_distill_skill_after_success,
    )
    from backend.core.use_cases.run_agent_once import _persist_short_term_memory

    worktree = Path(tempfile.mkdtemp(prefix="rv5-pos-"))
    try:
        config = AppConfig(
            memory=MemoryConfig(
                enabled=False,
                base_dir=str(worktree / "memory"),
                skill_drafts_dir=str(worktree / "drafts"),
                promoted_skills_dirs=(str(worktree / "promoted"),),
            )
        )
        issue = IssueSummary(
            number=1,
            title="Fix lint F401 unused imports",
            url="https://example/1",
            body="b",
            labels=("area/lint",),
        )
        attempt = AttemptResult(
            attempt_number=1,
            failure_type=FailureType.SUCCESS,
            recovered=False,
            detail="removed unused import",
            agent="claude",
        )

        _persist_short_term_memory(
            config=config,
            issue=issue,
            worktree_path=worktree,
            attempt=attempt,
            repo_id="keda-main",
        )
        commit_result = AgentCommitResult(
            verification_results=[
                CommandResult(
                    command=("pre-commit", "run"),
                    return_code=0,
                    stdout="",
                    stderr="",
                )
            ],
            attempt_results=[attempt],
        )
        _try_distill_skill_after_success(
            issue=issue,
            worktree_path=worktree,
            config=config,
            commit_result=commit_result,
        )

        short_term = (worktree / ".iar" / "memory" / "short_term").exists()
        drafts = (
            (worktree / "drafts").glob("*.md") if (worktree / "drafts").exists() else []
        )
        payload = {
            "short_term_exists": short_term,
            "drafts_exists": bool(list(drafts)),
            "expected": "no memory writes, no skill drafts",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        success = not short_term and not list(drafts)
        return 0 if success else 1
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
