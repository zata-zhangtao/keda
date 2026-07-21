# Testing Standards

## Validation Is Mandatory

完成实现前，必须运行与改动范围相匹配的验证。

优先策略：

- 小改动跑最相关的 targeted tests
- 文档或规范改动至少跑一致性检查和 `mkdocs build`
- 后端行为变更跑对应的 `pytest`
- 会改变 API、CLI、前端流程、后台任务、持久化、启动或部署行为时，补充最高可行保真度的真实入口验证

## Realistic Validation

测试分层应覆盖“逻辑正确”和“真实入口可用”两件事。

常见层级：

- unit：验证纯逻辑、边界条件和错误分支
- integration：验证模块组合、存储适配、配置解析和跨层契约
- real entry：通过项目实际入口验证行为，例如 HTTP API、CLI、应用启动、worker job、migration、service composition root
- user flow：通过 Playwright 或等价端到端流程验证用户可见行为
- sandbox/live：仅在外部服务行为无法用本地替身证明时使用，且必须显式 opt-in

规则：

- 不要求所有变更都上 live 外部服务，但必须说明最高可行保真度是什么。
- mock 可以用于慢速、昂贵或不稳定依赖；关键边界本身需要尽量保持真实，例如路由、配置加载、序列化、数据库迁移或启动装配。
- 需要凭据、沙箱账号或外部服务时，验证命令必须用环境变量显式开启，并记录无凭据时仍需通过的 fallback 验证。
- PRD 或 planning 任务必须写明真实入口、mock 边界、数据/环境需求、命令或人工沙箱流程，以及该验证是否阻塞验收。

## Python Test Workflow

优先使用仓库现有命令：

- `just test`
- `just test all`
- `uv run pytest ...`

验证架构和规范时，也常用：

- `uv run python hooks/shared/check_architecture.py`
- `uv run python hooks/shared/check_guidelines_consistency.py`
- `uv run mkdocs build`

## Playwright Boundary

`tests/playwright-e2e/` 是**独立的 TypeScript/Node.js 包**。

规则：

- 包管理器使用 `npm`
- 不要对该目录强加 Python SSA 命名规则
- 先看 `tests/playwright-e2e/README.md` 和 `docs/guides/e2e.md` 的适配说明

### 模板同步边界

E2E 基础设施（runner、配置、共享 support/fixtures/page-objects/scripts、README）是上游模板维护的共享层，`just sync-template` 会自动提示同步。项目特定的测试用例放在 `tests/playwright-e2e/tests/`，默认被 `config.toml` 的 `project_skip_paths` 排除，不会被模板覆盖。运行时产物（`.auth/`、`node_modules/`、`playwright-report/`、`test-results/`、`.env.e2e.local`）永远不会出现在同步列表中。

### AI Agent 常用命令

`just e2e` 是单命令入口，会自动启停 `backend + admin 前端 + public 前端`。服务已在运行时直接复用，跑完自动 `just down` 清理。

```bash
# 首次运行先安装依赖
just e2e-install

# 最稳妥：无需登录的测试，一行跑通
just e2e no-auth

# 跑单个文件 / 目录
just e2e tests/smoke/public-home.no-auth.spec.ts
just e2e tests/smoke

# 有头模式（弹出浏览器），--headed 可放在任意位置
just e2e tests/smoke/public-home.no-auth.spec.ts --headed
just e2e --headed tests/smoke/public-home.no-auth.spec.ts

# 全量测试（需要凭据，见下文）
just e2e

# smoke 测试
just e2e smoke

# 查看 HTML 报告
just e2e-report
```

### 凭据

`no-auth` 测试不需要凭据。

涉及登录的测试（`smoke` / `chromium` / `admin` project）默认从项目根目录 `.env.local` 读取种子凭据，后端启动时会自动创建对应用户：

```bash
# public 用户
APP_BOOTSTRAP_EMAIL=user@example.com
APP_BOOTSTRAP_PASSWORD=user123

# admin 用户
AUTH_ADMIN_BOOTSTRAP_USERNAME=admin
AUTH_ADMIN_BOOTSTRAP_PASSWORD=admin
```

如需覆盖，可在 `tests/playwright-e2e/.env.e2e.local` 中设置：

