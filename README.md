# keda

> 项目描述：请在此处添加项目的简要描述。

## 快速开始

```bash
just dev
```

`just dev` 会执行完整依赖同步并安装 pre-commit hooks，适合作为开发环境的一键启动命令。

## 安装说明

### 前置要求

- Python >= 3.14
- [uv](https://docs.astral.sh/uv/) - Python 包管理器
- [just](https://github.com/casey/just) - 命令运行器

### 安装步骤

1. **克隆仓库**
   ```bash
   git clone <repository-url>
   cd keda
   ```

2. **安装依赖**
   ```bash
   just dev
   ```

## 使用方法

```bash
# 运行主程序
just run

# 运行测试
just test

# 启动文档服务
just docs-serve
```

### `iar` CLI

本项目内置 `iar`（issue-agent-runner）CLI，用于将 GitHub Issues 转为本地 AI Agent 队列：

```bash
# 在目标仓库初始化本地配置
uv run iar init

# 同步当前仓库 GitHub Labels
uv run iar labels sync

# 同步指定仓库
uv run iar labels sync --repo-id keda

# 从 PRD 创建 GitHub Issue，并在 ready 前发布 PRD
uv run iar issue-from-prd tasks/pending/example.md --repo-id keda --agent codex --publish-prd --ready

# 单次执行（dry-run 预览）
uv run iar run-once --dry-run

# 单次执行（当前仓库）
uv run iar run-once

# 显式处理 config.toml registry 中所有启用仓库
uv run iar run-once --all

# Daemon 模式轮询（默认每 600 秒轮询一次，当前仓库）
uv run iar daemon
```

安装后也可直接使用 `iar`（通过 `pyproject.toml` 的 `[project.scripts]` 注册）。

多仓库 registry 示例（`config.toml`）：

```toml
[agent_runner.repositories.keda]
path = "/Users/zata/code/keda"
enabled = true

[agent_runner.repositories.backend_service]
path = "/Users/zata/code/backend-service"
enabled = true
```

`config.toml` 中的仓库列表现在是 legacy registry：仅在显式传入 `--repo-id` 或 `--all` 时使用。这里通常只保留 `path` 和 `enabled`；目标仓库自己的 display、git、runner 等配置应放在该仓库根目录的 `.iar.toml`。

## 配置说明

全局配置位于 `config.toml`，目标仓库 runner 配置位于 `.iar.toml`，敏感信息请使用 `.env` 文件管理。

主要配置项：
- `app.name` - 应用名称
- `app.log_level` - 日志级别
- `database.*` - 数据库配置
- `chat_model.*` - 聊天模型配置
- `agent_runner.*` - Agent Runner 配置（labels、git、worktree、runner、safety），仓库级覆盖优先放在 `.iar.toml`

## 开发指南

### 代码规范

- 使用 Google Style Docstrings
- 遵循 AI-Native 代码模式（详见 `AGENTS.md`）
- 提交前会自动运行 pre-commit hooks

### 常用命令

| 命令 | 说明 |
|------|------|
| `just dev` | 安装开发环境 |
| `just run` | 运行主程序 |
| `just test` | 运行测试 |
| `just docs-serve` | 启动文档服务 |
| `just clean` | 清理缓存文件 |

## 许可证

[请添加许可证信息]
