# playwright-e2e

Playwright E2E 测试包。将此目录复制到新项目，按照 TODO 注释适配即可。

## 目录结构

```
playwright-e2e/
├── fixtures/
│   └── session.fixture.ts      # 带 auth 的 test 扩展 + CleanupRegistry
├── page-objects/
│   └── LoginPage.ts            # 登录页 Page Object (需适配选择器)
├── scripts/
│   ├── global-setup.mjs        # 启动 stack + 等待就绪
│   ├── global-teardown.mjs     # 关闭 stack
│   └── stack-control.mjs       # 核心：docker/dev 模式切换 + 就绪轮询
├── support/
│   ├── api-client.ts           # 认证 HTTP 客户端 (seed / cleanup)
│   ├── cleanup.ts              # CleanupRegistry (逆序执行清理)
│   ├── demo-helpers.ts         # 演示/验收视频停顿 helper
│   ├── env.ts                  # URL / 凭证 / 路径解析 (读 env 变量)
│   └── test-data.ts            # 工具函数 (唯一名称生成等)
└── tests/
    ├── setup/
    │   └── auth.setup.ts           # 登录并持久化 session 到 .auth/
    ├── smoke/
    │   └── pages.spec.ts           # 页面冒烟测试
    └── workflows/
        ├── screenshot.spec.ts          # 截图模式 (@review / @visual)
        └── no-auth-example.no-auth.spec.ts  # 无需 auth 的测试模式
```

## 适配清单

本项目已适配：

1. **`support/env.ts`** — dev 模式默认 URL 优先读取 `just run` 写入项目根目录运行状态文件（`.env.run-state`）的端口；无 run-state 时 fallback public 前端 `3000`、admin 前端 `5173`、backend `8000`，健康检查路径 `/health`。
2. **`scripts/stack-control.mjs`** — 健康检查路径同步为 `/health`。
3. **`page-objects/LoginPage.ts`** — 保留模板，供需要表单登录的测试复用。
4. **`tests/setup/auth.setup.ts` / `tests/setup/admin-auth.setup.ts`** — 使用 `/api/auth/login` / `/api/admin/auth/login` 建立 session。
5. **`tests/smoke/public-home.no-auth.spec.ts`** — public 首页冒烟测试。
6. **`tests/smoke/admin-sign-in.admin.spec.ts`** — admin 前端表单登录冒烟测试（admin project，自行完成登录）。

## 模板同步边界

这个包本身被设计为**上游模板维护的共享层**：

- **共享（`just sync-template` 会同步）**：`fixtures/`、`page-objects/`、`scripts/`、`support/`、`playwright.config.ts`、`package.json`、`pnpm-lock.yaml`、`tsconfig.json`、`.env.e2e.example`、`.eslintrc.cjs`、`.gitignore`、本 `README.md`、`demo/`。
- **项目私有（不会被同步覆盖）**：`tests/` 目录下的 spec/setup 文件。在父仓库 `config.toml` 的 `project_skip_paths` 中已排除 `tests/playwright-e2e/tests/`。
- **运行时产物（永远不会出现在同步列表）**：`.auth/`、`node_modules/`、`playwright-report/`、`test-results/`、`.env.e2e.local`。

新增业务测试时，只在 `tests/` 下新增/修改文件，不要改动上层共享基础设施，除非要向上游模板反馈改进。

## 前置条件

本地开发推荐使用 `just e2e` 单命令运行：它会先调用 `just run all` 启动 backend + admin 前端 + public 前端，跑完测试后再 `just down` 清理。如果服务已经由外部 `just run` 启动，则会复用现有服务，不会重复启动或关闭。

`just run` 会写入 `.env.run-state`，E2E 会据此自动定位端口；无 run-state 时 fallback 到 backend `8000`、admin `5173`、public `3000`。

手动/CI 场景仍可直接使用 E2E 包自带的 Docker 模式或 dev 模式：

- **Docker 模式**：需要仓库根目录存在 compose 文件（`docker-compose.yml` / `compose.yaml` 等）。
- **Dev 模式**：目标服务已在运行时，设置 `PLAYWRIGHT_SKIP_STACK_BOOT=1` 跳过 stack 启动。

在父仓库中可通过 `just` 统一入口运行 E2E 测试：

```bash
just e2e-install     # 安装依赖并下载 Chromium（首次运行）
just e2e             # 运行所有测试（自动启停 just-run stack）
just e2e smoke       # 只跑 smoke
just e2e no-auth     # 只跑无 auth 测试
just e2e headed      # 带浏览器界面、单 worker 运行
just e2e-report      # 查看上一次运行的 HTML 报告

# 跑单个 spec / 目录 / grep tag（filter 会透传给 pnpm test）
just e2e tests/smoke/pages.spec.ts
just e2e tests/smoke
just e2e @visual
```

