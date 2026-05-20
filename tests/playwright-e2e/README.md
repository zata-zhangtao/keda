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

1. **`support/env.ts`** — 改 docker/dev 默认端口
2. **`scripts/stack-control.mjs`** — 改 `stackUpCommand` / `stackDownCommand`
3. **`page-objects/LoginPage.ts`** — 改登录表单选择器
4. **`tests/setup/auth.setup.ts`** — 改登录入口 URL 和登录后断言
5. **`fixtures/session.fixture.ts`** — 添加项目需要的 Page Object fixtures
6. **`support/api-client.ts`** — 改 `login()` 的 endpoint 和 payload

## 前置条件

**Docker 模式**（默认）需要在仓库根目录存在 compose 文件（`docker-compose.yml` / `compose.yaml` 等）。
若目标服务已在运行，设置 `PLAYWRIGHT_SKIP_STACK_BOOT=1` 跳过 stack 启动即可。

在父仓库中可通过 `just` 统一入口运行 e2e 测试：

```bash
just e2e-install   # 安装依赖（首次运行）
just e2e           # 运行所有测试
just e2e smoke     # 只跑 smoke
just e2e no-auth   # 只跑无 auth 测试
just e2e report    # 查看 HTML 报告
```

## 运行命令

```bash
# 安装依赖
npm install
npx playwright install chromium

# 运行所有测试 (需要本地 stack 或 docker-compose.yml)
npm test

# 只跑截图 / 审查类测试
npm run test:review    # playwright test --grep @review

# 视觉回归测试
npm run test:visual
npm run test:update    # 更新基准图

# 无 auth 的测试（指向远端环境）
PLAYWRIGHT_SKIP_STACK_BOOT=1 \
PLAYWRIGHT_BASE_URL=https://your-staging.example.com \
PLAYWRIGHT_HEALTH_URL=https://your-staging.example.com \
npm run test:no-auth

# 带 auth 的测试（指向远端环境）
PLAYWRIGHT_SKIP_STACK_BOOT=1 \
PLAYWRIGHT_BASE_URL=https://your-admin.example.com \
PLAYWRIGHT_API_BASE_URL=https://your-api.example.com \
PLAYWRIGHT_HEALTH_URL=https://your-api.example.com/healthz \
PLAYWRIGHT_IDENTIFIER=admin \
PLAYWRIGHT_PASSWORD=secret \
npm test

# 查看 HTML 报告
npm run report
```

## 两种 Project 模式

| Project | 文件命名 | 需要登录 | 使用场景 |
|---|---|---|---|
| `chromium` | `*.spec.ts` | ✓ (auth.setup.ts 先跑) | 需要 session 的业务测试 |
| `no-auth` | `*.no-auth.spec.ts` | ✗ | 公开页面、demo proxy、健康检查 |