```bash
PLAYWRIGHT_IDENTIFIER=<public 邮箱>
PLAYWRIGHT_PASSWORD=<密码>
PLAYWRIGHT_ADMIN_IDENTIFIER=<admin 用户名>
PLAYWRIGHT_ADMIN_PASSWORD=<密码>
```

`.env.e2e.local` 优先级高于 `.env.local`。缺少凭据时，相关 setup 会失败，此时应改跑 `just e2e no-auth` 或先配置凭据。

### 测试结果

`just e2e` 会把视频、截图、trace、junit.xml 写入按运行时间戳命名的目录：

```text
tests/playwright-e2e/test-results/2026-07-02T11-31-08/
```

HTML 报告仍固定在 `tests/playwright-e2e/playwright-report/`，可用 `just e2e-report` 打开。

`just test` 会先执行 `SKIP=check-test-flag just lint --full`；当测试最终通过时，会同时刷新 `just test` 与 full lint 的本地通过标记。若代码有效 tree 未变化，后续 `just lint --full` 可以复用该标记走快速路径，但提交门禁仍会检查 `just test` 标记。

交付前建议：

- 日常迭代先跑 `just lint`，确认 staged 变更与真实 pre-commit hook 一致。
- 涉及复用边界、架构、AI 规范入口或重复风险时补跑 `just lint --reuse`。
- 最终交付、PRD 归档或合并前跑 `just lint --repo`；若无法运行总入口，至少跑 `just lint --full`、`just lint --reuse`、`just test` 和受影响文档的 `uv run mkdocs build --strict`。

## AI 实现后的验证证据

使用 `just ai implement` 实现 PRD 时，Agent 必须生成可审查的证据包，而不是直接勾选 Acceptance Checklist。

> **证据留存策略**：`tasks/evidence/` 只提交文本报告（`*.verification-plan.md`、`*.evidence-report.md`、`*.verifier-report.md`）；原始证据产物（截图、录屏、命令输出、大日志）由 `.gitignore` 排除，仅留在本地磁盘。这与 iar agent runner 的 `.iar/evidence/` 同一原则——原始证据不进代码历史（避免二进制膨胀、密钥泄漏永久化）。verifier 与 `check_prd_evidence.sh` 在实现期仍针对本地文件运行。`tasks/evidence/` 是本地 `just ai implement` 流的约定；经 iar daemon 的 Issue→PR 流改用 worktree 内的 `.iar/evidence/`（git 排除，证据发布到 PR 评论 + orphan 分支），见 `docs/guides/agent-runner.md`。

流程：

1. **生成验证计划**：executor 在 `tasks/evidence/<prd-basename>/<prd-basename>.verification-plan.md` 中列出每条验收项对应的可执行验证命令或 e2e 用例。
2. **收集证据**：executor 执行验证命令，把命令输出、测试日志、截图、录屏按 `rv-id` 命名保存到 `tasks/evidence/<prd-basename>/`。
3. **写证据报告**：executor 在 `<prd-basename>.evidence-report.md` 中解释每条证据对应哪个验收项、证据显示了什么、为什么能证明验收项成立。
4. **独立 verifier 审查**：`just ai implement` 自动启动一个 verifier Agent，默认使用与 executor 不同的 AI 工具；verifier 只读审查证据与 PRD 验收项的匹配度，输出 `<prd-basename>.verifier-report.md`，结论为 `PASS` 或 `REJECT`。
5. **循环**：verifier 输出 `REJECT` 时，executor 必须修复问题并重新收集证据，再次进入 verifier 审查。
6. **前端强制视觉证据**：如果 PRD 涉及 `frontend-admin/` 或 `frontend-public/` 改动，证据目录必须包含至少一个 `.png`、`.jpg` 或 `.webm` 文件。
7. **最终校验**：verifier 通过后，`just ai implement` 运行 `scripts/shared/just/check_prd_evidence.sh` 再次确认前端视觉证据存在，缺少则阻止流程结束。
8. **工具不可用**：verifier 默认工具不可用时，降级到与 executor 相同工具；相同工具也不可用时，流程暂停并提示人工，不自动回退到 executor 自检。

人工终点审查时，按风险地图顺序查看证据包，重点抽查高风险 oracle 结果、前端截图/录屏和 verifier report。

## Change Recording

当任务带有 PRD 或 planning 记录时，记录实际执行的验证命令和结果。
