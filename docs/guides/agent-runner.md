# Agent Runner 使用指南

`iar`（issue-agent-runner）是一个将 GitHub Issues 转为本地 AI Agent 队列的 CLI 工具，已按照本仓库的四层架构迁移并集成。

## 功能概述

- **labels sync**：在目标仓库创建或更新标准 labels（`agent/ready`、`agent/running`、`agent/supervising` 等）
- **issue-from-prd**：从 PRD Markdown 文件创建 GitHub Issue，并可在 ready 前发布 PRD
- **run-once**：单次轮询 `agent/ready` 的 Issues，claim 后执行 AI Agent，验证、pre-push review、创建 draft PR、进入 `agent/supervising` 并运行 post-PR supervisor
- **review-once**：单次检查 `agent/supervising` 和 `agent/review` 的 Issues，基于 PR 上下文变化运行 supervisor cycle
- **review-daemon**：常驻进程，按指定间隔循环执行 `review-once`
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
        AI 做完 → pre-push review → push → Draft PR → agent/supervising
               ↓
        supervisor 通过 → 换成 agent/review → 人工审完关闭
               ↓
        出问题 → 换成 agent/failed 或 agent/blocked
```

没有这些标签，`iar` 无法识别哪些 Issue 可以执行、哪些正在执行、哪些需要 review。

### 13 个标准标签

| 类别 | 标签 | 颜色 | 作用 |
|---|---|---|---|
| **AI 执行状态** | `agent/ready` | 🟢 绿色 | Issue 已准备好，等待 AI runner 认领 |
| | `agent/running` | 🟡 黄色 | 代码正在被修改（首次实现或 PR branch rework） |
| | `agent/supervising` | 🔵 浅蓝 | Draft PR 已创建，自动 post-PR supervisor 正在审查或重新处理 |
| | `agent/review` | 🔵 蓝色 | 自动总控审查已通过，当前 PR 等待人类 review |
| | `agent/failed` | 🔴 红色 | AI runner 执行失败 |
| | `agent/blocked` | ⬛ 黑色 | AI runner 需要人工介入 |
| **工具路由** | `agent/codex` | 🟣 紫色 | 指定使用 Codex 执行 |
| | `agent/claude` | 🩵 浅蓝 | 指定使用 Claude Code 执行 |
| | `agent/kimi` | 🩷 粉色 | 指定使用 Kimi 执行 |
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
iar labels sync --repo /path/to/target-repo

# 同步指定配置仓库
iar labels sync --repo-id keda
```

首次使用 `iar` 时只需执行一次，后续标签会自动复用。

## 多仓库配置

`iar` 支持在 `config.toml` 中配置多个目标仓库：

```toml
[agent_runner]
max_issues = 1

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
display_name = "Backend Service"

[agent_runner.repositories.backend_service.runner]
verification_commands = [
  "git diff --check",
  "uv run pytest",
]
```

- 每个仓库必须有 `path`（本地绝对路径）。
- `enabled = false` 可临时禁用某个仓库。
- 仓库级 `labels`、`git`、`worktree`、`runner`、`safety`、`pre_push_review`、`post_pr_supervisor` 可覆盖全局 `[agent_runner]` 默认值。
- 未指定 `--repo` 或 `--repo-id` 时，`labels sync`、`run-once`、`daemon`、`review-once`、`review-daemon` 会自动处理所有 `enabled = true` 的仓库。

## 状态流转与两阶段审查

### 完整状态机

```text
agent/ready
    → claim → agent/running
    → implementation agent commit
    → Issue comment: Implementation Complete
    → pre-push review (仍在 agent/running)
    → publish branch + Draft PR
    → agent/supervising
    → Issue comment: Draft PR Created
    → post-PR supervisor cycle
    → supervisor approve → agent/review
    → supervisor repair/rebase → agent/running (existing PR branch rework)
    → supervisor human-input-needed → agent/blocked
    → supervisor failed → agent/failed
```

### Pre-Push Review

`run-once` 在实现 agent 完成并提交后、push 之前，会执行一次 pre-push AI code review：

1. Runner 写 Issue comment `Implementation Complete`
2. Runner 构建 review packet（Issue、PRD、diff、changed paths、verification results、AI standards、review workflow）
3. 打开新的 AI session 执行 review
4. Reviewer 可直接修改 worktree；修改后写入 `.agent-runner/commit-request.json`
5. Runner 通过 commit proxy 提交 reviewer 修改并重新运行 `verification_commands`
6. Runner 写 Issue comment `Pre-Push Review Result`
7. Review 通过后才调用 `publish_changes`

