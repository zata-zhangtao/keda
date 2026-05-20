---
applyTo: "tests/playwright-e2e/**/*.ts,tests/playwright-e2e/**/*.tsx,tests/playwright-e2e/**/*.js,tests/playwright-e2e/**/*.mjs,tests/playwright-e2e/package.json"
---

`tests/playwright-e2e/` is a standalone TypeScript/Node package. Follow `tests/playwright-e2e/README.md` for adaptation and runtime details.

Use `npm` for dependency management and test execution in this subtree, not `uv`.

Follow TypeScript and Playwright community conventions here. Do not force Python SSA naming rules or Python docstring rules onto this package.
