# Agent Runner 使用指南

`iar`（issue-agent-runner）是一个将 GitHub Issues 转为本地 AI Agent 队列的 CLI 工具，已按照本仓库的四层架构迁移并集成。

## 功能概述

- **labels sync**：在目标仓库创建或更新标准 labels（`agent/ready`、`agent/running` 等）
- **issue-from-prd**：从 PRD Markdown 文件创建 GitHub Issue，并可在 ready 前发布 PRD
- **run-once**：单次轮询 `agent/ready` 的 Issues，claim 后执行 AI Agent，验证并创建 draft PR
- **daemon**：常驻进程，按指定间隔循环执行 `run-once`

## 安装

`iar` 已通过 `pyproject.toml` 的 `[project.scripts]` 注册：

```bash
# 通过 uv 运行
uv run iar --help

# 或安装后直接使用
iar --help
```

## labels sync 详解

`iar labels sync` 会在目标 GitHub 仓库中创建或更新一套标准化的 issue 标签，作为整个 agent-runner 工作流的状态基础设施。

### 为什么需要同步标签？

`iar` 依靠 GitHub labels 实现任务状态的自动流转：

```
创建 Issue → 贴上 agent/ready → AI 认领（换成 agent/running）
               ↓
        AI 做完 → 换成 agent/review → 人工审完关闭
               ↓
        出问题 → 换成 agent/failed 或 agent/blocked
```

没有这些标签，`iar` 无法识别哪些 Issue 可以执行、哪些正在执行、哪些需要 review。

### 12 个标准标签

| 类别 | 标签 | 颜色 | 作用 |
|---|---|---|---|
| **AI 执行状态** | `agent/ready` | 🟢 绿色 | Issue 已准备好，等待 AI runner 认领 |
| | `agent/running` | 🟡 黄色 | AI runner 正在执行 |
| | `agent/review` | 🔵 蓝色 | AI runner 已完成，等待人工 review |
| | `agent/failed` | 🔴 红色 | AI runner 执行失败 |
| | `agent/blocked` | ⬛ 黑色 | AI runner 需要人工介入 |
| **工具路由** | `agent/codex` | 🟣 紫色 | 指定使用 Codex 执行 |
| | `agent/claude` | 🩵 浅蓝 | 指定使用 Claude Code 执行 |
| **来源标识** | `source/prd` | 🔵 深蓝 | Issue 关联了仓库内的 PRD 文件 |
| **任务类型** | `type/feature` | 🔵 | 功能需求 |
| | `type/refactor` | 🟣 | 代码重构 |
| | `type/bug` | 🔴 | Bug 修复 |
| **队列状态** | `status/backlog` | 🩵 | 待办/未开始 |

> 标签名称可在 `config.toml` 的 `[agent_runner.labels]` 段自定义。

### 使用示例

```bash
# 同步当前目录对应的仓库
iar labels sync

# 同步指定路径的仓库
iar labels sync [--repo]
```

首次使用 `iar` 时只需执行一次，后续标签会自动复用。

## 常用命令

```bash
# 同步 Labels
iar labels sync [--repo]

# 从 PRD 创建 ready Issue，并先发布 PRD
iar issue-from-prd tasks/pending/example.md [--repo] --type feature --agent codex --publish-prd --ready

# 单次执行（dry-run 预览）
iar run-once [--repo] --dry-run

# 单次执行
iar run-once [--repo]

# Daemon 模式（每 600 秒轮询一次）
iar daemon [--repo] --interval 600
```

## 失败重跑

Issue 执行失败后会被标记为 `agent/failed`，runner 不会再自动处理。以下是将失败 Issue 重新置为可执行状态的完整流程。

### 何时适合重跑

- 临时网络故障或 API 限流导致 Agent 中断
- 本地环境问题（如 API Key 失效、worktree 权限错误）已修复
- 目标仓库的 pre-commit hook 等外部检查临时失败

> **不建议重跑的情况**：Issue 描述本身有误、Agent 逻辑已正确执行但业务结果不符合预期。这类情况应修改 Issue 内容或人工接管，而不是简单重跑。

### 操作步骤

1. **可选：清理旧的 worktree**

   如果上一次失败时 worktree 已创建但处于脏状态，建议先清理，避免残留文件影响重跑：

   ```bash
   # 删除对应 issue 的 worktree（将 <issue-number> 替换为实际编号）
   git worktree remove issue-<issue-number>
   ```

