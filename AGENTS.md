# AI Agent Entry Guide

本文件是仓库的**跨工具 AI 入口适配层**。

统一规范源在：

- `docs/ai-standards/index.md`
- `docs/ai-standards/architecture.md`
- `docs/ai-standards/naming.md`
- `docs/ai-standards/comments-docstrings.md`
- `docs/ai-standards/documentation.md`
- `docs/ai-standards/testing.md`
- `docs/ai-standards/tooling.md`

详细后端架构权威文档仍在：

- `docs/architecture/system-design.md`

## Read Order

1. 先读 `docs/ai-standards/index.md`
2. 若涉及后端新功能，再读 `docs/architecture/system-design.md`
3. 根据任务类型补读对应标准页

## Critical Summary

- 后端必须遵守四层依赖方向：
  `src/backend/api/ -> src/backend/core/ -> src/backend/engines/ -> src/backend/infrastructure/`
  - `api/` 可导入 `core/` 和 `engines/`，禁止直接导入 `infrastructure/`
  - `core/` 可导入自身和 `core/shared/interfaces/`，禁止导入 `engines/` 或 `infrastructure/`
  - `engines/` 可导入 `core/` 和 `infrastructure/`，提供适配与工厂能力
  - `infrastructure/` 只能依赖外部第三方包，禁止导入 `core/` / `engines/` / `api/`
- Python 项目优先使用 `uv` 和 `just`
- 公共 Python API 使用 Google Style Docstrings
- Python 文本文件 I/O 必须显式写 `encoding="utf-8"`
- 变量命名必须具有来源、类型或状态语义，避免 `data`、`item`、`res`
- 新增或修改代码前先搜索现有实现；禁止复制粘贴后微调，参数超过 4 个时收敛到对象
- 除非用户明确要求，否则不要自动执行 `git add`、`git commit`、`git push` 等 Git 变更操作
- 单代码文件非空行不超过 1000 行；`just lint` 会对此发出警告
- PRD 对应任务全部完成后：将已完成项打勾，所有 Acceptance Checklist 条目达到完成态后，再将 PRD 从 `tasks/pending/` 归档到 `tasks/archive/`
- 变更代码时同步更新 `docs/` 与 `mkdocs.yml`
- `tests/playwright-e2e/` 是独立 TypeScript/Node 包，使用 `npm`，不强制套用 Python SSA 命名规范

## Maintenance Rule

共享规范优先写入 `docs/ai-standards/`，不要把长篇规则重新复制回本文件。
用户让你写prd的时候，如果你觉得需求不明确，需要不断询问用户
完成任务之后必须要运行 just test 保证改动正确，不引入回归
用于询问问题默认基于当前仓库回答，如果用户说的你不明白，你要去先查看仓库，查看完仓库还是不明白再询问用户