Pre-push review 不产生独立的 durable label，整个过程仍在 `agent/running` 内。

### Post-PR Supervisor

Draft PR 创建后，Issue 先进入 `agent/supervising`，并立即运行至少一次 supervisor cycle：

1. Runner 写 Issue comment `Draft PR Created`
2. Supervisor 收集 PR context、Issue comments、PR comments、base branch 状态、CI/check 状态、diff、verification results
3. Supervisor 输出结构化 action：
   - `approve_for_human_review` → 进入 `agent/review`
   - `repair_pr_branch` / `resolve_conflict` → 进入 `agent/running` 做现有 PR branch 修复
   - `rebase_pr_branch` → 进入 `agent/running` 做 rebase
   - `request_human_input` → 进入 `agent/blocked`
   - `mark_failed` → 进入 `agent/failed`

4. 需要代码修改时，runner 先写 `post_pr_rework_requested` event marker，再切到 `agent/running`
5. 后续 `run-once` 或 `review-daemon` 检测到该 marker 和 open PR/branch 后，在现有 PR branch 上执行 rework

### 持续观察

```bash
# 单次检查所有 supervising/review Issues
uv run iar review-once

# 常驻 review daemon（默认每 600 秒轮询）
uv run iar review-daemon --interval 600
```

`review-once` / `review-daemon` 会：
- 扫描 `agent/supervising` 和 `agent/review` 的 open Issues
- 加载 linked PR context 和最新 `iar:event` marker
- 检测 head SHA、base SHA、CI/check 状态、comment 变化
- 变化时先移回 `agent/supervising`，运行 supervisor cycle
- 根据 supervisor 结果移动 label

### Rework Guard

`run-once` 遇到 `agent/running` Issue 时，不会自动视为 rework。只有同时满足以下两个条件才会进入现有 PR branch rework 路径：

1. 存在 open PR 或已知 PR branch
2. 最新 Issue comment 包含 `phase=post_pr_rework_requested` 的 `iar:event` marker

否则 `run-once` 会跳过该 Issue，避免抢占另一个 runner 正在首次执行的任务。

## 常用命令

```bash
# 同步 Labels（所有启用仓库）
iar labels sync

# 同步单个配置仓库
iar labels sync --repo-id keda

# 从 PRD 创建 ready Issue，并先发布 PRD
iar issue-from-prd tasks/pending/example.md --repo-id keda --type feature --agent codex --publish-prd --ready

# 单次执行（dry-run 预览）
iar run-once --dry-run

# 单次执行（所有启用仓库）
iar run-once

# Daemon 模式（默认每 600 秒轮询一次，所有启用仓库）
iar daemon

# 单次 review 检查
iar review-once

# Review daemon 模式
iar review-daemon --interval 600
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
   # 默认路径为 <repo>-worktrees/tasks/issue-<issue-number>
   git worktree remove <repo>-worktrees/tasks/issue-<issue-number>
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
   iar run-once

   # 或等待 daemon 下次轮询
   ```

### 状态流转回顾

```
agent/ready  →  agent/running  →  agent/supervising  →  agent/review  →  关闭
      ↑              ↓                                    ↓
      └──────  agent/failed  ←───────────────────────────┘
              （人工修复后改回 ready）

agent/supervising ── supervisor 要求 rework ──→ agent/running ── 修复/rebase ──→ agent/supervising
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
uv run iar labels sync
```

> `target-repo` 是你要 AI 改代码的目标仓库（不是 keda 本身）。

#### 3. 写 PRD 并创建 Issue

```bash
# 写 PRD 文件，例如 tasks/pending/feature-login.md
# 然后创建 GitHub Issue
uv run iar issue-from-prd tasks/pending/feature-login.md \
  --repo-id keda \
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

Runner 新建 issue worktree 时，默认会同步 base branch 的远程 tracking ref 作为起点，使新分支基于最新远程提交，而非可能过期的本地 base branch。复用已存在的 worktree 时不会自动 rebase 或 reset；如需更新基线，请手动处理或删除旧 worktree 后重建。

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
> `iar` 以无人值守方式调用 Claude Code，会使用 `--dangerously-skip-permissions` 跳过文件编辑权限确认，并启用 verbose `stream-json`。runner 会过滤原始 JSON，只显示工具调用摘要、assistant 文本和最终错误。

#### 3. 单次执行（测试用）

```bash
cd ~/keda

# Dry run 预览（不实际执行）
uv run iar run-once --dry-run

