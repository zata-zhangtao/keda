# Agent Runner 使用指南

`iar`（issue-agent-runner）是一个将 GitHub Issues 转为本地 AI Agent 队列的 CLI 工具，已按照本仓库的四层架构迁移并集成。

CLI 入口基于 Typer/Rich：`iar --help` 会展示分组命令、参数和别名；脚本可继续使用历史命令，日常人工操作优先使用更短的别名。

## 功能概述

- **init**：在目标 Git 仓库创建仓库本地 `.iar.toml` 配置
- **labels sync**：在目标仓库创建或更新标准 labels（`agent/ready`、`agent/running`、`agent/supervising` 等）
- **issue create**：从 PRD Markdown 文件创建 GitHub Issue，并可在 ready 前发布 PRD（兼容旧命令 `issue-from-prd`）
- **run**：单次轮询 `agent/ready` 的 Issues，claim 后执行 AI Agent，验证、pre-push review、创建 draft PR、进入 `agent/supervising` 并运行 post-PR supervisor（兼容旧命令 `run-once`）
- **review**：单次检查 `agent/supervising` 和 `agent/review` 的 Issues，基于 PR 上下文变化运行 supervisor cycle（兼容旧命令 `review-once`）
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

### Shell 自动补全

`iar` 支持生成 shell completion。zsh 用户安装后，输入 `iar is<Tab>` 可补全到 `issue` / `issue-from-prd`：

```bash
iar completion install --shell zsh
source ~/.zshrc
```

如需只查看脚本内容而不写入配置：

```bash
iar completion show --shell zsh
```

`completion install` 同时支持 `--shell bash` 和 `--shell fish`。

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

### 14 个标准标签

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

> 标签名称可在全局 `config.toml` 或目标仓库 `.iar.toml` 的 `[agent_runner.labels]` 段自定义。

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

## 仓库本地配置

`iar` 默认以当前 Git 仓库作为目标仓库。首次在目标仓库使用前，先生成仓库本地配置：

```bash
cd /path/to/target-repo
uv run --project /path/to/keda iar init --dry-run
uv run --project /path/to/keda iar init
```

`iar init --dry-run` 只打印将要写入的内容，不创建文件。`.iar.toml` 已存在时，`iar init` 会拒绝覆盖；确认需要重建时显式传入 `--force`。

生成示例：

```toml
[agent_runner.repository]
id = "target-repo"
enabled = true
display_name = "target-repo"

[agent_runner.git]
remote = "origin"
base_branch = "main"

[agent_runner.runner]
verification_commands = [
  "git diff --check",
]
```

仓库本地 `.iar.toml` 可覆盖 `git`、`runner`、`labels`、`worktree`、`safety`、`prompts`、`pre_push_review`、`post_pr_supervisor` 和 `generated_content`。`config.toml` 继续保存全局默认值、环境级设置和 legacy registry，不应保存 token、API key 或账号凭据。

单仓库命令的目标解析规则：

| 命令形态 | 目标解析 |
|---|---|
| `iar run` / `iar labels sync` / `iar review` / `iar issue create ...` | 当前 Git 仓库，合并当前仓库 `.iar.toml` |
| `iar run --repo /path/to/repo` | 指定 Git 仓库，合并 `/path/to/repo/.iar.toml` |
| `iar --repo /path/to/repo run` | 等价的顶层 selector 写法，适合把目标仓库放在命令前 |
| `iar run --repo-id keda` | 从 legacy registry 找到路径，再合并目标仓库 `.iar.toml` |
| `iar run --all` | 显式处理 `config.toml` 中所有 enabled registry entries |

历史命令 `iar run-once`、`iar review-once`、`iar issue-from-prd` 和 `iar recover-publish` 仍可用于脚本兼容。

## 多仓库 Registry 兼容

`config.toml` 中的 `[agent_runner.repositories.*]` 现在作为显式 registry 兼容路径使用，适合 `--repo-id` 或 `--all`：

```toml
[agent_runner]
max_issues = 1

[agent_runner.repositories.keda]
path = "/Users/zata/code/keda"
enabled = true

[agent_runner.repositories.backend_service]
path = "/Users/zata/code/backend-service"
enabled = true
```

- 每个仓库必须有 `path`（本地绝对路径）。
- `enabled = false` 可临时禁用某个仓库。
- registry 通常只保留 `path` 和 `enabled`；仓库级 overrides 仍兼容，但建议迁移到目标仓库的 `.iar.toml`。
- 未指定 `--repo`、`--repo-id` 或 `--all` 时，单仓库命令只处理当前 Git 仓库，不会隐式处理所有 enabled registry entries。

迁移示例：

```toml
# 旧 config.toml
[agent_runner.repositories.backend_service]
path = "/Users/zata/code/backend-service"
enabled = true
display_name = "Backend Service"

[agent_runner.repositories.backend_service.git]
remote = "origin"
base_branch = "main"

[agent_runner.repositories.backend_service.runner]
verification_commands = ["git diff --check", "uv run pytest"]
```

