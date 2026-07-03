"""rv-7-negative: auto_promote=False leaves the draft in drafts/.

Reproduces the negative control of realistic-validation item rv-7: with
``auto_promote=False`` and threshold=3, three consecutive successful
similar Issues must dedupe to a single draft and leave it inside
``.iar/skills/drafts/`` — it must NOT be promoted to the promoted dir.
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

    worktree = Path(tempfile.mkdtemp(prefix="rv7-neg-"))
    try:
        drafts_dir = worktree / "drafts"
        promoted_dir = worktree / "promoted"
        drafts_dir.mkdir(parents=True)
        promoted_dir.mkdir(parents=True)
        config = AppConfig(
            memory=MemoryConfig(
                enabled=True,
                base_dir=str(worktree / "memory"),
                skill_drafts_dir=str(drafts_dir),
                promoted_skills_dirs=(str(promoted_dir),),
                auto_promote=False,
                auto_promote_threshold=3,
            )
        )

        def _issue(num: int) -> IssueSummary:
            return IssueSummary(
                number=num,
                title="Fix lint F401 unused imports",
                url=f"https://example/{num}",
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
        for i in range(1, 4):
            _try_distill_skill_after_success(
                issue=_issue(i),
                worktree_path=worktree,
                config=config,
                commit_result=commit_result,
            )

        drafts = sorted(p.name for p in drafts_dir.glob("*.md"))
        promoted = sorted(p.name for p in promoted_dir.glob("*.md"))
        payload = {
            "drafts": drafts,
            "promoted": promoted,
            "expected": "draft stays in drafts/, no promotion",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        ok = len(drafts) == 1 and not promoted
        return 0 if ok else 1
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