2. **将标签从 `agent/failed` 改为 `agent/ready`**

   使用 GitHub CLI：

   ```bash
   gh issue edit <issue-number> --add-label ready --remove-label failed
   ```

   或者在 GitHub 网页上手动编辑 Issue 标签，移除 `agent/failed` 并添加 `agent/ready`。

3. **触发 runner 执行**

   标签改回 `ready` 后，runner 会在下一次轮询时自动拾取：

   ```bash
   # 单次轮询（立即执行）
   iar run-once [--repo]

   # 或等待 daemon 下次轮询
   ```

### 状态流转回顾

```
agent/ready  →  agent/running  →  agent/review  →  关闭
      ↑              ↓
      └──────  agent/failed  ←─────┘
              （人工修复后改回 ready）
```

## 多机部署与操作指南

`iar` 支持在单台或多台电脑上运行。以下介绍两种典型部署方式：

### 角色分工

| 电脑 | 角色 | 做什么 |
|------|------|--------|
| **A 电脑** | 任务管理端 | 写 PRD → 创建 GitHub Issue → 查看 AI 生成的 PR |
| **B 电脑** | Agent 执行端 | 常驻运行 `iar daemon`，轮询 Issue、执行 AI、提交代码 |
| **同一台电脑** | 混合 | A 和 B 的操作都在这台机器上执行 |

### A 电脑操作（任务管理端）

#### 1. 环境准备

```bash
# 克隆仓库（即本项目 keda）
git clone <keda-repo-url>
cd keda

# 安装依赖
just sync

# 确保 GitHub CLI 已登录
gh auth login
```

#### 2. 初始化 Labels（只需一次）

```bash
uv run iar labels sync [--repo]
```

> `target-repo` 是你要 AI 改代码的目标仓库（不是 keda 本身）。

#### 3. 写 PRD 并创建 Issue

```bash
# 写 PRD 文件，例如 tasks/pending/feature-login.md
# 然后创建 GitHub Issue
uv run iar issue-from-prd tasks/pending/feature-login.md \
  [--repo] \
  --type feature \
  --agent codex \
  --publish-prd \
  --ready
```

> `--agent` 可选 `codex` / `claude` / `auto`（按 Issue label 自动路由）。推荐在交给 runner 前使用 `--publish-prd --ready`，确保 runner 的 base branch 能读取到已回写 Issue URL 的 canonical PRD。

#### `--publish-prd` 发布边界

`--publish-prd` 是显式 Git 发布行为，未传入时 `issue-from-prd` 只创建 Issue 并本地回写 PRD，不执行 `git add`、`git commit` 或 `git push`。

传入 `--publish-prd` 后，命令会在 Issue URL 回写到目标 PRD 后执行 PRD-only 发布：只 `git add` 传入的 PRD 文件，只提交该 PRD 文件，然后 push 到 `config.toml` 中 `[agent_runner.git]` 配置的 remote。工作区其他未跟踪或已修改文件不会被加入这个 commit；如果 Git index 里已经 staged 了非目标 PRD 文件，命令会失败，避免把用户已有 staged changes 混入 PRD 发布 commit。

当同时传入 `--publish-prd --ready` 时，创建 Issue 的第一步不会带 `agent/ready`。只有 PRD commit push 成功后，命令才会通过 GitHub API 给 Issue 添加 `agent/ready`。如果 push 失败，命令返回失败，保留已创建但未 ready 的 backlog Issue，runner 不会领取它。

Ready 发布要求当前分支等于 `[agent_runner.git].base_branch`，因为 runner 默认从 base branch 创建 worktree。若当前分支不是 base branch，命令会失败并提示切换到 base branch 或改用 `--no-ready`。

#### 4. 查看结果

等待 B 电脑执行完毕后，去 GitHub 上 Review AI 生成的 Draft PR。

### B 电脑操作（Agent 执行端）

#### 1. 环境准备

```bash
# 克隆目标仓库（AI 要修改的代码仓库）
git clone <target-repo-url>
cd target-repo

# 安装 keda 项目依赖（需要 iar CLI）
git clone <keda-repo-url> ~/keda
cd ~/keda && just sync
```

#### 2. 安装 AI Agent CLI

根据你想用的 Agent，安装对应工具（二选一或都装）：

**用 Codex（OpenAI）：**

```bash
# 安装 Codex CLI
npm install -g @openai/codex

# 配置 API Key
export OPENAI_API_KEY="sk-your-openai-key"
```

**用 Claude Code（Anthropic）：**