迁移后保留轻量 registry：

```toml
# keda/config.toml
[agent_runner.repositories.backend_service]
path = "/Users/zata/code/backend-service"
enabled = true
```

把仓库细节放到目标仓库：

```toml
# /Users/zata/code/backend-service/.iar.toml
[agent_runner.repository]
id = "backend_service"
enabled = true
display_name = "Backend Service"

[agent_runner.git]
remote = "origin"
base_branch = "main"

[agent_runner.runner]
verification_commands = ["git diff --check", "uv run pytest"]
```

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
3. 打开新的 AI session 执行 review，并使用 `[agent_runner.pre_push_review].timeout_seconds` 限制最长运行时间
4. Reviewer 可直接修改 worktree；修改后写入 `.agent-runner/commit-request.json`
5. Runner 通过 commit proxy 提交 reviewer 修改并重新运行 `verification_commands`
6. Runner 写 Issue comment `Pre-Push Review Result`
7. Review 通过后才调用 `publish_changes`

Pre-push review 不产生独立的 durable label，整个过程仍在 `agent/running` 内。Runner 会记录 review start、cycle、reviewer exit code、parsed verdict、commit-request 处理和 result comment 写入等日志；底层进程 runner 对长时间运行的 agent 命令每 60 秒输出一次 heartbeat，并在达到 timeout 时终止子进程。

> **空 commit request 行为**：当 reviewer 写出了 `.agent-runner/commit-request.json` 但工作树已无任何可提交改动（例如 reviewer 的建议与现状一致，或上一轮 cycle 已经提交过修复），runner 会按 reviewer 解析出的真实 verdict 处理：
>
> - `approved` → 写一条 `Pre-Push Review Result` 评论（action summary 为 `reviewer approved with an empty commit request`），循环正常收敛。
> - `changes_requested` → 写一条评论（action summary 为 `reviewer requested changes but produced no committable diff`）并继续下一轮 cycle；用尽 `max_attempts` 后走 `Pre-push review did not approve after N attempt(s): ...` 软失败路径。
>
> 若 reviewer stdout 没有可解析的 JSON verdict，runner 会在 commit request 中读取可选的 `verdict`、`summary`、`findings_high`、`findings_medium`、`findings_low` 元数据作为兜底。空提交信号由 `EmptyCommitRequestError`（`RuntimeError` 的子类，message 保持 `"Agent requested a commit but produced no file changes."`）承载，因此 `is_recoverable_commit_request_error(...)` 仍把它分类为可恢复，且不会被升级为 `Pre-push review repair failed` 硬失败。

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
- 加载 linked PR context、Issue comments、PR comments 和最新 `iar:event` marker
- 检测以下维度变化：
  - `head_sha` 或 `base_sha` 变化
  - `checks_state` 变化（如 CI 从 `PENDING` 变为 `FAILURE`）
  - `mergeable` 状态变化（如冲突出现或消失）
  - Issue comments 数量超过最新 supervisor marker 记录的游标
  - PR review comments 数量增加
- 任一维度变化时，先移回 `agent/supervising`，运行 supervisor cycle
- 无变化时直接 skip，避免无意义重评

Supervisor 结果评论会把自身写入后的 Issue comment 数量记录进 marker，
因此 runner 自己写出的 `Agent Runner Post-PR Supervisor` 评论不会触发下一轮重审。

PR context 读取使用当前 GitHub CLI 支持的 `statusCheckRollup` 字段聚合
checks 状态：

- 任一 check/status 失败时，`checks_state=FAILURE`
- 任一 check/status 仍在 queued、in_progress 或 pending 时，`checks_state=PENDING`
- 所有 check/status 成功、skipped 或 neutral 时，`checks_state=SUCCESS`
- 无 CI/check rollup 时，`checks_state` 为空，不阻断人工 review

即使 supervisor 返回 `approve_for_human_review`，runner 仍会执行确定性门禁：

- PR 当前不可合并或存在冲突时，approval 会被改写为 `rebase_pr_branch`
- PR checks 已失败时，approval 会被改写为 `repair_pr_branch`
- open PR 存在但完整 PR context 暂时无法读取时，本轮 supervision 会 defer 并保留待观察状态；发布、rework 后续评审和循环内再次评审都不会使用不完整 context 批准进入 review

这样可以避免 `agent/review` label 覆盖仍需 `run-once` 消费的 rework/rebase 状态。

`review-once` 的 CLI 日志会打印本轮 outcome，例如 `queued_rebase_pr_branch`、
`approved_for_human_review` 或 `deferred_pr_context_unavailable`。被 queue 的
rebase/repair 仍由下一次 `iar run-once` 在 PR branch worktree 中执行。

### Rework Guard

`run-once` 遇到 `agent/running` Issue 时，不会自动视为 rework。只有同时满足以下两个条件才会进入现有 PR branch rework 路径：

1. 存在 open PR 或已知 PR branch
2. 最新 Issue comment 包含 `phase=post_pr_rework_requested` 的 `iar:event` marker

