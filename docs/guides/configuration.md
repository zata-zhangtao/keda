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

`just worktree`（底层实现位于 `scripts/shared/worktree/create.sh`）支持以下环境变量来控制新 worktree 的依赖准备行为：

- `KODA_WORKTREE_BASE_BRANCH`
  - 新 worktree 默认使用的 base branch 名称，默认值为 `main`。
  - 命令行参数 `--base <branch>` 会覆盖这个环境变量。
- `KEDA_WORKTREE_SYNC_BASE`
  - 默认 `true`。创建 worktree 前自动 fetch 远程 base branch 并使用最新远程提交作为起点。
  - 设为 `false` 时关闭远程同步，保持旧行为（直接从本地 base branch 创建）。
  - 远程不存在时会自动回退到本地 base branch；远程存在但 fetch 失败时命令会非零退出，避免静默使用过期基线。
- `KEDA_WORKTREE_BASE_REMOTE`
  - 覆盖默认 remote 名称。未设置时优先读取 `branch.<base>.remote`，不存在时回退到 `origin`。
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

## Agent Runner 仓库配置

`config.toml` 的 `[agent_runner.repositories.<repo_id>]` 段支持配置多个目标仓库：

- `path`：本地已 clone 的仓库绝对路径（必填）。
- `enabled`：是否启用该仓库，默认为 `true`。
- `display_name`：前端展示名称，默认为 `repo_id`。

每个仓库可独立覆盖 `labels`、`git`、`worktree`、`runner`、`safety` 子配置：

```toml
[agent_runner.repositories.keda]
path = "/Users/zata/code/keda"
enabled = true
display_name = "Keda"

[agent_runner.repositories.keda.git]
remote = "origin"
base_branch = "main"

[agent_runner.repositories.backend_service]
path = "/Users/zata/code/backend-service"
enabled = true

[agent_runner.repositories.backend_service.runner]
verification_commands = [
  "git diff --check",
  "uv run pytest",
]
```

未覆盖的字段自动继承全局 `[agent_runner]` 默认值。环境变量仍可对全局段生效，但暂不支持通过环境变量覆盖单个仓库的字段。

## Agent Runner Deliberation 配置

`config.toml` 的 `[agent_runner.deliberation]` 段配置多 Agent 合议：

```toml
[agent_runner.deliberation]
default_rounds = 2
default_synthesizer = "claude"
default_output_dir = "logs/agent-runner/deliberations"

[agent_runner.deliberation.profiles.architect]
agent = "claude"
role = "architect"
behavior_prompt = "You are an experienced software architect..."

[agent_runner.deliberation.profiles.skeptic]
agent = "kimi"
role = "skeptic"
behavior_prompt = "You are a skeptical reviewer..."

[agent_runner.deliberation.profiles.implementer]
agent = "codex"
role = "implementer"
behavior_prompt = "You are a pragmatic implementer..."
```

- `default_rounds`：默认讨论轮数（不含综合轮）。
- `default_synthesizer`：默认综合 agent 名称。
- `default_output_dir`：默认输出根目录。
- `profiles.<profile_id>`：自定义参与者 profile，至少包含 `agent`、`role`、`behavior_prompt`。

## Agent Runner Interactive Decision 配置

`config.toml` 的 `[agent_runner.interactive_decision]` 段配置 `iar ask` 行为：

```toml
[agent_runner.interactive_decision]
enabled = true
default_agent = "claude"
default_output_dir = "logs/agent-runner/decisions"
planner_timeout_seconds = 120
max_context_chars = 24000
allow_execute_yes = true
```

- `enabled`：是否启用 `iar ask`。
- `default_agent`：默认 planner agent（支持 `claude`、`codex`、`kimi`）。
- `default_output_dir`：决策审计文件默认输出目录。
- `planner_timeout_seconds`：planner agent 超时时间（秒）。
- `max_context_chars`：传入 planner 的上下文最大字符数。
- `allow_execute_yes`：是否允许 `--yes` 非交互确认。

## Agent Runner REPL 配置

`config.toml` 的 `[agent_runner.repl]` 段配置 `iar` 无参数进入的交互式
REPL 入口。整段与 `[agent_runner.interactive_decision]` 隔离，二者可
独立调整默认 agent、超时、白名单策略。

```toml
[agent_runner.repl]
enabled = true
default_agent = "claude"
default_output_dir = "logs/agent-runner/repl"
max_context_chars = 24000
agent_timeout_seconds = 120
auto_confirm_commands = [
  "labels sync --dry-run",
  "run --dry-run",
  "review --dry-run",
  "ask --plan-only",
]
confirm_commands = [
  "run",
  "daemon",
  "review",
  "review-daemon",
  "issue create",
  "recover",
  "blocked-continue",
  "worktree create",
  "worktree remove",
]
```