```bash
# 安装 Claude Code
npm install -g @anthropic-ai/claude-code

# 配置 API Key
export ANTHROPIC_API_KEY="sk-ant-your-anthropic-key"
```

> 也可以把 API Key 写到 `.env` 文件里，iar 会自动加载。

#### 3. 单次执行（测试用）

```bash
cd ~/keda

# Dry run 预览（不实际执行）
uv run iar run-once [--repo] --dry-run

# 真正执行一次
uv run iar run-once [--repo] --agent codex
```

#### 4. Daemon 常驻模式（生产用）

```bash
cd ~/keda

# 每 600 秒（10 分钟）轮询一次
uv run iar daemon [--repo] --interval 600 --agent auto
```

> 建议用 `tmux`、`screen` 或 `systemd` 保持后台运行。

**用 tmux 保持后台：**

```bash
tmux new -s iar-daemon
cd ~/keda && uv run iar daemon [--repo] --interval 600
# 按 Ctrl+B 再按 D  detach
```

### 同一台电脑运行

如果 A、B 是同一台电脑，直接合并操作：

```bash
# 1. 准备环境
git clone <keda-repo-url>
cd keda && just sync
gh auth login

# 2. 安装 AI Agent（codex 或 claude）
npm install -g @openai/codex
export OPENAI_API_KEY="sk-xxx"

# 3. 同步 labels（首次）
uv run iar labels sync [--repo]

# 4. 创建 Issue
uv run iar issue-from-prd tasks/pending/xxx.md [--repo] --agent codex --publish-prd --ready

# 5. 启动 daemon 自动执行
uv run iar daemon [--repo] --interval 600
```

### 运行前检查清单

| 检查项 | 命令 |
|--------|------|
| GitHub CLI 已登录？ | `gh auth status` |
| `codex` 可用？ | `codex --version` |
| `claude` 可用？ | `claude --version` |
| API Key 已设置？ | `echo $OPENAI_API_KEY` / `echo $ANTHROPIC_API_KEY` |
| 目标仓库路径正确？ | `ls /path/to/target-repo/.git` |

## 配置

Agent Runner 的配置统一放在 `config.toml` 的 `[agent_runner]` 段：

```toml
[agent_runner]
max_issues = 1
default_agent = "auto"

[agent_runner.labels]
ready = "agent/ready"
running = "agent/running"
review = "agent/review"
failed = "agent/failed"
blocked = "agent/blocked"
codex = "agent/codex"
claude = "agent/claude"

[agent_runner.git]
remote = "origin"
base_branch = "main"

[agent_runner.worktree]
create_command = "just worktree issue-{issue_number} enter_shell=false"
reuse_command = "bash -c 'test -d \"$(dirname \"$(git rev-parse --show-toplevel)\")/issue-{issue_number}\"'"
path_command = "bash -c 'echo \"$(dirname \"$(git rev-parse --show-toplevel)\")/issue-{issue_number}\"'"

[agent_runner.runner]
verification_commands = [
  "git diff --check",
  "uv run mkdocs build",
]

[agent_runner.safety]
auto_merge = false
forbidden_path_patterns = [
  ".env",
  ".env.*",
  "secrets/*",
  "docker-compose.prod.yml",
]
```

配置优先级：环境变量 > `config.toml` > 代码默认值。

## 安全边界

- `auto_merge` 固定为 `false`，不会自动合并 PR
- 发布变更前会检查 `forbidden_path_patterns`，拒绝匹配的文件变更
- Agent 执行在隔离 worktree 中进行，不影响主工作区

## FastAPI 状态端点

Agent Runner 同时暴露只读状态端点：

- `GET /api/v1/agent-runner/status` — 返回 runner 配置摘要
- `GET /api/v1/agent-runner/health` — 返回 runner 健康状态（GitHub CLI 可用性等）

## 架构说明

Agent Runner 的代码分布在四层架构中：

- `core/shared/models/agent_runner.py` — 领域模型（frozen dataclasses）
- `core/shared/interfaces/agent_runner.py` — 抽象端口（`IGitHubClient`、`IProcessRunner`）
- `core/use_cases/` — 业务用例（`sync_labels`、`create_issue_from_prd`、`run_agent_once`、`run_agent_daemon`）
- `engines/agent_runner/factory.py` — 基础设施适配层（实例化实现并注入用例）
- `infrastructure/github_client.py` / `infrastructure/process_runner.py` — 外部系统实现
- `api/cli.py` — CLI 入口
- `api/routes/agent_runner.py` — FastAPI 只读路由
