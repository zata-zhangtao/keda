"""rv-4-positive: build_prompt references a previously promoted skill.

Reproduces the positive path of realistic-validation item rv-4: place a
promoted skill under the worktree's promoted-skills dir, call
``build_prompt`` with a matching Issue, and assert the prompt contains
the skill catalog header, the skill name, and the skill path.
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

    worktree = Path(tempfile.mkdtemp(prefix="rv4-pos-"))
    try:
        skills_dir = worktree / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "ruff-f401.md").write_text(
            "---\n"
            "name: ruff-f401\n"
            "description: Remove unused imports flagged by ruff.\n"
            "tags: [ruff, lint]\n"
            "version: 1.0.0\n"
            "draft: false\n"
            "updated: 2026-01-01T00:00:00Z\n"
            "usage_count: 3\n"
            "success_count: 3\n"
            "---\n\nbody content\n",
            encoding="utf-8",
        )
        config = AppConfig(
            memory=MemoryConfig(
                enabled=True,
                base_dir=str(worktree / "memory"),
                skill_drafts_dir=str(worktree / "drafts"),
                promoted_skills_dirs=(str(skills_dir),),
                top_k_skills=2,
                top_k_facts=2,
            )
        )
        issue = IssueSummary(
            number=2,
            title="Fix ruff lint failure on unused import",
            url="https://example/2",
            body="Ruff flags F401.",
            labels=("lint", "ruff"),
        )
        prompt = build_prompt(
            issue,
            worktree,
            PromptConfig(),
            memory_config=config.memory,
        )
        print("--- has Available skills catalog ---")
        print("Available skills" in prompt)
        print("--- has skill name ---")
        print("ruff-f401" in prompt)
        print("--- has skill path ---")
        print(str(skills_dir / "ruff-f401.md") in prompt)
        success = (
            "Available skills" in prompt
            and "ruff-f401" in prompt
            and str(skills_dir / "ruff-f401.md") in prompt
        )
        return 0 if success else 1
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