否则 `run-once` 会跳过该 Issue，避免抢占另一个 runner 正在首次执行的任务。

## 常用命令

```bash
# 初始化当前目标仓库配置
iar init

# 同步当前仓库 Labels
iar labels sync

# 同步单个配置仓库
iar labels sync --repo-id keda

# 从 PRD 创建 ready Issue，并先发布 PRD
iar issue create tasks/pending/example.md --repo-id keda --type feature --agent codex --publish-prd --ready

# 单次执行（dry-run 预览）
iar run --dry-run

# 单次执行（当前仓库）
iar run

# 显式处理所有 enabled registry entries
iar run --all

# Daemon 模式（默认每 600 秒轮询一次，当前仓库）
iar daemon

# 单次 review 检查
iar review

# Review daemon 模式
iar review-daemon --interval 600

# 恢复发布失败（仅用于已完成审查后的 push/PR 收尾失败）
iar recover --issue 5

# 恢复发布失败（显式确认分支名）
iar recover --issue 5 --branch issue-5

# 兼容旧命令仍可使用
iar issue-from-prd tasks/pending/example.md --repo-id keda --type feature --agent codex --publish-prd --ready
iar run-once --dry-run
iar review-once
iar recover-publish --issue 5
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
   gh issue edit <issue-number> --add-label agent/ready --remove-label agent/failed
   ```

   或者在 GitHub 网页上手动编辑 Issue 标签，移除 `agent/failed` 并添加 `agent/ready`。

3. **触发 runner 执行**

   标签改回 `agent/ready` 后，runner 会在下一次轮询时自动拾取：

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

# recover-publish 恢复路径（supervisor enabled）
agent/failed ── recover-publish ──→ agent/supervising ── supervisor approve ──→ agent/review

# recover-publish 恢复路径（supervisor disabled）
agent/failed ── recover-publish ──→ agent/review
```

## 发布失败恢复

当 Agent 已完成代码修改、生成本地 commit，并且 runner 已经走到发布阶段（push、PR 创建、label 更新等）后失败时，Issue 会被标记为 `agent/failed`。此时重新运行 Agent 是浪费且可能引入不必要代码变更的。

`iar recover-publish` 命令用于安全、幂等地完成发布收尾，无需重新启动 Agent。

### 何时使用 recover-publish

- Agent 已执行完毕，本地 commit 已存在
- 发布阶段因网络错误、GitHub CLI 认证过期、API 限流等原因失败
- Issue 失败 comment 中包含 `iar recover-publish --issue <number>` 提示
- 该本地 commit 已经由正常 runner 路径完成过配置启用的 pre-push review，失败点只在 push、PR 创建或 label/comment 更新等发布收尾阶段

### 不适用的情况

- Agent 未产生任何 commit
- 工作区有未提交变更
- 需要修改 Agent 已生成的代码
- 当前分支是 base branch
- forbidden path 在 commit 阶段拦截后，由人工整理并提交了 worktree，但这些提交还没有经过 pre-push review

> **注意**：`iar labels sync` 只同步 GitHub labels，**不**校验发布环境。`iar run-once` 在领取 Issue 前会检查 `[agent_runner.git].remote` 是否存在。

### 与 pre-push review 和 post-PR supervisor 的关系

`recover-publish` 只做发布收尾：校验 worktree 干净、校验分支、push、创建或复用 Draft PR、更新 Issue label 和 comment。它不会运行 pre-push review（因为恢复路径要求本地 commit 已经由正常 runner 路径完成过 pre-push review）。

**当 `post_pr_supervisor.enabled = true` 时**，成功恢复后会先进入 `agent/supervising` 并运行 post-PR supervisor；只有 supervisor `approve_for_human_review` 后，Issue 才会进入 `agent/review`。

**当 `post_pr_supervisor.enabled = false` 时**，成功恢复后直接移除 `agent/failed` / `agent/running` / `agent/ready`，添加 `agent/review`。

如果失败发生在 `Refusing to publish forbidden paths: ...` 这类 forbidden path 拦截处，并且人工已经确认这些文件可以提交、手动创建了本地 commit，应改走 `agent/running` 的本地 commit 复用路径，让 `run-once` 执行完整的 verification、pre-push review、publish 和 post-PR supervisor：

```bash
# 1. 在对应 issue worktree 中确认已有本地 commit，且工作区干净
git status --short
git log -1 --oneline

# 2. 将 Issue 改回 running，让 run-once 通过本地 commit 恢复路径处理
gh issue edit <number> --add-label agent/running --remove-label agent/failed,agent/ready

# 3. 触发一次 runner 轮询；run-once 没有 --issue 参数，会扫描可处理的 Issues
uv run iar run-once
```

这条恢复路径要求 worktree 相对配置的 `{remote}/{base_branch}` 有本地 commit，且 `git status --short` 为空。若当前还有 `agent/ready` backlog，runner 会先消耗 ready 配额；必要时提高 `--max-issues`，或在没有 ready backlog 时执行。

### 使用方法

```bash
# 恢复 Issue #5 的发布
uv run iar recover-publish --issue 5

