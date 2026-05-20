# Cursor Project Adapter

本文件是 Cursor 的项目入口适配层。

统一规范源在：

- `docs/ai-standards/index.md`
- `docs/ai-standards/architecture.md`
- `docs/ai-standards/naming.md`
- `docs/ai-standards/comments-docstrings.md`
- `docs/ai-standards/documentation.md`
- `docs/ai-standards/testing.md`
- `docs/ai-standards/tooling.md`

跨工具入口摘要见：

- `AGENTS.md`

## Critical Summary

- 新的后端功能先读 `docs/architecture/system-design.md`
- Python 工作流优先 `uv` 与 `just`
- 公共 Python API 使用 Google Style Docstrings
- Python 文件读写显式使用 UTF-8
- `tests/playwright-e2e/` 使用 `npm`，不套用 Python SSA 规则

共享规范优先维护在 `docs/ai-standards/`，不要把长篇规则复制到本文件。
