"""rv-3-positive: distillation writes or merges a skill draft on success.

Reproduces the positive path of realistic-validation item rv-3: invoke
``_try_distill_skill_after_success`` twice with similar Issue inputs
(``save_skill_draft`` should dedupe to a single file with
``usage_count=2`` and ``success_count=2``), and assert the produced draft
file contains the full front matter.
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

    worktree = Path(tempfile.mkdtemp(prefix="rv3-pos-"))
    try:
        config = AppConfig(
            memory=MemoryConfig(
                enabled=True,
                base_dir=str(worktree / "memory"),
                skill_drafts_dir=str(worktree / "drafts"),
                promoted_skills_dirs=(str(worktree / "promoted"),),
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

        def _commit_result() -> AgentCommitResult:
            return AgentCommitResult(
                verification_results=[
                    CommandResult(
                        command=("pre-commit", "run", "--all-files"),
                        return_code=0,
                        stdout="",
                        stderr="",
                    )
                ],
                attempt_results=[
                    AttemptResult(
                        attempt_number=1,
                        failure_type=FailureType.SUCCESS,
                        recovered=False,
                        detail="removed unused import",
                        agent="claude",
                    )
                ],
            )

        _try_distill_skill_after_success(
            issue=_issue(1),
            worktree_path=worktree,
            config=config,
            commit_result=_commit_result(),
        )
        _try_distill_skill_after_success(
            issue=_issue(2),
            worktree_path=worktree,
            config=config,
            commit_result=_commit_result(),
        )

        drafts_dir = worktree / "drafts"
        skill_files = sorted(drafts_dir.glob("*.md")) if drafts_dir.exists() else []
        payload: dict[str, object] = {"draft_count": len(skill_files)}
        if skill_files:
            content = skill_files[0].read_text(encoding="utf-8")
            payload.update(
                {
                    "has 'name:':": "name:" in content,
                    "has 'description:':": "description:" in content,
                    "has 'tags:':": "tags:" in content,
                    "has 'version:':": "version:" in content,
                    "has 'draft: true':": "draft: true" in content,
                    "has 'updated:':": "updated:" in content,
                    "has 'usage_count:':": "usage_count:" in content,
                    "has 'success_count:':": "success_count:" in content,
                }
            )
            for marker in ("usage_count:", "success_count:"):
                line = next(
                    (
                        ln
                        for ln in content.splitlines()
                        if ln.strip().startswith(marker)
                    ),
                    "",
                )
                value = line.split(":", 1)[1].strip() if line else "0"
                payload[marker.rstrip(":")] = int(value)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        success = (
            payload.get("draft_count") == 1
            and payload.get("usage_count") == 2
            and payload.get("success_count") == 2
        )
        return 0 if success else 1
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