# 如果当前分支名不包含 issue 编号，需要显式确认分支
uv run iar recover-publish --issue 5 --branch feature-xyz
```

### 分支安全与 Issue number 边界

`recover-publish` 默认要求当前分支名把 Issue number 当作**完整 token 或路径 segment** 包含在内。以下分支在恢复 Issue #42 时会被**拒绝**：

- `issue-421`（42 不是完整 segment）
- `feature/issue-420`（420 ≠ 42）
- `task-142`（142 ≠ 42）

以下分支会被**接受**：

- `issue-42`
- `feature/issue-42`
- `task-42`
- `issue_42`

如果当前分支确实不匹配但你想强制恢复，使用 `--branch` 显式确认：

```bash
uv run iar recover-publish --issue 42 --branch issue-421
```

此时 runner 会精确比较当前分支与 `--branch` 参数，完全相等才放行。

### 恢复流程

1. 解析已存在的 issue worktree 路径
2. 校验工作区干净（无未提交变更）
3. 校验分支安全（非 base branch、分支名精确引用 issue 编号或显式 `--branch` 确认）
4. 校验配置的 remote 存在
5. Push 当前分支到配置 remote
6. 检查是否已有 open PR，有则复用，无则创建 draft PR
7. 发布成功 comment，记录分支、HEAD SHA、PR URL 和是否复用已有 PR
8. 更新 Issue labels：
   - `post_pr_supervisor.enabled = true`：移除 `agent/failed` / `agent/running` / `agent/ready` / `agent/review`，添加 `agent/supervising`，然后运行 supervisor
   - `post_pr_supervisor.enabled = false`：移除 `agent/failed` / `agent/running` / `agent/ready`，添加 `agent/review`

### 安全边界

`recover-publish` **不会**执行以下操作：

- 运行 implementation Agent 命令或 recovery prompt
- 执行 `git add` 或 `git commit`
- 创建新的 worktree
- 合并分支或删除分支
- 推送到非配置 remote

当 `post_pr_supervisor.enabled = true` 时，`recover-publish` 会复用现有 supervisor repair loop，但 supervisor 本身仍然是只读审阅；需要代码修改时由现有 repair/rebase commit proxy 处理，不会由 supervisor 直接提交文件。

### 手动恢复回退

当无法使用 `recover-publish` 命令、需要人工兜底时，可手动执行以下命令完成恢复。

**如果 `post_pr_supervisor.enabled = true`：**

```bash
# 1. 进入 issue worktree
cd <repo>-worktrees/tasks/issue-<number>

# 2. 确认当前分支和 commit
git branch --show-current
git log -1 --oneline

# 3. 推送分支到 remote
git push -u origin <branch>

# 4. 创建 draft PR（如果不存在）
gh pr create --draft --base main --title "[Agent] Issue #<number>" --body "Closes #<number>"

# 5. 更新 Issue labels 到 supervising（等待 supervisor 审批后再进入 review）
gh issue edit <number> --add-label agent/supervising --remove-label agent/failed,agent/running,agent/ready

# 6. 添加 comment 记录恢复结果
gh issue comment <number> --body "## Agent Runner Publish Recovered

- Branch: \`<branch>\`
- HEAD SHA: \`<sha>\`
- Draft PR: <pr-url>"
```

**如果 `post_pr_supervisor.enabled = false`（直接 review fallback）：**

```bash
# ...步骤 1-4 同上...

# 5. 更新 Issue labels 直接到 review
gh issue edit <number> --add-label agent/review --remove-label agent/failed,agent/running,agent/ready
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
# 克隆目标仓库和 keda CLI 项目
git clone <target-repo-url>
git clone <keda-repo-url> /path/to/keda
cd /path/to/keda

# 安装依赖
just sync

# 确保 GitHub CLI 已登录
gh auth login
```

#### 2. 初始化 Labels（只需一次）

```bash
cd /path/to/target-repo
uv run --project /path/to/keda iar init
uv run --project /path/to/keda iar labels sync
```

> `target-repo` 是你要 AI 改代码的目标仓库（不是 keda 本身）。如果已经安装了 `iar` 脚本，也可以在目标仓库中直接运行 `iar init` 和 `iar labels sync`。

#### 3. 写 PRD 并创建 Issue

```bash
# 写 PRD 文件，例如 tasks/pending/feature-login.md
# 然后创建 GitHub Issue
cd /path/to/target-repo
uv run --project /path/to/keda iar issue-from-prd tasks/pending/feature-login.md \
  --type feature \
  --agent codex \
  --publish-prd \
  --ready
```

> `--agent` 可选 `codex` / `claude` / `kimi` / `auto` / `none`。`auto` 按 Issue label 自动路由，`none` 不添加 agent 路由 label。推荐在交给 runner 前使用 `--publish-prd --ready`，确保 runner 的 base branch 能读取到已回写 Issue URL 的 canonical PRD。

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