- `enabled`：是否启用 REPL 入口；设为 `false` 时 `iar` 无参数仍走
  Typer 默认帮助路径。
- `default_agent`：默认 REPL agent（支持 `claude`、`codex`、`kimi`）。
  `auto` 不被接受为 REPL 默认 agent（`iar run` 才用 auto）。
- `default_output_dir`：REPL 会话审计目录前缀；每次会话创建
  `<default_output_dir>/<session-id>/`，包含 `session.json`、
  `transcript.md`、`commands.json`。
- `max_context_chars`：首条 system prompt 的最大字符数（超出部分被
  截断并保留头尾）。
- `agent_timeout_seconds`：每轮调用 agent 子进程的超时；超时或非零
  退出码会作为 `[IAR_EXEC_RESULT] exit_code=...` 块追加到对话历史。
- `auto_confirm_commands`：前缀匹配列表，匹配的命令直接执行。
- `confirm_commands`：前缀匹配列表，匹配的命令执行前先询问用户
  `Execute? [y/N]`。

`auto_confirm_commands` 与 `confirm_commands` 都按「剥离 `iar` 后剩余
的命令 tail」做前缀匹配，例如 `"run --dry-run"` 只对
`iar run --dry-run` 自动放行；`"run"` 则对 `iar run ...` 的所有调用
询问确认。

`.iar.toml` 可在 `[agent_runner.repl]` 段覆盖上述任意字段，实现仓库级
REPL 策略：默认全局 agent 是 `claude`，某个仓库可改为 `kimi` 或
`codex`；默认 dry-run 白名单之外的命令可通过
`confirm_commands` 在仓库层收紧或放宽。

## 预览部署配置

`config.toml` 的 `[preview]` 段控制 PR 预览部署的非敏感结构。敏感值（服务器地址、SSH 密钥、镜像仓库密码、数据库密码）必须通过 GitHub Secrets 注入，不得写入仓库文件。

```toml
[preview]
enabled = false
base_domain = "preview.example.com"
project_slug = "keda"
app_dir_root = "/opt/preview"
registry_host = "ghcr.io"
registry_namespace = "zata-zhangtao"
traefik_network = "traefik"
url_scheme = "https"
subdomain_template = "pr-{pr_number}.{base_domain}"
compose_template = "{project_slug}-pr-{pr_number}"
```

字段说明：

- `enabled`：是否启用预览部署工作流。设为 `true` 且配置 Secrets 后 PR 才会触发部署。
- `base_domain`：预览入口的基础域名，通配证书应覆盖 `*.<base_domain>`。
- `project_slug`：项目短标识，用于镜像名与 Compose project 名。
- `app_dir_root`：预览服务器上存放各 PR 栈的父目录。
- `registry_host` / `registry_namespace`：镜像仓库主机与命名空间。
- `traefik_network`：服务器上已存在的外部 Traefik 网络名。
- `url_scheme`：`https` 或 `http`，决定 sticky 评论中的 URL 协议。
- `subdomain_template`：PR 子域名模板，可用变量 `{pr_number}`、`{base_domain}`。
- `compose_template`：Compose project 名模板，可用变量 `{project_slug}`、`{pr_number}`。

环境变量覆盖：所有字段均可通过 `PREVIEW_` 前缀的环境变量覆盖，例如 `PREVIEW_ENABLED=true`。

## 日志相关配置

日志位于 `logs/` 目录，按日期命名，格式为 `app-YYYY-MM-DD.log`：

```bash
# 查看今天的日志
cat logs/app-$(date +%Y-%m-%d).log

# 实时查看日志
tail -f logs/app-$(date +%Y-%m-%d).log
```

### 日志特性

- **按日期划分**：每天生成一个独立的日志文件，如 `app-2026-05-24.log`
- **自动清理**：启动时自动删除超过 14 天的旧日志文件
- **时间戳格式**：日志条目使用 `YYYY-MM-DD HH:MM:SS` 格式
- **终端同步**：`iar` 命令的终端输出带有 `HH:MM:SS` 时间戳前缀

### 日志内容

日志文件记录以下内容：

- CLI 启动和配置加载事件
- Agent 工具调用摘要（如 `[agent tool] Read`）
- Agent 返回结果摘要（如 `[agent result]`）
- Agent 错误信息（如 `[agent error]`）
- Agent 输出文本（按消息边界汇总记录）
- 子进程输出（Codex/Kimi 等非 Claude agent 的输出）

## 数据库 URL 解析

`AppSettings.resolved_database_url` 支持：

- 直接使用 `DATABASE_URL`。
- 在未提供完整 URL 时，通过组件拼接生成连接字符串。
