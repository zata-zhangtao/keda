# Testing Standards

## Validation Is Mandatory

完成实现前，必须运行与改动范围相匹配的验证。

优先策略：

- 小改动跑最相关的 targeted tests
- 文档或规范改动至少跑一致性检查和 `mkdocs build`
- 后端行为变更跑对应的 `pytest`

## Python Test Workflow

优先使用仓库现有命令：

- `just test`
- `just test all`
- `uv run pytest ...`

验证架构和规范时，也常用：

- `uv run python hooks/check_architecture.py`
- `uv run python hooks/check_guidelines_consistency.py`
- `uv run mkdocs build`

## Playwright Boundary

`tests/playwright-e2e/` 是**独立的 TypeScript/Node.js 包**。

规则：

- 包管理器使用 `npm`
- 不要对该目录强加 Python SSA 命名规则
- 先看 `tests/playwright-e2e/README.md` 的适配说明

常用命令：

- `just e2e-install`
- `just e2e`
- `just e2e smoke`
- `just e2e no-auth`

`just test` 会先执行 `SKIP=check-test-flag just lint --full`；当测试最终通过时，会同时刷新 `just test` 与 full lint 的本地通过标记。若代码有效 tree 未变化，后续 `just lint --full` 可以复用该标记走快速路径，但提交门禁仍会检查 `just test` 标记。

交付前建议：

- 日常迭代先跑 `just lint`，确认 staged 变更与真实 pre-commit hook 一致。
- 涉及复用边界、架构、AI 规范入口或重复风险时补跑 `just lint --reuse`。
- 最终交付、PRD 归档或合并前跑 `just lint --repo`；若无法运行总入口，至少跑 `just lint --full`、`just lint --reuse`、`just test` 和受影响文档的 `uv run mkdocs build --strict`。

## Change Recording

当任务带有 PRD 或 planning 记录时，记录实际执行的验证命令和结果。
