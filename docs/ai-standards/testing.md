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

## Frontend Visual Validation Fidelity

前端截图或录屏必须标注验证层级。不得把组件预览、临时路由或手工注入状态称为真实入口验证。

验证层级：

- `component preview`：直接渲染组件、使用 Storybook、创建临时预览路由，或手工注入 props/state
- `production composition`：使用生产中的 Dialog、Portal、Provider、布局容器和响应式约束，但允许替换后端数据
- `real user flow`：从项目真实页面入口操作，通过真实路由和交互到达目标状态
- `live integration`：真实用户流程连接真实后端及必要外部服务

规则：

1. 前端视觉改动至少验证到 `production composition`。涉及弹窗、浮层、Portal、父级布局、滚动容器或响应式约束时，单独的 `component preview` 不得作为验收证据。
2. 改动用户流程、请求结果状态或跨组件交互时，应优先通过 Playwright 或等价方式从真实页面入口到达目标状态。无法完成时，必须记录阻塞条件、实际验证层级和未覆盖风险。
3. 后端依赖可以 mock，但与目标改动相关的生产布局边界不得 mock，包括实际父容器、Dialog/Portal、Provider、主题、字体、视口和响应式断点。
4. 临时预览页只能用于开发诊断，不得单独证明真实入口可用，且必须在交付前删除。
5. 视觉证据必须记录验证层级、进入目标状态的操作路径、mock 或手工注入边界、浏览器、视口、主题，以及截图对应的具体断言。
6. 未执行真实用户流程时，结论必须写成“组件预览通过”或“生产组合验证通过”，不得写“真实验证通过”“完整流程通过”或“E2E 通过”。
7. 涉及响应式布局或可变内容长度时，必须覆盖与风险对应的代表性边界，例如窄视口、长文本、多行内容或滚动状态。
8. 生成截图本身不等于视觉验收通过。Agent 必须检查最终渲染结果，并说明证据如何证明目标问题已经解决。

## Python Test Workflow

优先使用仓库现有命令：

- `just test`
- `just test all`
- `uv run pytest ...`

验证架构和规范时，也常用：

- `uv run python hooks/shared/check_architecture.py`
- `uv run python hooks/shared/check_guidelines_consistency.py`
- `uv run mkdocs build`

## Guard Tests

`tests/guards/` 下的测试是**守卫测试**：它们断言仓库自身的约定、hook 行为、
构建脚本契约和公共 API 契约没被破坏，而不是验证业务功能逻辑。业务功能测试
放在 `tests/` 根目录或 `tests/backend/`。

### 失败时如何处理

**守卫测试失败，修复触发它的源代码、配置或脚本，不要修改守卫测试本身来让
测试通过。** 改测试让失败消失等于拆掉规则本身——失败本来就是在告诉你有人
破坏了约定。例如 `test_alembic_migration_naming.py` 失败，说明某个迁移文件
名违反了命名约定，正确做法是改那个迁移文件名，而不是放宽测试断言。

仅当**约定本身需要变更**时才修改守卫测试，且必须同步更新对应的约定文档。

### 为什么单独成目录

守卫测试和业务测试混在一起时，AI 编码代理无法区分二者：它的默认目标是"让
测试通过"，而改测试是最短路径。集中到 `tests/guards/` 并在每个文件头标注
guard test，是为了让代理第一眼识别"这是规则本身，不是被规则约束的对象"。
每个守卫测试的 module docstring 都以"守护 X 的守卫测试（guard test）"开头，
并指向本节。守卫测试清单见 `tests/guards/README.md`。

### 提交保护

修改 `tests/guards/**` 会触发 pre-commit hook `check-guard-test-modification`：
默认拒绝提交，提示确认这是有意的规则更新。确认后设置环境变量再提交：

```bash
GUARD_UPDATE_ACK=1 git commit ...
```

AI 代理默认不会设置该变量，因此会被 hook 拦下——这是"指令被忽略"时的最后一
道硬门禁。

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

`just test`（local 档）会先读 `.last_tested_commit` 与 `.last_linted_commit` 两个本地通过标记：test 标记命中时直接跳过 pytest，lint 标记命中时跳过 lint 前置。当 `just test` 真正运行测试并通过后，会同时刷新 test 与 full lint 标记，避免刚跑完 `just test` 后再次执行完整 full lint。本地调用默认使用 `pytest -q` 以压低启动/格式化开销，CI 环境（`CI` 非空）下回退到 `-v` 以保留每条用例的结果输出。CI 环境同时禁用这两条快路径，强制走完整 lint + pytest，避免跨 job/host 的 flag 文件泄漏掩盖回归。提交门禁仍会检查 `just test` 标记。

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

### 证据链完整性

可执行 oracle 除了 `real_entry` 与预期结果，还必须记录关键值来源、必须穿过的真实边界、禁止的旁路、fresh-state 独立观察，以及证据对应的最终代码树。具体要求：

- UI 显示或复制的 URL、token、ID、命令、载荷必须从 UI 原样提取并用于后续动作，禁止重构等价值或硬编码替代路径。
- 写 API 返回成功后，必须用新的 browser/request/process/DB session 经消费者入口读取，证明事务提交与持久化已经完成。
- 前端流程必须断言浏览器实际请求的 canonical path、method 与 contract；已知 legacy/重复前缀需有负断言。
- 影响入口、关键值构造、代理/路由、事务、存储、消费者或断言的相关改动会使旧证据失效，必须在最终代码树重新收集。
- 真实运行或现场报告反驳已归档的 verifier `PASS` 时，旧验收立即失效；重开或创建关联回归 PRD，修复并重新独立验收后才能再次归档。

人工终点审查时，按风险地图顺序查看证据包，重点抽查高风险 oracle 结果、前端截图/录屏和 verifier report。

## Change Recording

当任务带有 PRD 或 planning 记录时，记录实际执行的验证命令和结果。
