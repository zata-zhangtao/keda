---
applyTo: "tests/**/*.py,hooks/**/*.py"
---

Use `uv` and `just` for Python test and validation workflows.

Pick the smallest validation set that matches the change, but do not skip validation. Common commands include `uv run pytest ...`, `uv run python hooks/check_guidelines_consistency.py`, and `uv run mkdocs build`.

Keep tests aligned with architecture boundaries. Do not introduce shortcuts that make tests depend on forbidden cross-layer imports.
