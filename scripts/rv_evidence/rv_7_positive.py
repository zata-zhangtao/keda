"""rv-7-positive: 3 similar successes auto-promote the draft to .iar/skills/.

Reproduces the positive path of realistic-validation item rv-7: with
``auto_promote=True`` and a threshold of 3, three consecutive successful
similar Issues must (a) dedupe to a single draft with
``usage_count=3, success_count=3`` and (b) auto-move the draft to
``.iar/skills/`` (or the configured promoted dir) with ``draft: false``.
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

    worktree = Path(tempfile.mkdtemp(prefix="rv7-pos-"))
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
                auto_promote=True,
                auto_promote_threshold=3,
                auto_promote_min_success_rate=1.0,
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
        payload: dict[str, object] = {"drafts": drafts, "promoted": promoted}
        if promoted:
            promoted_file = promoted_dir / promoted[0]
            content = promoted_file.read_text(encoding="utf-8")
            usage = next(
                (
                    int(ln.split(":", 1)[1].strip())
                    for ln in content.splitlines()
                    if ln.strip().startswith("usage_count:")
                ),
                0,
            )
            success = next(
                (
                    int(ln.split(":", 1)[1].strip())
                    for ln in content.splitlines()
                    if ln.strip().startswith("success_count:")
                ),
                0,
            )
            payload[promoted[0]] = {
                "has draft: false": "draft: false" in content,
                "usage_count": usage,
                "success_count": success,
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        ok = (
            not drafts
            and len(promoted) == 1
            and payload.get(promoted[0], {}).get("usage_count") == 3
            and payload.get(promoted[0], {}).get("success_count") == 3
        )
        return 0 if ok else 1
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