# 真正执行一次（所有启用仓库）
uv run iar run-once --agent codex
```

#### 4. Daemon 常驻模式（生产用）

```bash
cd ~/keda

# 每 600 秒（10 分钟）轮询一次（所有启用仓库）
uv run iar daemon --agent auto
```

> 建议用 `tmux`、`screen` 或 `systemd` 保持后台运行。

**用 tmux 保持后台：**

```bash
tmux new -s iar-daemon
cd ~/keda && uv run iar daemon
# 按 Ctrl+B 再按 D  detach
```

#### 5. Review Daemon（可选）

如果你希望 PR 创建后持续自动检查并维护 PR 状态：

```bash
cd ~/keda

# 每 600 秒检查一次 supervising/review Issues
uv run iar review-daemon --interval 600
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
uv run iar labels sync

# 4. 创建 Issue
uv run iar issue-from-prd tasks/pending/xxx.md --repo-id keda --agent codex --publish-prd --ready

# 5. 启动 daemon 自动执行
uv run iar daemon
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

[agent_runner.labels]
ready = "agent/ready"
running = "agent/running"
supervising = "agent/supervising"
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
default_agent = "auto"
max_recovery_attempts = 2
recovery_retry_delay_seconds = 30
verification_commands = [
  "git diff --check",
  "just test",
  "uv run mkdocs build --strict",
]

[agent_runner.safety]
auto_merge = false
forbidden_path_patterns = [
  ".env",
  ".env.*",
  "secrets/*",
  "docker-compose.prod.yml",
]

[agent_runner.prompts]
default_phase = "execution"

[agent_runner.prompts.phases]
execution = [
  "Complete GitHub Issue #{issue_number}: {issue_title}",
  "",
  "Issue URL: {issue_url}",
  "Worktree: {worktree_path}",
  "{prd_line}",
  "",
  "Issue body:",
  "{issue_body}",
  "",
  "Execution rules:",
  "- Read AGENTS.md and follow repository instructions.",
  "- Only modify files inside the current worktree.",
  "- Do not merge main, delete branches, push, or create PRs; the runner handles publishing.",
  "- Do not run `git add` or `git commit`; the runner exposes a restricted commit proxy.",
  "- After finishing your changes, request a commit by writing `.agent-runner/commit-request.json` as JSON with `commit_message`.",
  "- Do not touch production systems or real business data.",
  "- Implement the requested task with focused tests and docs updates.",
  "- Finish with a concise summary, tests run, and remaining risk.",
]

[agent_runner.pre_push_review]
enabled = true
review_agent = "auto"
allow_same_agent = true
max_attempts = 2

[agent_runner.post_pr_supervisor]
enabled = true
supervisor_agent = "auto"
max_repair_attempts = 2
```

配置优先级：环境变量 > `config.toml` > 代码默认值。

Prompt 模板支持以下变量占位符：

| 变量 | 说明 |
|---|---|
| `{issue_number}` | GitHub Issue 编号 |
| `{issue_title}` | GitHub Issue 标题 |
| `{issue_url}` | GitHub Issue URL |
| `{worktree_path}` | 当前 worktree 的绝对路径 |
| `{issue_body}` | Issue 完整正文 |
| `{prd_line}` | 自动生成的 PRD 引用行（有 PRD 时提示读取，无 PRD 时给出通用建议） |

## Issue Comment Event Markers

每个关键状态变化都会向 Issue 写入结构化 Markdown comment，并带隐藏 `iar:event` marker：

```markdown
<!-- iar:event version=1 phase=pre_push_review cycle=1 head=abc123 -->

## Agent Runner Pre-Push Review