根据你想用的 Agent，安装对应工具：

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

**用 Kimi：**

```bash
# 安装并配置本地 kimi CLI，确保 runner 可以直接执行 kimi
kimi --help
```

> 也可以把 API Key 写到 `.env` 文件里，iar 会自动加载。
> `iar` 以无人值守方式调用 Claude Code，会使用 `--dangerously-skip-permissions` 跳过文件编辑权限确认，并启用 verbose `stream-json`。runner 会过滤原始 JSON，只显示工具调用摘要、assistant 文本和最终错误。

#### 3. 单次执行（测试用）

```bash
cd /path/to/target-repo

# Dry run 预览（不实际执行）
uv run --project ~/keda iar run-once --dry-run

# 真正执行一次（当前仓库）
uv run --project ~/keda iar run-once --agent codex
```

#### 4. Daemon 常驻模式（生产用）

```bash
cd /path/to/target-repo

# 每 600 秒（10 分钟）轮询一次（当前仓库）
uv run --project ~/keda iar daemon --agent auto
```

> 建议用 `tmux`、`screen` 或 `systemd` 保持后台运行。

**用 tmux 保持后台：**

```bash
tmux new -s iar-daemon
cd /path/to/target-repo && uv run --project ~/keda iar daemon
# 按 Ctrl+B 再按 D  detach
```

#### 5. Review Daemon（可选）

如果你希望 PR 创建后持续自动检查并维护 PR 状态：

```bash
cd /path/to/target-repo

# 每 600 秒检查一次 supervising/review Issues
uv run --project ~/keda iar review-daemon --interval 600
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

# 3. 在目标仓库初始化并同步 labels（首次）
cd /path/to/target-repo
uv run --project /path/to/keda iar init
uv run --project /path/to/keda iar labels sync

# 4. 创建 Issue
uv run --project /path/to/keda iar issue-from-prd tasks/pending/xxx.md --agent codex --publish-prd --ready

# 5. 启动 daemon 自动执行
uv run --project /path/to/keda iar daemon
```

### 运行前检查清单

| 检查项 | 命令 |
|--------|------|
| GitHub CLI 已登录？ | `gh auth status` |
| `codex` 可用？ | `codex --version` |
| `claude` 可用？ | `claude --version` |
| `kimi` 可用？ | `kimi --help` |
| API Key 已设置？ | `echo $OPENAI_API_KEY` / `echo $ANTHROPIC_API_KEY` |
| 目标仓库路径正确？ | `ls /path/to/target-repo/.git` |

## 配置

Agent Runner 的默认配置来自 keda 的 `config.toml`，目标仓库细节优先来自目标仓库 `.iar.toml`。两者使用相同的 `[agent_runner.*]` section shape；通常把通用默认值放在 `config.toml`，把仓库特定的 `git`、`runner`、`labels` 等覆盖项放在 `.iar.toml`。

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
kimi = "agent/kimi"

[agent_runner.git]
remote = "origin"
base_branch = "main"

[agent_runner.worktree]
create_command = "just worktree issue-{issue_number} enter_shell=false"
reuse_command = "bash -c 'test -d \"$(dirname \"$(git rev-parse --show-toplevel)\")/$(basename \"$(git rev-parse --show-toplevel)\")-worktrees/tasks/issue-{issue_number}\"'"
path_command = "bash -c 'echo \"$(dirname \"$(git rev-parse --show-toplevel)\")/$(basename \"$(git rev-parse --show-toplevel)\")-worktrees/tasks/issue-{issue_number}\"'"

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
timeout_seconds = 900

