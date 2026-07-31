---
applyTo: "tests/**/*.py,hooks/shared/**/*.py"
---

Use `uv` and `just` for Python test and validation workflows.

Pick the smallest validation set that matches the change, but do not skip validation. Common commands include `uv run pytest ...`, `uv run python hooks/shared/check_guidelines_consistency.py`, and `uv run mkdocs build`.

Keep tests aligned with architecture boundaries. Do not introduce shortcuts that make tests depend on forbidden cross-layer imports.

Guard tests in `tests/guards/` assert repo conventions and hook contracts, not business logic. When a guard test fails, fix the source code or config that triggered it-never edit the guard test to make it pass. Only modify `tests/guards/**` when the convention itself changes (and sync the corresponding docs); commits require `GUARD_UPDATE_ACK=1 git commit`. See `docs/ai-standards/testing.md` (Guard Tests).
