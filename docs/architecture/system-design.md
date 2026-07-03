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
- `src/backend/infrastructure/config/`：承接配置管理；还承接 OpenAI 协议 provider 注册表与 `create_chat_model` 工厂（按 `provider/model_id` 分发）。
- `src/backend/infrastructure/logging/`：承接日志实现。
- `src/backend/infrastructure/persistence/`：承接数据库与持久化实现。
- `src/backend/infrastructure/helpers.py`：承接通用无状态辅助函数。
- `src/backend/engines/`：承接项目特定的平台能力（如 RAG、爬虫、OCR 等）。
- `tests/`：用于验证边界、用例和适配器行为。

## 模块关系图

```mermaid
flowchart TD
    Browser["浏览器 / 用户"] --> FE

    subgraph FE["前端层"]
        direction TB
        subgraph FE_ADMIN["frontend-admin/ 管理平台"]
            FE_ADMIN_PAGES["src/pages/\nLoginPage · DashboardPage"]
            FE_ADMIN_LAYOUT["src/components/\nAppSidebar · SiteHeader · shadcn/ui"]
            FE_ADMIN_AUTH["src/auth/\nSessionProvider · RequireSession"]
            FE_ADMIN_API["src/api/\nclient · auth"]
            FE_ADMIN_PAGES --> FE_ADMIN_LAYOUT
            FE_ADMIN_PAGES --> FE_ADMIN_AUTH
            FE_ADMIN_AUTH --> FE_ADMIN_API
            FE_ADMIN_LAYOUT --> FE_ADMIN_API
        end

        subgraph FE_PUBLIC["frontend-public/ 前台官网"]
            FE_PUBLIC_MARKETING["app/(marketing)/\nHome · Pricing · Features"]
            FE_PUBLIC_APP["app/(app)/\nDashboard · Settings"]
            FE_PUBLIC_AUTH["lib/auth.tsx"]
            FE_PUBLIC_API["lib/api.ts"]
            FE_PUBLIC_MARKETING --> FE_PUBLIC_AUTH
            FE_PUBLIC_APP --> FE_PUBLIC_AUTH
            FE_PUBLIC_APP --> FE_PUBLIC_API
            FE_PUBLIC_AUTH --> FE_PUBLIC_API
        end
    end

    FE_ADMIN_API -->|"HTTP /api/*\n(nginx 反代)"| Apps
    FE_PUBLIC_API -->|"HTTP /api/*\n(直接调用)"| Apps

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

本仓库包含两个独立前端项目，分别服务不同场景：

### 管理平台 `frontend-admin/`

| 层 | 路径 | 职责 |
|---|---|---|
| 页面层 | `src/pages/` | 路由级页面组件（登录、Dashboard） |
| 布局层 | `src/components/` | Sidebar、Header、shadcn/ui 基础组件 |
| 认证层 | `src/auth/` | SessionProvider 上下文、RequireSession 路由守卫 |
| API 层 | `src/api/` | API 客户端封装、会话缓存，与后端唯一通信入口 |

### 前台官网 `frontend-public/`

| 层 | 路径 | 职责 |
|---|---|---|
| 营销页面 | `app/(marketing)/` | 首页、功能、定价、FAQ 等落地页 |
| 应用页面 | `app/(app)/` | 登录后的 Dashboard、Settings、Tasks、Projects |
| 认证层 | `lib/auth.tsx` | 会话状态、受保护布局 |
| API 层 | `lib/api.ts` | axios/fetch 封装、环境基址、错误处理 |

前端与后端之间**仅通过 `/api/*` HTTP 接口通信**：

- `frontend-admin/` 开发时由 Vite 代理转发，生产时由 Nginx `/api/*` 反代到后端。
- `frontend-public/` 开发时直接请求 `http://localhost:8000`，生产时通过 `API_BASE_URL` 指向后端。

两侧无任何代码直接依赖。

## 为什么前端不属于四层

本仓库的四层架构用于约束 Python 后端内部的依赖方向：

```text
src/backend/api/ → src/backend/core/ → src/backend/engines/ → src/backend/infrastructure/
```

该规则同样适用于 `src/` 下所有遵循相同四层结构的其他模块。

这里的 `src/backend/api/` 指后端请求接入层，不等于浏览器前端。

- `frontend-admin/` 与 `frontend-public/` 都是系统边界外的 Web 客户端。
- 它们负责路由、页面渲染、会话状态和接口调用。
- 它们通过 HTTP 或 WebSocket 调用 `src/backend/api/` 暴露的后端入口。
- 它们不参与后端内部的依赖传递，因此不应被硬塞进四层中的任意一层。

如果需要讨论前端自身的模块拆分，应使用单独的前端内部架构文档，而不是混入后端四层依赖规则。详见 [`frontend-architecture.md`](frontend-architecture.md)。

## 依赖规则

1. `src/backend/api/` 只能调用 `src/backend/core/` 暴露的用例和 DTO。
2. `src/backend/core/` 只能依赖抽象接口和纯业务模型。
3. `src/backend/engines/` 只能实现 `src/backend/core/` 定义的契约。
4. `src/backend/infrastructure/` 负责具体集成，不包含业务编排。
5. 配置、日志、数据库和通用辅助函数位于 `src/backend/infrastructure/` 下的正式模块。

## 认证与会话域

系统采用**两套物理隔离的认证域**，互不穿透：

| 维度 | public 域（C 端用户） | admin 域（内部管理员） |
|---|---|---|
| 用户表 | `public_user`（开放自助注册，UUID 主键） | `admin_user`（仅种子创建，不开放注册） |
| 登录端点 | `/auth/*`（login/register/logout/me） | `/admin/auth/*`（login/logout/me，无 register） |
| 会话 Cookie | `session_id` | `admin_session_id` |
| 会话存储 | Redis，key 前缀 `public:session:` | Redis，key 前缀 `admin:session:` |
| 鉴权依赖 | `get_current_public_user` | `get_current_admin_user` |
| 主要 API | 业务 `agents/workflows/sessions/tools`（`owner_id = public_user.id`） | `/admin/users` 管理 public 用户 |
| 前端 | `frontend-public` | `frontend-admin` |

### 隔离保证

- 两域各自独立的用户表、Cookie 与 Redis 命名空间；一个域的会话 token 在另一域命名空间中查不到，因此 public 会话无法通过 admin 守卫（反之亦然），均返回 401。
- admin 专属路由集中在 `src/backend/api/admin/`，统一经 `get_current_admin_user` 守卫。
- 业务资源归属 public 用户；admin 域仅用于管理，不拥有业务资源。
- 被禁用（`is_active=false`）的用户既有会话在下次请求解析时立即失效。

### 共享编排（避免重复）

登录 / 登出 / 会话解析 / 注册的编排集中在单一的 `core/auth/AuthService`；两域通过注入不同的用户仓库、会话存储（不同前缀）与 `allow_registration` 标志复用同一实现，从而在保持物理隔离的同时不复制近似认证代码。`UserAccount` 领域模型位于 `core/shared/models/`，作为跨层契约被基础设施层仓库实现引用。

### 关键实现位置

- 领域编排：`core/auth/`（`service.py` / `directory.py` / `models.py`）、`core/shared/models/user_account.py`
- 抽象端口：`core/shared/interfaces/`（`user_account_repository.py` / `password_hasher.py` / `session_store.py`）
- 基础设施实现：`infrastructure/auth/`（`redis_session_store.py` / `bcrypt_password_hasher.py` / `redis_client.py`）、`infrastructure/persistence/repos/user_account_repo.py`、`infrastructure/persistence/models/{public_user,admin_user}.py`
- 接入层：`api/auth_router.py`、`api/admin/`、`api/dependencies.py`
- 初始管理员：`main.py` 启动时按 `AUTH_ADMIN_BOOTSTRAP_*` 幂等种子创建
- 运行依赖：Redis（`REDIS_URL`）；密码以 bcrypt 哈希持久化
