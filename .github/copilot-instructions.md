Canonical AI standards live in `docs/ai-standards/`. Treat that directory as the source of truth and treat this file as the GitHub Copilot adapter.

Before new backend features, read `docs/architecture/system-design.md` and follow the four-layer dependency direction: `src/backend/api/ -> src/backend/core/ -> src/backend/engines/ -> src/backend/infrastructure/`.

Use `uv` and `just` for Python workflows. Public Python APIs require Google Style Docstrings. Python text file I/O must explicitly set `encoding="utf-8"`.

Keep `docs/` and `mkdocs.yml` in sync when behavior, configuration, architecture, or standards change.

Before any code change, and again before collecting validation evidence or claiming completion, read `docs/ai-standards/testing.md`. A component preview, temporary route, or manually injected state must not be presented as real-entry or end-to-end validation.

`tests/playwright-e2e/` is a standalone TypeScript/Node package that uses `npm`; do not force Python SSA naming conventions onto that subtree.

Guard tests in `tests/guards/` assert repo conventions and hook contracts, not business logic. When one fails, fix the source code or config that triggered it-never edit the guard test to make it pass. Modifying `tests/guards/**` requires `GUARD_UPDATE_ACK=1 git commit`. See `docs/ai-standards/testing.md` (Guard Tests).

When a matching file under `.github/instructions/` applies to the current path, follow both this file and the scoped file and avoid conflicts.
