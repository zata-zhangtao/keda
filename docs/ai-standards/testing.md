# Testing Standards

## Validation Is Mandatory

完成实现前，必须运行与改动范围相匹配的验证。

优先策略：

- 小改动跑最相关的 targeted tests
- 文档或规范改动至少跑一致性检查和 `mkdocs build`
- 后端行为变更跑对应的 `pytest`

## Python Test Workflow

优先使用仓库现有命令：

- `just test` — 基于 `pytest-testmon` 的 change-aware 测试选择，仅运行与本次变更相关的测试
- `just test all` — 强制运行 `tests/` 下全部测试，忽略 `.testmondata`
- `just test real` — 强制运行标记为 `real_api` 的测试，忽略 `.testmondata`
- `uv run pytest ...` — 直接调用 pytest，同样会默认启用 `--testmon`

第一次运行 `just test` 时会完整执行全部测试并生成 `.testmondata` 缓存；后续修改文件后再次运行将自动选择受影响的最小测试子集。若 `.testmondata` 异常或需要重建，可直接删除该文件（已加入 `.gitignore`）。

验证架构和规范时，也常用：

- `uv run python hooks/check_architecture.py`
- `uv run python hooks/check_guidelines_consistency.py`
- `uv run mkdocs build`

## Local Testing Middleware

需要本地模拟 PostgreSQL、Redis 或 S3-compatible storage 时，可使用独立测试中间件 Compose 文件：

```bash
docker compose -f docker-compose.testing.yml up -d
docker compose -f docker-compose.testing.yml down
```

该文件只用于本地开发和测试，不用于生产部署。

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