[agent_runner.post_pr_supervisor]
enabled = true
supervisor_agent = "auto"
max_repair_attempts = 2
```

配置优先级：目标仓库 `.iar.toml` 覆盖项 > 环境变量 > `config.toml` 全局默认值 > 代码默认值。目标仓库覆盖项只影响对应 repository context，不会改变 keda 全局设置。

Prompt 模板支持以下变量占位符：

| 变量 | 说明 |
|---|---|
| `{issue_number}` | GitHub Issue 编号 |
| `{issue_title}` | GitHub Issue 标题 |
| `{issue_url}` | GitHub Issue URL |
| `{worktree_path}` | 当前 worktree 的绝对路径 |
| `{issue_body}` | Issue 完整正文 |
| `{prd_line}` | 自动生成的 PRD 引用行（有 PRD 时提示读取，无 PRD 时给出通用建议） |

## Generated Content 配置

`[agent_runner.generated_content]` 是面向人类阅读的 GitHub Issue 和 PR 内容生成配置。它与 `[agent_runner.prompts]`（实现 Agent 的任务提示词）是独立入口，不要新增 `[agent_runner.content_generation]`。

### 两种生成模式

- `mode = "template"`：用 `.format()` 渲染配置的 `title_template` 和 `body_template`。
- `mode = "agent"`：用 `.format()` 渲染配置的 `prompt`，调用本地只读 agent，解析输出。

无论哪种模式，只要输出不合法或生成失败，都会回退到现有确定性模板。

### Issue 生成变量

| 变量 | 说明 |
|---|---|
| `{issue_type}` | Issue 类型（feature / bug / refactor） |
| `{title}` | 自动构造的 Issue 标题 |
| `{prd_title}` | PRD 文档标题 |
| `{relative_prd_path}` | PRD 相对于仓库根目录的路径 |
| `{acceptance_items}` | Acceptance Checklist 项目 |
| `{prd_text}` | PRD 完整正文 |
| `{prd_introduction}` | PRD Introduction 段落 |
| `{prd_goals}` | PRD Goals 段落 |
| `{prd_requirement_shape}` | PRD Requirement Shape 段落 |
| `{prd_change_impact_tree}` | PRD Change Impact Tree 段落 |

### PR 生成变量

| 变量 | 说明 |
|---|---|
| `{issue_number}` | GitHub Issue 编号 |
| `{issue_title}` | GitHub Issue 标题 |
| `{issue_body}` | Issue 完整正文 |
| `{branch}` | 当前分支名 |
| `{base_branch}` | 配置的基础分支名 |
| `{commit_log}` | branch 相对 base 的 commit message 列表 |
| `{commit_messages}` | `{commit_log}` 的兼容别名 |
| `{diff_stat}` | branch 相对 base 的 diff stat |
| `{git_diff_stat}` | `{diff_stat}` 的兼容别名 |

### 必需锚点

- Issue body 必须包含精确行：`- PRD path: \`<relative_prd_path>\``
- PR body 必须包含：`Closes #<issue_number>`

### 安全边界

- AI 内容生成是只读行为，不修改仓库文件
- 生成后若工作区变脏，视为生成失败并回退
- 生成失败不会阻断 Issue/PR 创建

### 配置示例

```toml
[agent_runner.generated_content]
enabled = false
fallback = "template"
max_input_chars = 20000
default_agent = "auto"

[agent_runner.generated_content.issue_from_prd]
enabled = false
mode = "template"
output = "json"
title_template = "{prd_title}"
body_template = [
  "## Summary",
  "",
  "{prd_introduction}",
  "",
  "## Canonical PRD",
  "",
  "- PRD path: `{relative_prd_path}`",
  "",
  "## Acceptance Summary",
  "",
  "{acceptance_items}",
]
agent = "auto"
timeout_seconds = 60
prompt = [
  "Generate a readable GitHub Issue from this PRD.",
  "Return strict JSON with keys: title, body.",
]

[agent_runner.generated_content.draft_pr]
enabled = false
mode = "template"
output = "markdown"
include_commit_log = true
include_diff_stat = true
title_template = "[Agent] {issue_title}"
body_template = [
  "Closes #{issue_number}",
  "",
  "Generated by issue-agent-runner.",
]
agent = "auto"
timeout_seconds = 60
```

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
- `commit-request.json` 必须提供 `commit_message`；pre-push reviewer 可额外提供 `verdict`、`summary` 和 `findings_*` 元数据作为空提交兜底。runner 会校验当前 branch 未变化、删除请求文件、检查 `forbidden_path_patterns`，再执行 `git add -A` 和 `git commit`
- 不同仓库应在 `verification_commands` 中配置自己的验证命令，例如 `just test`、`npm test`、`pnpm lint` 或 `make test`
- runner 会在提交前先运行一次 `verification_commands`；发现未提交变更并执行 `git add -A` 后，会再次运行同一组验证命令，覆盖依赖 staged 状态的 commit hook 或测试标记
- 如果验证过程中的 formatter 或 lint 自动修复了已跟踪文件，runner 会在安全路径校验后用 `git add -u` 同步这些 tracked 修改，避免 `.last_tested_commit` 指向 working tree 而 commit hook 检查到过期 staged tree
- Agent CLI 非零退出或任一验证失败时，runner 最多按 `max_recovery_attempts` 重新调用同一个 Agent；每次 recovery 前会等待 `recovery_retry_delay_seconds` 秒，并把失败摘要或失败命令的 exit code、stdout、stderr 放入 recovery prompt；Agent 修复后仍只能写 commit request，不能直接提交
- Runner 通过 `classify_failure` 对每次尝试进行分层失败识别，覆盖 `UNCOMMITTED_CHANGES`、`NO_COMMITS`、`VERIFICATION_FAILED`、`AGENT_ERROR`、`UNRECOVERABLE` 等类型；不可恢复错误（如安全路径拦截）会立即终止 retry loop
- 每轮尝试的结果都会记录在 `AttemptResult` 中，最终 Issue comment 包含「Attempt History」表格，展示 attempt_number、failure_type、recovered 状态，便于人工 review 时追踪 Agent 的修复轨迹
- 如果 Agent 没有产生任何新 commit 且工作区也没有未提交变更，runner 仍会将 Issue 标记为 `agent/failed`
- Pre-push reviewer 的修改同样必须通过 `verification_commands` 才能发布
- Post-PR supervisor 的 rebase 操作使用 `--force-with-lease` 且仅作用于 PR branch，不会推送 base branch
- 自动化 rebase 前会校验 HEAD 和 branch 名称，发现不匹配时中止，防止误操作
- rebase 遇到冲突时，runner 会调用 agent 进入有限次数的冲突解决循环（复用 `max_repair_attempts`）；agent 修改冲突文件并通过 commit proxy 提交后，runner 重新尝试 `git rebase --continue`；耗尽后安全 abort 并转人工

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