也可以按 PRD 收集 E2E 证据（日志、HTML 报告、失败视频）：

```bash
# 凭据默认从项目根目录 .env.local 读取；如需覆盖再创建 .env.e2e.local
just e2e-evidence tasks/pending/P2-FEAT-20260701-133736-playwright-e2e-smoke-tests.md rv-2
```

## 运行命令

### 推荐：通过父仓库 `just` 入口

```bash
# 首次安装
just e2e-install

# 运行所有测试（自动启停 just-run stack）
just e2e

# 常用过滤
just e2e smoke                          # 只跑 smoke
just e2e no-auth                        # 只跑无 auth project
just e2e headed                         # 带浏览器界面、单 worker
just e2e @visual                        # 按 grep tag 过滤

# 跑单个 spec / 目录（filter 透传给 playwright test）
just e2e tests/smoke/public-home.no-auth.spec.ts
just e2e tests/smoke

# 有头模式跑单个文件（--headed 可放在任意位置）
just e2e tests/smoke/public-home.no-auth.spec.ts --headed
just e2e --headed tests/smoke/public-home.no-auth.spec.ts

# 查看 HTML 报告
just e2e-report
```

`just e2e` 会把每次运行的 artifacts（视频、截图、trace、junit.xml）写入按时间戳命名的目录，例如 `tests/playwright-e2e/test-results/2026-07-02T11-31-08/`。HTML 报告仍固定在 `playwright-report/`，方便 `just e2e-report` 打开。

### 凭据

`no-auth` 测试不需要凭据。

登录测试默认读取项目根目录 `.env.local`：

```bash
# public 用户（后端启动时自动创建）
APP_BOOTSTRAP_EMAIL=user@example.com
APP_BOOTSTRAP_PASSWORD=user123

# admin 用户（后端启动时自动创建）
AUTH_ADMIN_BOOTSTRAP_USERNAME=admin
AUTH_ADMIN_BOOTSTRAP_PASSWORD=admin
```

如需覆盖，创建 `tests/playwright-e2e/.env.e2e.local`：

```bash
PLAYWRIGHT_IDENTIFIER=<public 邮箱>
PLAYWRIGHT_PASSWORD=<密码>
PLAYWRIGHT_ADMIN_IDENTIFIER=<admin 用户名>
PLAYWRIGHT_ADMIN_PASSWORD=<密码>
```

`.env.e2e.local` 优先级高于 `.env.local`。

### 手动运行 E2E 包（高级 / CI）

```bash
# 安装依赖
pnpm install
pnpm exec playwright install chromium

# 运行所有测试（默认走 docker stack mode；需要 docker-compose.yml）
pnpm test

# 只跑截图 / 审查类测试
pnpm test:review    # playwright test --grep @review

# 视觉回归测试
pnpm test:visual
pnpm test:update    # 更新基准图

# 无 auth 的测试（指向远端环境）
PLAYWRIGHT_SKIP_STACK_BOOT=1 \
PLAYWRIGHT_BASE_URL=https://your-staging.example.com \
PLAYWRIGHT_HEALTH_URL=https://your-staging.example.com \
pnpm test:no-auth

# 带 auth 的测试（指向远端环境）
PLAYWRIGHT_SKIP_STACK_BOOT=1 \
PLAYWRIGHT_BASE_URL=https://your-admin.example.com \
PLAYWRIGHT_API_BASE_URL=https://your-api.example.com \
PLAYWRIGHT_HEALTH_URL=https://your-api.example.com/health \
PLAYWRIGHT_IDENTIFIER=admin \
PLAYWRIGHT_PASSWORD=secret \
pnpm test

# 查看 HTML 报告
pnpm report
```

## 两种 Project 模式

| Project | 文件命名 | 需要登录 | 使用场景 |
|---|---|---|---|
| `chromium` | `*.spec.ts` | ✓ (auth.setup.ts 先跑) | 需要 session 的业务测试 |
| `no-auth` | `*.no-auth.spec.ts` | ✗ | 公开页面、demo proxy、健康检查、演示视频 |

### no-auth project 与演示/验收视频

`no-auth` project 在 `playwright.config.ts` 里默认配置了 `launchOptions: { slowMo: 200 }`，让每次点击、输入都有约 200ms 的间隔，录制出来的视频不会是一瞬间完成的操作流。

如果某些关键步骤需要额外停留（例如填完表单后让观众看清），在测试里引入共享 helper：

```ts
import { humanPause } from '../support/demo-helpers'

await page.goto('/login')
await humanPause(page, 900) // 额外停留约 0.9 秒
```

设置 `PLAYWRIGHT_DEMO_PAUSE=0` 可在常规 CI 跑测时跳过这些人工停顿。
