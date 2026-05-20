# 配置说明

本项目通过 `src/backend/infrastructure/config/settings.py` 中的 `AppSettings` 统一管理配置，并组合多个子配置模型，实现工程化的配置分层。

## 配置来源优先级

总优先级从高到低：

1. 环境变量（含 `.env` / `.env.local`）
2. `config.toml`
3. 代码默认值

## 关键配置模块

- **应用层配置**：应用名、日志级别、日志目录。
- **数据库配置**：后端类型、主机、端口、库名、驱动。
- **模型配置**：默认聊天模型提供商、模型名和温度。
- **基础设施配置**：MinIO、Qdrant、Embedding、Chunking、Timeout 等。

## 常用实践

- 推荐在 `.env` 中存放密钥与敏感信息。
- 推荐在 `config.toml` 中维护非敏感默认项。
- 所有业务代码统一从 `config` 实例读取，不直接散落调用 `os.getenv`。

## Worktree 相关环境变量

`just worktree`（底层实现位于 `scripts/worktree/create.sh`）支持以下环境变量来控制新 worktree 的依赖准备行为：

- `KODA_WORKTREE_BASE_BRANCH`
  - 新 worktree 默认使用的本地基底分支，默认值为 `main`。
  - 命令行参数 `--base <branch>` 会覆盖这个环境变量。
- `WORKTREE_FRONTEND_STRATEGY`
  - `install-per-worktree`：默认值。扫描 worktree 根目录和子目录中的前端项目，并在各自目录执行锁文件驱动的依赖安装。
  - `symlink-from-main`：不重新安装依赖，而是尝试把新 worktree 中的前端项目 `node_modules` 链接到源仓库对应目录。
- `WORKTREE_SKIP_FRONTEND_INSTALL`
  - 仅在 `WORKTREE_FRONTEND_STRATEGY=install-per-worktree` 时生效。
  - 设为 `true` 后，跳过前端依赖安装步骤。

对包含多个前端子项目的仓库，默认策略会覆盖类似 `demo-frontend/`、`admin-frontend/` 这类嵌套目录，而不是只处理仓库根目录。

## 模板同步配置

`just sync-template` 会读取 `config.toml` 中的 `[template_sync]` 表，用来决定默认模式下哪些项目路径不参与模板同步。

- `project_skip_paths`：默认跳过的项目路径，例如 `src/backend/`、`frontend/`、`infra/`。
- `project_include_paths`：即使命中 `project_skip_paths` 也仍然显示的路径。

运行 `just sync-template --all` 时会忽略这些项目路径过滤规则，临时查看所有模板差异。

也可以用环境变量临时覆盖：

- `SYNC_TEMPLATE_PROJECT_SKIP_PATHS`：逗号或空格分隔的跳过路径列表。
- `SYNC_TEMPLATE_PROJECT_INCLUDE_PATHS`：逗号或空格分隔的保留显示路径列表。

## 日志相关配置

日志位于 `logs/` 目录，默认文件为 `logs/app.log`，并通过 `TimedRotatingFileHandler` 按天切分。

## 数据库 URL 解析

`AppSettings.resolved_database_url` 支持：

- 直接使用 `DATABASE_URL`。
- 在未提供完整 URL 时，通过组件拼接生成连接字符串。