## deliberate 多 Agent 合议

`iar deliberate` 启动一次只读的多 Agent 合议会话，适合在编码前对复杂需求做多视角推演。

### 基本用法

```bash
# 使用默认 3 个 agent（architect、skeptic、implementer）合议 2 轮
uv run iar deliberate "实现一个用户认证系统"

# 指定参与 agent 和轮数
uv run iar deliberate "优化数据库查询性能" \
  --agents architect,implementer \
  --rounds 3 \
  --synthesizer claude

# 指定输出目录和 session ID（便于复现或测试）
uv run iar deliberate "设计缓存策略" \
  --output /tmp/deliberations \
  --session-id cache-strategy-001
```

### 输出文件

每次会话默认写入 `logs/agent-runner/deliberations/<session_id>/`：

- `events.jsonl`：机器可读事件流，每行一个 JSON 对象
- `transcript.md`：按轮次和 agent 分组的人类可读讨论记录
- `result.md`：最终结论（Recommendation、Consensus、Disagreements、Risks、Next Actions）
- `session.json`：会话元数据、profile 配置、命令参数
- `workspaces/<profile_id>/round-<n>-output.md`：单个参与 agent 在对应轮次的原始输出
- `workspaces/synthesizer/synthesis-output.md`：synthesizer 的原始结构化输出

### 实时输出文件

每个 agent 的 workspace 输出文件在子进程运行期间实时增长，而不是等进程结束后一次性写入：

- `workspaces/<profile_id>/round-<n>-output.md` 在对应 agent 子进程启动前或启动时创建
- 每个可读输出 chunk 到达时立即追加写入对应文件
- agent 失败时保留 partial output 文件，便于排查

### 终端实时输出

合议过程中终端会实时显示结构化事件：

```
[session-id] round=1 agent=architect event=agent_started
[session-id] round=1 agent=skeptic event=agent_started
[session-id] round=1 agent=architect event=agent_finished
[session-id] round=1 agent=skeptic event=agent_finished
[session-id] round=0 agent=synthesizer event=agent_started
[session-id] round=0 agent=synthesizer event=agent_finished
```

#### 交互式 TTY Live 视图

在交互式终端（TTY）中运行时，`iar deliberate` 会显示实时 live view，并**按终端宽度自适应版式**：

- **宽终端并排分栏**：当 `终端宽度 / 当前并发 agent 数 ≥ 40 列` 时，每个 agent 占一栏并排显示，栏数等于当前并发运行的 agent 数量（默认 `architect`、`skeptic`、`implementer` 为三栏）。
- **窄终端竖向堆叠**：当每栏宽度不足以容纳可读文本时，自动改为整宽面板上下堆叠，避免文字被压得过窄。
- **面板内容**：每个面板显示 round、agent、provider、状态，底部标注对应 workspace 目录，并展示最近输出。
- **文字不截断**：正文在面板内换行，绝不横向截断；面板只保留最近若干行（按终端高度裁剪），完整内容以 workspace 文件为准。
- **实时刷新**：输出到达时自动刷新；换轮时上一轮面板会冻结进终端滚动历史，再切换到新一轮。
- **实时推理/工具日志**：部分 provider（如 `codex`）会把 banner、推理过程和工具调用日志写到 stderr。这些内容会被捕获并实时显示在对应面板里作为进度，但**仅用于展示**——不会写入 `round-<n>-output.md`，也不会进入 transcript。落盘和 transcript 只保留 provider 在 stdout 上的可读最终输出。这样既能在运行中看到 agent 在做什么，又能保持 workspace 文件干净。

宽终端并排示例：

```
╭─ round=1 agent=architect provider=claude running ─╮ ╭─ round=1 agent=skeptic provider=kimi running ─╮ ╭─ round=1 agent=implementer provider=codex run─╮
│ 架构师：这是我的可读输出样例。              │ │ 质疑者：这是我的可读输出样例。            │ │ implementer：这是我的可读输出样例。       │
│ ...                                               │ │ ...                                         │ │ ...                                           │
╰────────── workspaces/architect/ ─────────────────╯ ╰───────── workspaces/skeptic/ ────────────────╯ ╰──────── workspaces/implementer/ ─────────────╯
```

窄终端竖向堆叠示例：

