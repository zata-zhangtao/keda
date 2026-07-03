# 前端架构

本仓库的前端由两个独立的 npm 包组成，分别服务不同场景。本文档是前端架构的**索引页**，不描述任何单个前端的内部细节——各前端的内部架构见其目录下的 README。

## 两个前端

| 前端 | 目录 | 技术栈 | 场景 |
|---|---|---|---|
| 前台官网 | `frontend-public/`（详见其目录下 README） | Next.js 16 App Router + React 19 + Tailwind v4 + shadcn/ui | 营销页（auth-free）+ 登录 + agent-runner 监控控制台 |
| 管理平台 | `frontend-admin/`（详见其目录下 README） | Vite + React 19 + TanStack Router + Zustand + shadcn/admin | admin 域登录与后台管理骨架 |

两个前端互不依赖，各自维护 lockfile，与后端仅通过 `/api/*` HTTP 接口通信。包管理器为 pnpm，仓根 `pnpm-workspace.yaml` 声明两个 workspace。

## 与后端四层的边界

```mermaid
flowchart LR
    FrontendPublic["frontend-public/ Web Client"] -->|HTTP /api/*| Apps["src/backend/api/"]
    FrontendAdmin["frontend-admin/ Web Client"] -->|HTTP /api/*| Apps
    Apps --> Core["src/backend/core/"]
    Core --> Capabilities["src/backend/engines/"]
    Core --> Infrastructure["src/backend/infrastructure/"]
    Capabilities --> Infrastructure
```

这表示：

- 前端只依赖后端暴露的接口契约，不依赖后端 Python 模块。
- 后端内部如何在 `src/backend/api/`、`src/backend/core/`、`src/backend/engines/`、`src/backend/infrastructure/` 之间拆分，对前端来说应是透明的。
- 各前端内部的路由图、状态图和组件边界约束，见对应目录的 README，不再在本文档展开。

## 历史说明

本仓库曾有一个单一的 `frontend/` 目录（Vite + Refine + react-router + npm）。它已通过 strangler 迁移被 `frontend-public/` 吸收（7 个 agent-runner 页面 + 5 个组件 + API 客户端）并在收尾阶段删除。迁移细节见 `tasks/archive/P1-FEAT-20260702-140755-frontend-template-migration.md`。