- Verdict: approved
- Reviewer: codex
- Head Before: `abc123`
- Head After: `def456`
- Verification: passed
- Findings: 0 high, 0 medium, 0 low
- Action: reviewer approved without changes
```

Marker 是幂等 cursor，不依赖本地状态文件。可读正文用于人类审计。支持的 phase 包括：

- `implementation_complete`
- `pre_push_review`
- `draft_pr_created`
- `post_pr_supervisor`
- `post_pr_rework_requested`
- `rebase_repair_complete`

## 安全边界

- `auto_merge` 固定为 `false`，不会自动合并 PR
- `iar labels sync` 只同步 GitHub labels，不校验发布 remote；`iar run-once` 在领取 Issue 前会校验 `[agent_runner.git].remote` 必须存在，不存在时直接失败并列出当前可用 remote
- 发布变更前会检查 `forbidden_path_patterns`，拒绝匹配的文件变更
- Agent 执行在隔离 worktree 中进行，不影响主工作区
- Agent 不直接执行 `git add` 或 `git commit`；完成修改后写入 `.agent-runner/commit-request.json` 请求 runner 在 host 侧提交
- `commit-request.json` 只允许提供 `commit_message`；runner 会校验当前 branch 未变化、删除请求文件、检查 `forbidden_path_patterns`，再执行 `git add -A` 和 `git commit`
- 不同仓库应在 `verification_commands` 中配置自己的验证命令，例如 `just test`、`npm test`、`pnpm lint` 或 `make test`
- runner 会在提交前先运行一次 `verification_commands`；发现未提交变更并执行 `git add -A` 后，会再次运行同一组验证命令，覆盖依赖 staged 状态的 commit hook 或测试标记
- Agent CLI 非零退出或任一验证失败时，runner 最多按 `max_recovery_attempts` 重新调用同一个 Agent；每次 recovery 前会等待 `recovery_retry_delay_seconds` 秒，并把失败摘要或失败命令的 exit code、stdout、stderr 放入 recovery prompt；Agent 修复后仍只能写 commit request，不能直接提交
- 如果 Agent 没有产生任何新 commit 且工作区也没有未提交变更，runner 仍会将 Issue 标记为 `agent/failed`
- Pre-push reviewer 的修改同样必须通过 `verification_commands` 才能发布
- Post-PR supervisor 的 rebase 操作使用 `--force-with-lease` 且仅作用于 PR branch，不会推送 base branch
- 自动化 rebase 前会校验 HEAD 和 branch 名称，发现不匹配时中止，防止误操作

### PRD-backed Issue 的强制 Closeout

当 Issue body 中包含 `PRD path: \`tasks/pending/xxx.md\`` 时，runner 成功路径会强制完成 PRD closeout：

1. **Prompt 引导**：`build_prompt()` 从 `config.toml` 的 `[agent_runner.prompts.phases]` 模板渲染 prompt，默认模板会明确要求 Agent 在请求 commit 前更新 PRD 的 `Acceptance Checklist`，并在所有验收项完成后将 PRD 从 `tasks/pending/` 移动到 `tasks/archive/`。`build_recovery_prompt()` 也在 recovery 阶段给出同样的 closeout 提醒。
2. **提交前 Delivery Gate**：runner 在 `publish_changes()` 之前执行 PRD delivery gate：
   - 无 PRD path：跳过 gate，保持现有行为。
   - PRD 仍在 `tasks/pending/`：若 `Acceptance Checklist` 还有未勾选项，将失败原因交回 recovery prompt；若已全部勾选，runner 自动执行 `git mv tasks/pending/<name>.md tasks/archive/<name>.md`。
   - PRD 已在 `tasks/archive/`：校验 `Acceptance Checklist` 全部完成。
   - PRD 文件不存在、archive 目录缺失或 `Acceptance Checklist` section 缺失：进入 recovery loop，重试耗尽后标记 `agent/failed`。
3. **归档纳入同一 Commit**：`git mv` 发生在 `git add -A` 之前，因此 PRD 归档变更会随 Agent 的代码变更一起进入同一个 commit，并包含在随后创建的 Draft PR 中，不需要 publish 后再追加 commit。

> **注意**：runner 不会自动判断业务验收是否真实完成，只校验 Agent 是否已将 PRD 更新到交付完成态（checklist 全勾、文件在 archive）。

## FastAPI 状态端点

Agent Runner 同时暴露只读状态端点：

- `GET /api/v1/agent-runner/status` — 返回 runner 配置摘要与仓库列表
- `GET /api/v1/agent-runner/health` — 返回 runner 健康状态（GitHub CLI 可用性等）

## 架构说明

Agent Runner 的代码分布在四层架构中：

- `core/shared/models/agent_runner.py` — 领域模型（frozen dataclasses）
- `core/shared/interfaces/agent_runner.py` — 抽象端口（`IGitHubClient`、`IProcessRunner`）
- `core/use_cases/` — 业务用例（`sync_labels`、`create_issue_from_prd`、`run_agent_once`、`run_agent_repositories_once`、`run_agent_daemon`、`agent_review`、`pr_supervisor`、`review_once`、`review_daemon`）
- `engines/agent_runner/factory.py` — 基础设施适配层（实例化实现并注入用例）
- `infrastructure/github_client.py` / `infrastructure/process_runner.py` — 外部系统实现
- `api/cli.py` — CLI 入口
- `api/routes/agent_runner.py` — FastAPI 只读路由
