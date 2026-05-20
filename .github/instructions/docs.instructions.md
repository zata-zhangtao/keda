---
applyTo: "docs/**/*.md,mkdocs.yml,AGENTS.md,CLAUDE.md,.cursor/commands/cursor.md,.github/copilot-instructions.md"
---

Use `docs/ai-standards/` as the canonical AI standards source. Keep adapter files short and avoid duplicating long guidance bodies.

Treat `docs/` and `mkdocs.yml` as product code. When adding a lasting document page, update MkDocs navigation.

Keep Markdown files UTF-8 encoded. For Python API documentation, prefer `mkdocstrings` references instead of copying code comments into Markdown.

When modifying AI guidance, update the standards hub first and then synchronize the relevant adapter entry files.
