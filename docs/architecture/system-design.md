# 系统架构

本仓库遵循 **DDD 边界意识 + Clean Architecture 依赖方向 + 模块化单体** 的设计理念，目标是把模板演进为一个可长期维护的四层 AI Agent 骨架，而不是把所有能力堆在一个扁平工具目录里。

## 架构原则

- **依赖向内**：外层可以依赖内层，内层不能反向依赖外层。
- **按职责分层**：按变化率和业务边界分层，而不是按技术栈堆目录。
- **模块化单体**：保持一个仓库、一个部署单元，但内部按层隔离。
- **可替换实现**：模型、存储、HTTP、日志、配置等都放在基础设施层，核心层只依赖接口。

## 目标分层

### `src/backend/api/` 请求接入层

- 接收 HTTP、WebSocket、CLI 等请求。
- 只负责参数校验、DTO 转换、调用用例。
- 不写业务规则。

### `src/backend/core/` 核心编排层

- 放置用例、Orchestrator、Planner、Memory 和领域契约。
- 负责业务规则和任务编排。
- 不直接依赖具体 SDK、数据库或 HTTP 客户端。

### `src/backend/engines/` 平台能力层

- 放置 skills、RAG、registry、可插拔能力。
- 实现 `src/backend/core/` 定义的端口。
- 不承担基础设施细节。

### `src/backend/infrastructure/` 基础设施层

- 放置模型客户端、数据库、HTTP 客户端、配置和日志实现。
- 对接外部 API、物理数据库和文件系统。
- 只提供可被内层调用的具体实现。

## 当前目录

- `src/backend/main.py`：后端真实 composition root，只做依赖组装。
- `main.py`：根目录兼容启动包装器，转发到 `backend.main`。
- `src/backend/infrastructure/config/`：承接配置管理。
- `src/backend/infrastructure/logging/`：承接日志实现。
- `src/backend/infrastructure/persistence/`：承接数据库与持久化实现。
- `src/backend/infrastructure/helpers.py`：承接通用无状态辅助函数。
- `src/backend/infrastructure/models/`：承接模型配置解析与 LLM 客户端装配。
- `src/backend/engines/`：承接项目特定的平台能力（如 RAG、爬虫、OCR 等）。
- `tests/`：用于验证边界、用例和适配器行为。

## 模块关系图

```mermaid
flowchart TD
    Browser["浏览器 / 用户"] --> FE

    subgraph FE["frontend/ 前端层"]
        direction TB
        FE_PAGES["pages/\nLoginPage · DashboardPage"]
        FE_LAYOUT["components/\nAppSidebar · SiteHeader · shadcn/ui"]
        FE_AUTH["auth/\nSessionProvider · RequireSession"]
        FE_SHARED["shared/\napi/client · auth/sessionStore"]
        FE_PAGES --> FE_LAYOUT
        FE_PAGES --> FE_AUTH
        FE_AUTH --> FE_SHARED
        FE_LAYOUT --> FE_SHARED
    end

    FE_SHARED -->|"HTTP /api/*\n(Vite proxy / nginx 反代)"| Apps

    subgraph Backend["Python 后端"]
        direction TB
        Apps["src/backend/api/ 接入层"]
        Core["src/backend/core/ 核心编排层"]
        Platform["src/backend/engines/ 平台能力层"]
        Infra["src/backend/infrastructure/ 基础设施层"]

        Apps --> Core
        Core --> Platform
        Core --> Infra
        Platform --> Infra

        subgraph CoreDetails["src/backend/core/"]
            UC["use_cases"]
            ORCH["agent/orchestrator"]
            PLAN["agent/planner"]
            MEM["agent/memory"]
            IFACE["shared/interfaces"]
        end

        subgraph PlatformDetails["src/backend/engines/"]
            REG["skills/registry"]
            SKILL["skills/concrete skills"]
            RAG["rag/retriever + chunker"]
        end

        subgraph InfraDetails["src/backend/infrastructure/"]
            CFG["config/settings"]
            LOG["logging/logger"]
            LLM["models/clients"]
            DB["persistence/repos"]
            HTTP["http_clients"]
        end

        Core --> CoreDetails
        Platform --> PlatformDetails
        Infra --> InfraDetails
    end

    Timer["定时器 / 外部系统"] --> Apps
```

## 前端分层说明

| 层 | 路径 | 职责 |
|---|---|---|
| 页面层 | `src/pages/` | 路由级页面组件（登录、Dashboard） |
| 布局层 | `src/components/` | Sidebar、Header、shadcn/ui 基础组件 |
| 认证层 | `src/auth/` | SessionProvider 上下文、RequireSession 路由守卫 |
| 共享层 | `shared/` | API 客户端封装、会话缓存，与后端唯一通信入口 |

前端与后端之间**仅通过 `/api/*` HTTP 接口通信**，开发时由 Vite 代理转发，生产时由 nginx 反代。两侧无任何代码直接依赖。

## 为什么前端不属于四层

本仓库的四层架构用于约束 Python 后端内部的依赖方向：

```text
src/backend/api/ → src/backend/core/ → src/backend/engines/ → src/backend/infrastructure/
```

该规则同样适用于 `src/` 下所有遵循相同四层结构的其他模块。

这里的 `src/backend/api/` 指后端请求接入层，不等于浏览器前端。

- `frontend/` 是系统边界外的 Web 客户端。
- 它负责路由、页面渲染、会话状态和接口调用。
- 它通过 HTTP 或 WebSocket 调用 `src/backend/api/` 暴露的后端入口。
- 它不参与后端内部的依赖传递，因此不应被硬塞进四层中的任意一层。

如果需要讨论 `frontend/` 自身的模块拆分，应使用单独的前端内部架构文档，而不是混入后端四层依赖规则。详见 [`frontend-architecture.md`](frontend-architecture.md)。

## 依赖规则

1. `src/backend/api/` 只能调用 `src/backend/core/` 暴露的用例和 DTO。
2. `src/backend/core/` 只能依赖抽象接口和纯业务模型。
3. `src/backend/engines/` 只能实现 `src/backend/core/` 定义的契约。
4. `src/backend/infrastructure/` 负责具体集成，不包含业务编排。
5. 配置、日志、数据库和通用辅助函数位于 `src/backend/infrastructure/` 下的正式模块。
