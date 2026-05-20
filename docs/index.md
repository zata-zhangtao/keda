# AI Agent 项目文档

本项目是一个面向 AI Agent 与通用 Python 工程实践的模板仓库，当前的演进方向不是“松散工具集合”，而是 **DDD 边界意识 + Clean Architecture 依赖方向 + 模块化单体** 的四层架构骨架。

## 项目定位

- 作为可复制的 Python 项目模板，保留低门槛启动体验。
- 作为 AI Agent 平台骨架，预留清晰的应用层、核心层、平台层和基础设施层。
- 作为工程实践参考，统一配置、日志、测试、Hook 与文档规范。

## 架构概览

- **`src/backend/api/`**: 请求接入层，负责 API、WebSocket、CLI 等入口。
- **`src/backend/core/`**: 核心编排层，负责用例、Agent 编排和领域契约。
- **`src/backend/engines/`**: 平台能力层，负责 skills、RAG 和可插拔能力。
- **`src/backend/infrastructure/`**: 基础设施层，负责模型、存储、HTTP、配置和日志。

## 核心特性

- 基于 `uv` + `just` 的开发工作流。
- 以 Clean Architecture 为主线的分层约束。
- 以 DDD 思想划分业务边界和能力边界。
- 基于 MkDocs Material 的工程化文档站点。
- 配置、日志、数据库、模型与可插拔能力均按四层结构组织。

## 快速安装

```bash
just dev
```

## 文档导览

- **快速开始**：介绍依赖安装、同步与本地运行。
- **使用指南**：说明配置与部署建议。
- **核心概念**：记录 Prompt 工程与 AI 资产管理思路。
- **数据库**：展示实体模型和迁移规范模板。
- **API 参考**：使用 `mkdocstrings` 自动渲染 Python API。
- **系统架构**：说明四层架构、依赖方向与迁移路径。
- **开发者**：收录测试与评测实践。
