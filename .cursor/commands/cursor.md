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
- 任何代码变更开始前，以及收集验证证据或声称完成前，重新核对 `docs/ai-standards/testing.md`；组件预览不得冒充真实入口验证
- Python 工作流优先 `uv` 与 `just`
- 公共 Python API 使用 Google Style Docstrings
- Python 文件读写显式使用 UTF-8
- `tests/playwright-e2e/` 使用 `npm`，不套用 Python SSA 规则
- 守卫测试（`tests/guards/`）失败时修复源代码或配置，不要改守卫测试本身让测试通过；修改 `tests/guards/**` 需 `GUARD_UPDATE_ACK=1 git commit`，详见 `docs/ai-standards/testing.md`

共享规范优先维护在 `docs/ai-standards/`，不要把长篇规则复制到本文件。
