# Cursor AI Agent Entry Guide

本文件是 `.cursor/` 目录的 **AI 入口适配层**。

## Scope

本文件管辖 `.cursor/` 及其所有子目录下的文件。

## Upstream References

跨工具统一规范见：

- `AGENTS.md`
- `docs/ai-standards/index.md`
- `docs/ai-standards/architecture.md`
- `docs/ai-standards/naming.md`
- `docs/ai-standards/comments-docstrings.md`
- `docs/ai-standards/documentation.md`
- `docs/ai-standards/testing.md`
- `docs/ai-standards/tooling.md`

## Critical Summary

- 修改 `.cursor/` 下的配置时，确保与项目根规范保持一致
- 新增 Cursor 命令或规则前，先查看 `.cursor/commands/cursor.md` 的现有定义，避免重复或冲突
- 共享规范优先维护在 `docs/ai-standards/`，不要把长篇规则复制到本文件