```
╭─ round=1 agent=architect provider=claude running ──────────────╮
│ 架构师：这是我的可读输出样例，正文在面板内换行，不会被横向截断。 │
╰──────────────────────── workspaces/architect/ ─────────────────╯
╭─ round=1 agent=skeptic provider=kimi running ──────────────────╮
│ 质疑者：这是我的可读输出样例。                                  │
╰───────────────────────── workspaces/skeptic/ ──────────────────╯
╭─ round=1 agent=implementer provider=codex running ─────────────╮
│ implementer：这是我的可读输出样例。                            │
╰─────────────────────── workspaces/implementer/ ────────────────╯
```

#### 非 TTY / CI / Plain 模式

在非交互式终端、CI 环境、重定向输出或显式 plain 模式下，退回带前缀的普通文本：

```
[round=1 agent=architect status=running] 架构师：这是我的可读输出样例。
[round=1 agent=skeptic status=running] 质疑者：这是我的可读输出样例。
[round=1 agent=implementer status=running] implementer：这是我的可读输出样例。

[round=0 agent=synthesizer status=running] ## 综合建议
[round=0 agent=synthesizer status=running] ...
```

### 验证命令

验证默认三 agent 输出文件：

```bash
# 运行合议
uv run iar deliberate "test prompt" --rounds 1 --session-id test-001

# 检查输出文件
ls logs/agent-runner/deliberations/test-001/workspaces/
# 应显示：architect  implementer  skeptic  synthesizer

# 查看各 agent 输出
cat logs/agent-runner/deliberations/test-001/workspaces/architect/round-1-output.md
cat logs/agent-runner/deliberations/test-001/workspaces/skeptic/round-1-output.md
cat logs/agent-runner/deliberations/test-001/workspaces/implementer/round-1-output.md
cat logs/agent-runner/deliberations/test-001/workspaces/synthesizer/synthesis-output.md
```

若任一参与 agent 或 synthesizer 子进程返回非 0 退出码，`iar deliberate` 会整体失败并返回非 0 退出码；已发生的事件和 partial output 仍保留在对应文件中便于排查。

### 安全边界

- `iar deliberate` 不执行 `git add`、`git commit`、`git push`、`gh issue` 或 `gh pr`
- 每个 agent 在 `logs/agent-runner/deliberations/<session_id>/workspaces/<profile_id>/` 下获得独立工作目录
- 每次 agent 运行前后检查目标仓库 `git status --porcelain`；若发生变化，会话失败并写入 error event
- 合议 prompt 明确禁止文件修改、提交、推送、创建 PR 和触碰真实业务数据
- 本功能不展示模型隐藏 chain-of-thought；所谓“全过程”指可审计的公开回复、工具调用摘要、状态事件和报告

### 配置

在 `config.toml` 的 `[agent_runner.deliberation]` 段配置默认值和自定义 profile：

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
```

## 架构说明

Agent Runner 的代码分布在四层架构中：

- `core/shared/models/agent_runner.py` / `agent_deliberation.py` — 领域模型（frozen dataclasses）
- `core/shared/interfaces/agent_runner.py` — 抽象端口（`IGitHubClient`、`IProcessRunner`、`IAgentTranscriptRunner`）
- `core/shared/interfaces/agent_output_view.py` — 终端输出视图抽象端口（`IAgentOutputView`）
- `core/use_cases/` — 业务用例（`sync_labels`、`create_issue_from_prd`、`run_agent_once`、`run_agent_repositories_once`、`run_agent_daemon`、`agent_review`、`pr_supervisor`、`review_once`、`review_daemon`、`run_agent_deliberation`）
- `engines/agent_runner/factory.py` — 基础设施适配层（实例化实现并注入用例）
- `engines/agent_runner/live_terminal.py` — 终端 live view 适配器（Rich 自适应分栏/竖向堆叠 / plain fallback）
- `infrastructure/github_client.py` / `infrastructure/process_runner.py` — 外部系统实现
- `api/cli.py` — CLI 入口
- `api/routes/agent_runner.py` — FastAPI 只读路由

## 运行日志

`iar` 命令的运行日志按日期存放在 `logs/` 目录下，文件名格式为 `app-YYYY-MM-DD.log`：

```bash
# 查看今天的日志
cat logs/app-$(date +%Y-%m-%d).log

# 实时查看日志
tail -f logs/app-$(date +%Y-%m-%d).log

# 查看最近 7 天的日志
ls -la logs/app-*.log | head -7
```

### 日志特性

- **按日期命名**：每天生成一个独立的日志文件，便于按日期排查问题
- **14 天保留期**：自动清理超过 14 天的旧日志文件
- **时间戳格式**：日志条目使用 `YYYY-MM-DD HH:MM:SS` 格式
- **终端同步**：终端输出同时带有 `HH:MM:SS` 时间戳前缀，便于实时观察

### 日志内容

日志文件包含以下内容：

- CLI 启动和配置加载事件
- Agent 工具调用摘要（如 `[agent tool] Read: /path/to/file.py`）
- Agent 返回结果摘要（如 `[agent result] Task completed`）
- Agent 错误信息（如 `[agent error] API Error: 400`）
- Agent 输出文本（按消息边界汇总）
- 子进程输出（非 Claude agent 如 Codex/Kimi 的输出）
