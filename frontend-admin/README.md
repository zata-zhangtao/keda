# 管理平台前端

本目录是 `zata-codes-template` 的管理平台前端（Admin Dashboard），基于
[satnaing/shadcn-admin](https://github.com/satnaing/shadcn-admin) 二次开发。

## 技术栈

- **构建工具**：Vite
- **框架**：React 19
- **路由**：TanStack Router（文件即路由）
- **样式**：Tailwind CSS v4
- **组件库**：shadcn/ui
- **状态管理**：Zustand
- **HTTP 客户端**：axios
- **表单**：React Hook Form + Zod

## 本地开发

```bash
# 安装依赖
pnpm install

# 启动开发服务器（默认端口 5173）
pnpm dev
```

开发服务器启动后访问 `http://localhost:5173`。

## 构建生产产物

```bash
pnpm build
```

产物输出到 `dist/` 目录，由根目录 `frontend-admin/Dockerfile` 中的 Nginx 镜像托管。

## 项目结构

```text
frontend-admin/
├── src/
│   ├── api/          # HTTP 客户端与认证 API
│   ├── auth/         # 会话 Provider 与路由守卫
│   ├── components/   # 共享组件与 shadcn/ui
│   ├── features/     # 按业务领域组织的页面模块
│   ├── hooks/        # React hooks
│   ├── lib/          # 工具函数与配置
│   ├── pages/        # 路由级页面
│   └── app.tsx       # 应用根组件
├── index.html
├── vite.config.ts
└── Dockerfile
```

## 与后端的集成

- 开发时，Vite 将 `/api/*` 代理到 `http://localhost:8000`。
- 生产时，Nginx 将 `/api/*` 代理到后端服务（如 `zata-codes-template-backend:8000`）。
- 认证使用后端签发的 HTTP-only `session_id` cookie。

## 容器化

```bash
docker build -t zata-codes-template-admin -f Dockerfile .
```

镜像基于 Nginx 托管静态产物，端口 `80`。
