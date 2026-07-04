"""rv-2-negative: build_prompt produces no memory block on an empty worktree.

Reproduces the negative control of realistic-validation item rv-2: call
``build_prompt`` on a fresh worktree with no ``.iar/memory/`` or
``.iar/skills/`` and assert the rendered prompt does not contain the
skill catalog header or any long-term memory fact.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root))

    from backend.core.shared.models.agent_runner import (
        AppConfig,
        IssueSummary,
        MemoryConfig,
        PromptConfig,
    )
    from backend.core.use_cases.agent_runner_feedback import build_prompt

    worktree = Path(tempfile.mkdtemp(prefix="rv2-neg-"))
    try:
        config = AppConfig(
            memory=MemoryConfig(
                enabled=True,
                base_dir=str(worktree / "memory"),
                skill_drafts_dir=str(worktree / "drafts"),
                promoted_skills_dirs=(str(worktree / "skills"),),
                top_k_skills=2,
                top_k_facts=2,
            )
        )
        issue = IssueSummary(
            number=2,
            title="Generic issue with no skill overlap",
            url="https://example/2",
            body="No matching skill expected.",
            labels=(),
        )
        prompt = build_prompt(
            issue,
            worktree,
            PromptConfig(),
            memory_config=config.memory,
        )
        print("--- without any memory files, prompt has no skills section ---")
        print("Available skills" in prompt or "available skills" in prompt)
        print("--- and no long-term memory section ---")
        print("long-term memory" in prompt)
        success = (
            "Available skills" not in prompt
            and "available skills" not in prompt
            and "long-term memory" not in prompt
        )
        return 0 if success else 1
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
