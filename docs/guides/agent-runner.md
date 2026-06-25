# Agent Runner 使用指南

`iar`（issue-agent-runner）是一个将 GitHub Issues 转为本地 AI Agent 队列的 CLI 工具，已按照本仓库的四层架构迁移并集成。

CLI 入口基于 Typer/Rich：`iar --help` 会展示分组命令、参数和别名；脚本可继续使用历史命令，日常人工操作优先使用更短的别名。

## 功能概述

- **init**：在目标 Git 仓库创建仓库本地 `.iar.toml` 配置
- **labels sync**：在目标仓库创建或更新标准 labels（`agent/ready`、`agent/running`、`agent/supervising` 等）
- **issue create**：从一个或多个 PRD Markdown 文件创建 GitHub Issue，默认在 ready 前发布 PRD（可用 `--no-publish-prd` 关闭，兼容旧命令 `issue-from-prd`）
- **run**：单次轮询 `agent/ready` 的 Issues，claim 后执行 AI Agent，验证、push、pre-PR review、创建 draft PR、进入 `agent/supervising` 并运行 post-PR supervisor（兼容旧命令 `run-once`）
- **review**：单次检查 `agent/supervising` 和 `agent/review` 的 Issues，基于 PR 上下文变化运行 supervisor cycle（兼容旧命令 `review-once`）
- **review-daemon**：常驻进程，按指定间隔循环执行 `review-once`
- **daemon**：常驻进程，按指定间隔循环执行 `run-once`
- **ask**：受限自然语言决策入口，默认只生成计划，确认后执行白名单动作
- **worktree cleanup**：清理 GitHub Issue 已关闭、远端分支已删除但本地仍残留的 `issue-<number>` 分支和 iAR worktree

## 安装

`iar` 已通过 `pyproject.toml` 的 `[project.scripts]` 注册：

```bash
# 通过 uv 运行
uv run iar --help

# 或安装后直接使用
iar --help
```

### 初始化门禁

除 `iar init` 外，所有 `iar` 子命令在执行前都会检查目标仓库是否已完成初始化（仓库根目录存在有效的 `.iar.toml`）。未初始化时命令会立即以非零退出码失败，并提示先运行 `iar init`：

```bash
# 首次在目标仓库使用前，必须先初始化
iar init

# 之后才能执行其他命令
iar labels sync
iar run --dry-run
```

`iar init` 本身不受门禁限制，包括 `--dry-run` 和 `--force` 形式。本 PRD 不提供 `--skip-init-check` 等绕过开关。

### Shell 自动补全

`iar` 支持生成 shell completion。zsh 用户安装后，输入 `iar is<Tab>` 可补全到 `issue`：

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
        AI 做完 → push → pre-PR review → Draft PR → agent/supervising
               ↓
        supervisor 通过 → 换成 agent/review → 人工审完关闭
               ↓
        出问题 → 换成 agent/failed 或 agent/blocked
```

没有这些标签，`iar` 无法识别哪些 Issue 可以执行、哪些正在执行、哪些需要 review。

### Workflow label 互斥

六个 AI 执行状态标签（`agent/ready`、`agent/running`、`agent/supervising`、`agent/review`、`agent/failed`、`agent/blocked`）是互斥的 durable workflow labels。runner 在任何状态切换时都会清理其他 workflow labels，只保留目标状态。这意味着：

- 不会同时出现 `agent/running` + `agent/review`
- 不会同时出现 `agent/supervising` + `agent/failed`
- 历史脏状态（如同时贴有多个 workflow labels）会在下一次被处理时自动收敛到单一状态

工具路由标签（`agent/codex`、`agent/claude`、`agent/kimi`）、任务组标签（`task-group/*`）等非 workflow labels 不会被清理。

### 14 个标准标签

| 类别 | 标签 | 颜色 | 作用 |
|---|---|---|---|
| **AI 执行状态** | `agent/ready` | 🟢 绿色 | Issue 已准备好，等待 AI runner 认领 |
| | `agent/running` | 🟡 黄色 | 代码正在被修改（首次实现或 PR branch rework） |
| | `agent/supervising` | 🔵 浅蓝 | Draft PR 已创建，自动 post-PR supervisor 正在审查或重新处理 |
| | `agent/review` | 🔵 蓝色 | 自动总控审查已通过，当前 PR 等待人类 review |
| | `agent/failed` | 🔴 红色 | AI runner 执行失败 |
| | `agent/blocked` | ⬛ 黑色 | AI runner 需要人工介入 |
| | `agent/waiting` | 🟠 橙色 | Issue 依赖未满足，等待上游 closure |
| **工具路由** | `agent/codex` | 🟣 紫色 | 指定使用 Codex 执行 |
| | `agent/claude` | 🩵 浅蓝 | 指定使用 Claude Code 执行 |
| | `agent/kimi` | 🩷 粉色 | 指定使用 Kimi 执行 |
| **来源标识** | `source/prd` | 🔵 深蓝 | Issue 关联了仓库内的 PRD 文件 |
| **异步讨论** | `agent/deliberate` | 🩶 浅灰 | 复杂需求先走 Issue 评论区异步讨论，收敛后换 `agent/rework-prd` 落地 PRD |
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

## Issue 依赖门禁（Dependency Gate）

当多个 Issue 之间存在先后依赖关系时（例如 B 组必须等 A 组全部合并），可以使用依赖门禁实现自动调度，无需人工盯着上游 PR 合并后再给下游 Issue 打 label。

### 基本原理

依赖门禁采用 **PRD 结构化依赖声明 + IAR materialized marker/label** 的无状态方案：

- PRD 中包含工具无关的 `Delivery Dependencies` 小节
- `iar issue create` 将 `Gate type: hard` 的依赖物化为 Issue body 中的 `<!-- iar:depends-on ... -->` marker
- runner 每次轮询时实时查询 GitHub 状态：依赖未满足时跳过领取、叠加 `agent/waiting` label 并写等待 comment
- 依赖未满足的 ready Issue 不消耗 `max_issues` 的处理额度；runner 会继续扫描后续 ready Issue，直到找到可领取任务或扫描窗口耗尽
- 上游 Issue 全部 closed 后，下一轮轮询自动移除 `agent/waiting` 并正常领取

### PRD 中的 Delivery Dependencies 语法

在 PRD 中添加如下小节：

```markdown
## Delivery Dependencies

- Group: my-group
- Depends on groups: upstream-group-a, upstream-group-b
- Depends on tasks/issues: #42, tasks/pending/P2-FEAT-20260527-190923-prd-from-issue.md
- Gate type: hard
- Notes: 等待上游 API 改造完成
```

字段说明：

| 字段 | 说明 |
|---|---|
| `Group` | 本 Issue 所属的任务组；当其他 PRD 引用本 PRD 且本 PRD 尚未关联 Issue 时，可作为 fallback 物化为 `group:<name>` 依赖 marker |
| `Depends on groups` | 上游任务组，该组下全部 Issue closed 后才满足 |
| `Depends on tasks/issues` | 上游 Issue 编号或 PRD 引用；Issue 支持 `#N` / `N`，PRD 支持 repo-relative 路径或 `tasks/` 下唯一文件名/文件 stem，多个用逗号/分号或 Markdown 子列表分隔；引用后可以追加说明文字，解析器只提取引用 token |
| `Gate type` | `hard` = 阻塞门禁；`soft` = 仅文档信息，不阻塞；`none` = 不生成依赖 marker |
| `Notes` | 自由备注 |

PRD 引用只在 `iar issue create` 发布时解析，不会原样写入 Issue body。解析规则：

- 引用 PRD 已包含 `- GitHub Issue: .../issues/N` 时，物化为 `#N` 依赖 marker。
- 引用 PRD 没有 Issue link 但包含 `Delivery Dependencies` 的 `Group` 时，物化为 `group:<group>` 依赖 marker。
- 引用无法唯一匹配、引用 PRD 既没有 Issue link 也没有 `Group` 时，命令 fail fast，并提示改成 Issue 编号、repo-relative PRD 路径，或先补上游 PRD 的 Issue link / Group。
- 无依赖时可以留空或写 `none`。

### CLI 参数覆盖

即使 PRD 中没有写依赖，也可以在 `iar issue create` 时显式指定：

```bash
# 声明依赖上游 Issue #42 和 #43
iar issue create tasks/pending/foo.md --depends-on 42 --depends-on 43

# 声明依赖上游组 upstream-a
iar issue create tasks/pending/foo.md --depends-on-group upstream-a
```

CLI 参数与 PRD 声明会**合并去重**，CLI 参数优先级最高。

### runner 行为

- 无 `iar:depends-on` marker 的 Issue：行为与改动前完全一致（零破坏）
- 依赖未满足：`agent/ready` 保持，叠加 `agent/waiting`，写 comment 说明阻塞原因
- 依赖满足：自动移除 `agent/waiting`（若有），正常进入领取流程
- 上游出现 `agent/failed` 或 `agent/blocked`：等待 comment 中会点名该上游 Issue，提示 operator 干预
- 空 group（无任何成员）：判定为不满足，comment 中提示疑似拼写错误
- 连续多轮 blockers 不变时只有一条 comment（按 blockers 集合去重）
- `--dry-run` 模式下打印等待原因但不写任何 GitHub 状态

### 状态流转补充

```text
agent/ready + 依赖未满足 → agent/waiting（跳过领取）
agent/ready + 依赖满足   → 移除 waiting（正常领取）
agent/waiting              → 上游 closed → 自动放行
```

依赖判定完全无状态：每次轮询现查现算，依据 GitHub 实时状态，不引入本地缓存。

## 中断恢复（worktree rebase 卡死）

runner 在 rebase 中途崩溃或被中断（例如 post-PR supervisor 正在把 PR 分支
rebase 到最新 base 时进程退出），会把 Issue 的 worktree 留在 **detached HEAD +
未完成 rebase** 的状态：分支 ref 仍指向原 tip，但 worktree HEAD 停在 base 上。

- **自动重新认领**：下一轮轮询发现仍是 `agent/running` 的 Issue 时，除了「有可发布
  的干净本地 commit」外，也会检测 worktree 是否处于 mid-rebase / detached 状态。命中
  即进入恢复路径，由 `_ensure_worktree_branch` 治愈：优先 `git rebase --continue`；
  有冲突则让 agent 解决并续跑；超过配置的修复次数才回退 `git rebase --abort` 并重挂
  分支。治愈后照常复用已有 commit 完成发布。
- **并发互斥**：恢复路径在动任何 git 之前先获取 **per-worktree 原子认领锁**
  （`.agent-runner/blocked-claim.lock`，与 blocked 恢复共用同一把锁），确保不会有两个
  runner 同时 rebase/发布同一个 worktree。抢锁失败的 runner 记一条日志后跳过本轮。
- **死进程接管**：锁文件记录持有者 PID；若持有者已退出，下一个 runner 会自动夺锁，
  从而接管被中断的工作。

> 局限：认领锁基于 PID 存活判断，对「进程仍在但已放弃该任务」的情形无法自动接管；
> 此类需要 operator 介入或后续引入心跳/时间戳过期机制。

## 复杂需求：异步 Issue 评论讨论（`agent/deliberate`）

`agent/rework-prd` 是一次性读 Issue 体 + 全部评论直接生成 PRD 的全自动管道，**适合简单需求**。
对需要来回澄清才能定清楚的复杂 Issue（"先讨论清楚再写 PRD"），请改用 Issue 评论区异步多轮讨论：

1. 创建 Issue 时打上 **`agent/deliberate`** 标签。系统不自动分诊，由人显式声明"这是需要讨论的复杂需求"。
2. `iar run --once` / `iar daemon` 在 Phase 0 发现带 `agent/deliberate` 的开放 Issue，会复用 `run_agent_deliberation` 引擎（后台 NoOp 视图，不弹 TTY）跑多角色内部互辩，并把 synthesizer 的输出贴成一条结构化"澄清问题清单"评论，类别固定为 5 个：
   `## 范围边界` / `## 约束` / `## 验收标准` / `## 技术选型` / `## 风险`。
3. 每条 AI 评论尾部追加一个隐藏的 `<!-- iar:event version=1 phase=deliberation_question_posted cycle=N issue_comments_count=K -->` marker；后续轮询靠它判断"轮到谁"。
4. 人直接在 GitHub 上回复评论补充信息。下一次轮询发现 `当前评论数 > marker.issue_comments_count`，轮到 AI 续问，`cycle` 递增。
5. 连续 `stale_rounds_before_hint`（默认 3）轮 AI 提问但用户回复信息量很少时，问题清单评论末尾会自动追加"讨论接近完成，可将标签改为 `agent/rework-prd` 落地"的软提示。
6. 讨论清楚后，**人手动**把 `agent/deliberate` 换成 `agent/rework-prd`。Phase 1 接管，把 Issue 体 + 完整讨论评论生成为 PRD 并 push draft PR。
7. 收敛完全由人换标签触发——AI 不自动判定信息够了、不自动改标签、不自动产 PRD。

> 失败隔离：单个 Issue 跑合议失败时打 `agent/failed` 标签 + 失败说明评论，不污染其它 Issue 也不中断 Phase 1/Phase 2。
> 成本：`max_deliberation_issues`（默认 1）+ `[agent_runner.deliberation].default_rounds`（默认 2）共同决定每轮合议 agent×rounds 次调用；高复杂度 Issue 临时调小 rounds 可以节能。

### 实现阶段 prompt 内联 PRD 全文

不论走的是 `agent/rework-prd` 还是 `agent/deliberate`，最终都会进入 Phase 2 写代码。
原本实现/恢复/续作 prompt 里只有一行"读 PRD 路径"的指针；现在
`agent_runner_feedback._build_prd_context_block` 会**把 worktree 内 PRD 文件正文直接内联进
prompt**（带长度上限，缺省 20 000 字符）。超过上限会尾部截断并附"完整 PRD 见 `<path>`"提示。

内联实现的好处：

- 实现 agent 冷启动时不再需要先 `cat` 整个 PRD，节省一轮文件 I/O 与上下文切换。
- PRD/讨论沉淀的上下文一步到位地到达写代码阶段，避免"PRD 全在 Issue 评论里、实现阶段只剩几行指针"的丢失。
- 内联受长度上限保护，且 PRD 文件缺失时优雅回退到原有指针文案。

## 仓库本地配置

`iar` 默认以当前 Git 仓库作为目标仓库。**首次在目标仓库使用前必须先执行 `iar init`**，否则除 `iar init` 外的所有命令都会失败并提示初始化。

```bash
cd /path/to/target-repo
uv run --project /path/to/keda iar init --dry-run
uv run --project /path/to/keda iar init
```

`iar init --dry-run` 只打印将要写入的内容，不创建文件。`.iar.toml` 已存在时，`iar init` 会拒绝覆盖；确认需要重建时显式传入 `--force`。

`iar init` 成功写入本地配置后，还会自动把当前仓库注册（或更新路径）到全局 `config.toml` 的 `[agent_runner.repositories]` 中，使 `iar daemon` 默认即可在当前仓库启动。如果该 `repo_id` 已在 registry 中但指向不同路径，init 会自动更新 registry 路径到当前位置。

### `verification_commands` 自动探测

`iar init` 不会写死验证命令，而是按目标仓库实际情况探测 `[agent_runner.runner].verification_commands`（实现见 `src/backend/engines/agent_runner/repository_local.py` 的 `detect_verification_commands`）：

- 基线始终包含 `git diff --check`；
- 声明了 `mkdocs` 依赖且存在 `mkdocs.yml` → 追加 `uv run mkdocs build`；
- 存在 `just test` 配方（含经 `import 'justfile.shared'` 等导入的配方，以及 `@test` quiet 前缀）→ 追加 `just test`，并**优先选它**：`just test` 跑的就是 `git commit` 时 pre-commit 强制的同一组 lint/format/test 钩子，并刷新 `.last_tested_commit` 标记，因此 runner 验证通过后提交不会再被 pre-commit 挡下；
- 否则走通用回退：声明了 `pre-commit` 依赖且有 `.pre-commit-config.yaml` → 追加 `uv run pre-commit run --all-files`；声明了 `pytest` 且有 `tests/` → 追加 `uv run pytest -q`。

> **check-test-flag 护栏**：若仓库装了 `check-test-flag` 钩子却没有可探测的 `just test` 配方，`iar init` 会**跳过** `pre-commit run --all-files` 并打印告警——该钩子只认 `just test` 写入的 `.last_tested_commit` 标记，bare `pre-commit run` 会触发它并让 runner 代提交死锁。这类仓库应补一个 `just test` 配方，或移除 check-test-flag。
>
> 探测只在 `iar init` 时发生：已存在的 `.iar.toml` 不会自动更新，需重跑 `iar init --force` 或手改 `verification_commands` 才会采用新探测结果。

生成示例：

```toml
# IAR 本地仓库配置
# 本文件只覆盖当前仓库特有的配置；未指定的字段继承 config.toml / 环境变量的全局默认值。
# 修改后无需重启 daemon，下一次轮询自动生效。
# 完整字段说明见 docs/guides/agent-runner.md。

# 仓库身份标识（用于多仓库管理时区分不同仓库）
[agent_runner.repository]
# 仓库在 IAR 中的唯一标识，通常与远程仓库名一致
id = "target-repo"
# 是否允许 runner 处理该仓库的 Issue
enabled = true
# 管理终端 / 日志中显示的友好名称
display_name = "target-repo"

# Git 发布配置：推送 remote、目标基础分支 base_branch
[agent_runner.git]
# 推送分支和创建 PR 时使用的 Git remote 名称
remote = "origin"
# 创建 worktree 与 PR 的目标基础分支
base_branch = "main"

# Issue worktree 的创建与定位命令；默认使用 iar worktree，通常无需修改
[agent_runner.worktree]
# 创建新 worktree 的命令；{issue_number} 和 {base_branch} 会被替换
create_command = "iar worktree create --branch issue-{issue_number} --base-branch {base_branch}"
# 复用已有 worktree 时定位路径的命令
reuse_command = "iar worktree path --branch issue-{issue_number}"
# 获取 worktree 绝对路径的命令
path_command = "iar worktree path --branch issue-{issue_number}"

# Runner 行为配置：每轮处理 Issue 数量、默认 agent、提交前验证命令
[agent_runner.runner]
# 每次轮询每个仓库最多处理多少个 Issue
max_issues = 1
# 单轮内并行处理的 Issue 数量：1 为串行（默认）；>1 时同一轮并行跑多个 Issue。
# 仅 `iar daemon --concurrency` 未指定时作为默认值。
max_concurrent_issues = 1
# 默认使用的 AI agent：auto / claude / codex / kimi
default_agent = "auto"
# Agent 失败后的最大重试次数
max_recovery_attempts = 5
# 每次重试前等待的秒数
recovery_retry_delay_seconds = 30
# 跨 agent fallback 链：主 agent 失败后依次尝试本机可用 agent。
# 某 agent 反复修不好或供应商受限时切到下一个；命令不存在则自动跳过。
# 设为空列表可关闭跨 agent 切换，回退到单 agent 行为。
agent_fallback_order = ["claude", "kimi", "codex"]
# 最多切换 agent 的次数（order=[a,b,c] 且 max_agent_switches=2 时最多尝试 3 个 agent）
max_agent_switches = 2
# 瞬时网络错误（socket 断开 / 5xx / 超时）的就地重试次数与退避秒数
transient_retry_attempts = 2
transient_retry_delay_seconds = 10
# 单次 agent 执行的 wall-clock 超时（秒）；超时会 kill 子进程并进入 recovery
timeout_seconds = 14400
# 无输出超时（秒）：agent 子进程在指定时间内没有 stdout/stderr 输出时被 kill
inactivity_timeout_seconds = 1200
# 提交前自动运行的验证命令；任一命令失败会进入 recovery
verification_commands = [
    "git diff --check",
]

### Agent 执行超时

`[agent_runner.runner]` 提供两类超时，防止 agent 子进程永久挂起：

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `timeout_seconds` | `14400`（4 小时） | 单次 agent 执行的 wall-clock 上限。超过后 runner 会 kill 子进程，并将本次尝试记录为可恢复的 `AGENT_ERROR`，随后进入 recovery 流程。 |
| `inactivity_timeout_seconds` | `1200`（20 分钟） | 无输出上限。只要 agent 子进程持续产生 stdout/stderr 数据，时钟就会重置；如果超过 20 分钟没有任何输出，runner 认为进程已卡死并 kill。 |

两类超时独立生效，满足任意一个都会终止子进程。它们同时作用于首次实现和每次 recovery attempt。如果某个任务确实需要更长时间，可以在目标仓库的 `.iar.toml` 或全局 `config.toml` 中调大对应值；如果某类任务经常静默运行（例如大型编译），可适当提高 `inactivity_timeout_seconds`。

超时后的日志示例：

```text
Claude stream (Issue #19: ...) timed out after 14400s; terminating: claude ...
Claude stream (Issue #19: ...) inactive for 1200s; terminating: claude ...
Agent command failed for Issue #19; asking agent to recover (1/5).
```

### 非 claude agent 的实时输出（PTY）

`claude` 用 `--output-format stream-json` 显式吐增量事件，所以一直能实时看到进度。`kimi` / `codex` 没有这种流式协议，而且很多 CLI 在发现 stdout 是管道（非终端）时会把输出从行缓冲切成**块缓冲**——结果就是运行中只看到几个点、最后才一次性打印，期间只有 watchdog 的 `still running after Ns` 心跳。

为此 runner 在跑**非 claude** agent 时改用**伪终端（PTY）**：让子进程以为 stdout 是终端，从而恢复行缓冲、实时吐出进度。stdout/stderr 合并到同一 PTY（顺序自然、无双管道死锁），随后接入与 claude 相同的输出通道——并行时进入每个 Issue 各自的面板与日志文件（见下文「并行处理 Issue」）。`still running after Ns` 仍是正常的存活心跳，不是报错。

> PTY 只能让**会输出但被缓冲**的 agent 实时可见；如果某 agent 本就几乎不打印进度，PTY 也变不出内容。

# 发布前安全边界：自动合并开关、禁止提交的路径模式
[agent_runner.safety]
# 是否允许自动合并 PR（强烈建议保持 false）
auto_merge = false
# 提交前禁止变更的路径通配模式
forbidden_path_patterns = [
    ".env",
    ".env.*",
    "secrets/*",
    "docker-compose.prod.yml",
]

# Realistic Validation 证据门禁配置
[agent_runner.validation]
# 是否启用 Realistic Validation 证据门禁
enabled = true
# worktree 内证据目录（默认被 info/exclude 排除，不会进入代码 diff）
evidence_dir = ".iar/evidence"
# orphan 证据分支前缀
branch_prefix = "iar-evidence/"
# 是否逐项检查证据文件格式
evidence_format_check = true
# 是否用 agent 解析 PRD 中的格式要求
parse_evidence_format_with_agent = true

# 实现 Agent 的 prompt 模板；默认 phase 与自定义阶段模板
[agent_runner.prompts]
# 默认使用的 prompt 阶段
default_phase = "execution"

[agent_runner.prompts.phases]

# Draft PR 创建前的 AI review 门禁（push 之后、PR 之前）
[agent_runner.pre_pr_review]
# 是否启用 Draft PR 创建前的 AI review
enabled = true
# 执行 review 的 agent：auto / claude / codex / kimi
review_agent = "auto"
# 是否允许实现 agent 与 reviewer 为同一个
allow_same_agent = true
# review 不通过时的最大修复轮数（默认 2，最后一轮允许 reviewer 提供最终修复 commit request）
max_attempts = 2
# review agent 最长运行秒数
timeout_seconds = 1800
# reviewer 报出 findings 但未写 commit request 时，同一轮内追加提醒的最大次数（默认 1）
commit_request_reminder_attempts = 1
# 自定义 review 提示词；空列表走代码默认模板，默认模板会调用 code-reviewer skill 并要求输出 findings JSON 数组
review_prompt_template = []

# Draft PR 创建后的自动 supervisor 配置
[agent_runner.post_pr_supervisor]
# 是否启用 post-PR supervisor
enabled = true
# 执行 supervisor 的 agent
supervisor_agent = "auto"
# supervisor 要求修复时的最大修复 / rebase 次数
max_repair_attempts = 2
# supervisor agent 进程崩溃（API / 网络等基础设施错误）时同一 cycle 内的最大重试次数
max_agent_crash_retries = 5
# 崩溃重试的初始退避秒数，之后每次重试翻倍
crash_retry_initial_backoff_seconds = 30
# 崩溃重试单次退避等待的最大秒数
crash_retry_max_backoff_seconds = 600

# GitHub Issue / PR 内容生成（面向人类阅读，不影响实现 Agent）
[agent_runner.generated_content]
# 是否启用 AI 生成 Issue / PR 正文
enabled = true
# 生成失败时的回退方式（当前仅支持 template）
fallback = "template"
# 生成 prompt 的最大字符数
max_input_chars = 20000
# 执行生成的默认 agent：auto / claude / codex / kimi
default_agent = "auto"

# 从 PRD 生成 GitHub Issue 的模板
[agent_runner.generated_content.issue_from_prd]
# 是否从 PRD 生成 Issue
enabled = true
# 生成模式：template（模板渲染）或 agent（调用 AI）
mode = "template"
# 输出格式：json / markdown
output = "json"
# Issue 标题模板
title_template = ""
# Issue 正文模板，支持字符串或字符串列表
body_template = ""
# 执行生成的 agent
agent = "auto"
# 生成超时秒数
timeout_seconds = 60
# agent 模式使用的 prompt
prompt = ""
# PR 生成时是否包含 commit log
include_commit_log = true
# PR 生成时是否包含 diff stat
include_diff_stat = true

# 从 commit 信息生成 Draft PR 的模板
[agent_runner.generated_content.draft_pr]
# 是否生成 Draft PR 正文
enabled = true
# 生成模式：template（模板渲染）或 agent（调用 AI）
mode = "template"
# 输出格式：json / markdown
output = "json"
# PR 标题模板
title_template = ""
# PR 正文模板，支持字符串或字符串列表
body_template = ""
# 执行生成的 agent
agent = "auto"
# 生成超时秒数
timeout_seconds = 60
# agent 模式使用的 prompt
prompt = ""
# PR 生成时是否包含 commit log
include_commit_log = true
# PR 生成时是否包含 diff stat
include_diff_stat = true

# 交互式决策（iar ask）配置
[agent_runner.interactive_decision]
# 是否启用 iar ask
enabled = true
# 默认 planner agent
default_agent = "claude"
# 决策日志输出目录
default_output_dir = "logs/agent-runner/decisions"
# 规划 agent 超时秒数
planner_timeout_seconds = 120
# 输入上下文最大字符数
max_context_chars = 24000
# 是否允许 iar ask --yes 跳过确认
allow_execute_yes = true

# 多 agent 审议（iar deliberate）配置
[agent_runner.deliberation]
# 默认审议轮数
default_rounds = 2
# 默认汇总 agent
default_synthesizer = "claude"
# 审议会话输出目录
default_output_dir = "logs/agent-runner/deliberations"

# 审议角色：架构师
[agent_runner.deliberation.profiles.architect]
agent = "claude"
role = "architect"
behavior_prompt = "You are an experienced software architect. Analyze the requirement from a system design perspective. Focus on modularity, scalability, and maintainability."

# 审议角色：质疑者
[agent_runner.deliberation.profiles.skeptic]
agent = "kimi"
role = "skeptic"
behavior_prompt = "You are a skeptical reviewer. Challenge assumptions, identify risks, and point out edge cases. Ask hard questions that others might miss."

# 审议角色：实现者
[agent_runner.deliberation.profiles.implementer]
agent = "codex"
role = "implementer"
behavior_prompt = "You are a pragmatic implementer. Focus on feasibility, concrete steps, and implementation details. Highlight what can be built and what resources are needed."

# GitHub labels 状态流转配置（如你的仓库使用不同标签名，可取消注释并覆盖）
# [agent_runner.labels]
# ready = "agent/ready"
# running = "agent/running"
# supervising = "agent/supervising"
# review = "agent/review"
# failed = "agent/failed"
# blocked = "agent/blocked"
# codex = "agent/codex"
# claude = "agent/claude"
# kimi = "agent/kimi"
```

仓库本地 `.iar.toml` 可覆盖 `git`、`runner`、`labels`、`worktree`、`safety`、`prompts`、`pre_pr_review`、`post_pr_supervisor`、`generated_content`、`interactive_decision` 和 `deliberation`。`config.toml` 继续保存全局默认值、环境级设置和 legacy registry，不应保存 token、API key 或账号凭据。

单仓库命令的目标解析规则：

| 命令形态 | 目标解析 |
|---|---|
| `iar run` / `iar labels sync` / `iar review` / `iar issue create ...` | 当前 Git 仓库，合并当前仓库 `.iar.toml` |
| `iar run --repo /path/to/repo` | 指定 Git 仓库，合并 `/path/to/repo/.iar.toml` |
| `iar --repo /path/to/repo run` | 等价的顶层 selector 写法，适合把目标仓库放在命令前 |
| `iar run --repo-id keda` | 从 legacy registry 找到路径，再合并目标仓库 `.iar.toml` |
| `iar run --all` | 显式处理 `config.toml` 中所有 enabled registry entries |
| `iar daemon` / `iar review-daemon` | 当前已初始化注册仓库；未命中、未初始化或匹配多个时报错 |
| `iar daemon --repo-id keda` | 仅处理指定仓库 |
| `iar daemon --all` | 显式处理 `config.toml` 中所有 enabled registry entries |

历史命令 `iar run-once`、`iar review-once`、`iar issue-from-prd` 和 `iar recover-publish` 已被删除；请改用 `iar run` / `iar review` / `iar issue create` / `iar recover`。

## Workflow Templates（`iar workflow install`）

`iar workflow install <name>` 把 IAR 内嵌的 workflow 模板复制到当前 Git 仓库，并写入最小的 `[preview]` 占位段。当前 v1 只支持 `preview` 工作流，会复制下列 7 个文件（相对仓库根）：

- `.github/workflows/deploy-preview.yml`
- `deploy/vps-traefik/README.md`
- `deploy/vps-traefik/deploy-preview.sh`（保持 `0755` 权限）
- `deploy/vps-traefik/docker-compose.preview.yml`
- `deploy/vps-traefik/preview.env.example`
- `scripts/preview_env.py`
- `scripts/provision_preview_server.py`

典型用法：

```bash
# 在目标仓库里（必须先 `iar init`）：
uv run iar workflow install preview

# 只看将要写哪些路径与字节数，不实际落盘：
uv run iar workflow install preview --dry-run

# 已存在同名文件会被拒绝（非零退出），需要重建时显式加 --force。
uv run iar workflow install preview --force
```

行为约定：

- 目标文件已存在 → 默认拒绝并以非零退出；`--force` 同时覆盖 7 个模板文件和 `config.toml [preview]` 段
- `--dry-run` 全程不写盘，只打印 `would write` / `would overwrite` 清单
- `config.toml` 末尾追加最小 `[preview]` 段；已存在时默认跳过，`--force` 用占位段整体替换
- 占位段字段名直接派生自 `backend.infrastructure.config.settings.PreviewSettings.model_fields`，避免硬编码字段清单；字段值统一为 `<set-me>`（`enabled` 保留 schema 默认值以保证 pydantic-settings 可解析）
- 缺 `.iar.toml` 时拒绝并提示 `iar init`
- 接收 `--repo` / `--repo-id` / `--config` 时拒绝并不落盘（与 `iar init` 行为一致）

模板维护说明：模板文件随 IAR Python 包一起发布，路径在 `src/backend/engines/agent_runner/templates/<name>/`。
源仓库改动（`deploy/`、`scripts/`、`.github/workflows/deploy-preview.yml`）后，需要同步把变更复制到 `templates/preview/`。
`just check-template-drift` 会在 CI 拒绝这两边不一致的情况；`deploy/vps-traefik/README.md` 第 35-45 行的字段表也必须与 `PreviewSettings.model_fields` 对齐，否则 drift 门禁会失败。

## worktree 中的本地 env 文件

`git worktree add` 只会物化被 Git 跟踪的文件，gitignored 的 `.env*`（密钥、本地配置）不会自动出现在新 worktree 里。为此 runner 在 worktree 创建/复用后会自动补齐缺失的 env 文件：

- `iar worktree create` 和 `iar run` 的 create/reuse 流程都会把主仓库目录下的 `.env*` 文件按相对路径复制到 worktree（含子目录，如 `tests/playwright-e2e/.env`）
- 只复制 worktree 中**缺失**的文件：被跟踪的 `.env*.example` 与 worktree 内已修改的 `.env` 永远不会被覆盖
- 复用已有 worktree 时同样补齐（旧 worktree 缺 `.env` 的，下一次 `iar run` 会自动治愈）
- 扫描会跳过 `.git`、`.iar-worktrees`、`.venv`、`node_modules` 等目录，避免把其他 worktree 的 env 文件复制串
- 与旧的 `just worktree` 脚本不同，这里**不会**用 `.env.example` 兜底生成 `.env`：用示例值静默跑测试比明确的缺配置失败更危险
- 复制是 best effort：单个文件失败（如悬空 symlink）只记日志，不会中断 agent run
- 复制过来的文件保持 gitignored 状态，不会让 worktree 变脏，也不影响 `iar worktree cleanup` 的默认清理判定

## worktree 中的前端依赖（node_modules）

同理，gitignored 的 `node_modules` 也不会被 `git worktree add` 物化，否则 worktree 里跑 `vite` 等构建会报 `vite: command not found`。runner 在补齐 env 文件之后，会把前端依赖从主仓库**软链**进 worktree（等价于 `just worktree` 脚本的 `symlink-from-main` 策略）：

- 扫描 worktree（而非主仓库）中所有含 `package.json` 的前端项目，对每个项目把 `node_modules` 软链到主仓库对应目录（含 `frontend/`、`frontend-admin/` 这类子目录）
- 只链 worktree 中**缺失**的 `node_modules`：worktree 内已有的真实目录或软链（例如已 `npm install` 过）永远不会被覆盖
- 复用已有 worktree 时同样补齐：旧 worktree 缺 `node_modules` 的，下一次 `iar run` 会自动治愈
- 采用软链而非重新安装：主仓库已装好依赖，软链是秒级、零安装成本；代价是 worktree 与主仓库共享依赖，分支间 `package.json` 差异较大时可能不准
- 主仓库该项目缺 `node_modules` 时**无法软链**，会记一条 `warning`（而非静默跳过），方便把后续 `vite: command not found` 追溯到"主仓库没装依赖"
- 链接是 best effort：单个项目失败（权限、竞态）只记日志，不会中断 agent run
- 软链同样保持 gitignored 状态，不会让 worktree 变脏

> 守护进程路径（`iar worktree create` / `iar run`）不读 `WORKTREE_FRONTEND_STRATEGY` 环境变量——该变量只对手动 `just worktree`（`scripts/shared/worktree/create.sh`）生效。daemon 路径固定采用上面的软链策略。

## worktree 分支安全与自动修复

`iar run` 在把 agent 放入 worktree 前会执行两项准备：

1. **远程分支对齐**：如果 `refs/remotes/<remote>/issue-<number>` 存在，则 fetch 并仅当本地分支是其祖先、且 worktree 干净时做 fast-forward；dirty、diverged 或本地领先场景会保留本地状态并失败/继续，不会 destructive reset。
2. **分支状态自愈**：如果 worktree 处于 detached HEAD（例如 post-PR supervisor 正在 rebase 或被人工 checkout 到某个 commit），runner 会尝试自动恢复：
   - 处于 active rebase 时：无冲突则 `rebase --continue`；有冲突则调用配置的 AI agent 解决冲突并继续 rebase，和 post-PR supervisor 的冲突解决策略一致。只有 agent 在 `max_repair_attempts` 次尝试后仍无法解决，才会 fallback 到 `rebase --abort` 并 checkout 目标分支。
   - 单纯 detached HEAD：若 `issue-<number>` 分支不存在或当前 HEAD 领先于该分支，则把分支指到当前 HEAD 并 checkout；若已分叉则报错，避免静默丢失历史。

如果恢复失败，runner 会把 Issue 标记为 `failed` 并给出可操作的错误信息；成功则继续正常执行 agent、验证和发布流程。

## 清理 stale issue worktree

当 Issue 对应的 PR 合并并删除远端分支后，本地可能仍保留 `issue-<number>` 分支和 `.iar-worktrees/issue-<number>`。可以用 `iar worktree cleanup` 做一次安全清理：

```bash
# 只预览，不删除
iar worktree cleanup --dry-run

# 真正删除满足条件的本地分支和 iAR worktree
iar worktree cleanup --yes

# 谨慎：允许删除脏 worktree 或未合入远端 base branch 的分支
iar worktree cleanup --yes --force
```

没有传 `--yes` 时，命令会按 dry-run 处理。执行删除前会先 `git fetch <remote> --prune`，然后只清理同时满足以下条件的分支：

- 本地分支名匹配默认 iAR 模式 `issue-<number>`
- GitHub Issue 状态为 `CLOSED`
- `refs/remotes/<remote>/issue-<number>` 已不存在
- worktree 位于当前仓库的 `.iar-worktrees/` 下
- 默认模式下 worktree 没有未提交或未跟踪文件
- 默认模式下分支已经合入 `<remote>/<base_branch>`，或者 GitHub 上存在以该分支 head 的已合并 PR（覆盖 squash / rebase merge 场景）

历史 `<repo>-worktrees/tasks/issue-<number>` worktree 是旧 `just worktree`/`just implement` 路径，不会被 `iar worktree cleanup` 自动删除。确认安全后可手动执行：

```bash
git worktree remove /path/to/<repo>-worktrees/tasks/issue-<number>
git branch -d issue-<number>
```

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
- 未指定 `--repo`、`--repo-id` 或 `--all` 时，单仓库命令（如 `iar run`、`iar review`）只处理当前 Git 仓库；`iar daemon` 和 `iar review-daemon` 同样只处理当前已初始化注册仓库，未命中、未初始化或匹配多个时报错。如需监控所有 enabled registry entries，请显式使用 `--all`。

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

### Registry 生命周期管理

已注册仓库可以通过 `iar registry` 子命令重新初始化或取消托管，无需手动编辑 `config.toml` 或进目录改 `.iar.toml`。

#### 重新初始化（`iar registry reinit`）

当已接管仓库的 `.iar.toml` 配置（如 `git.remote`）与实际情况不一致时，可以重新初始化：

```bash
# 默认把 remote 重置为 origin，覆盖现有 .iar.toml
iar registry reinit --repo-id zata-zhangtao-fsense

# 显式指定 remote 和 base_branch
iar registry reinit --repo-id zata-zhangtao-fsense --remote upstream --base-branch develop

# 重新初始化后立刻重启 daemon 和 review-daemon
iar registry reinit --repo-id zata-zhangtao-fsense --start-daemons
```

#### 取消托管（`iar registry remove`）

停止 daemon/review-daemon 并从 registry 移除条目：

```bash
# 仅取消托管，保留本地 clone
iar registry remove --repo-id zata-zhangtao-fsense

# 取消托管并删除本地 clone 目录
iar registry remove --repo-id zata-zhangtao-fsense --delete
```

`--delete` 只会删除 registry 中记录的克隆路径，且会校验路径与 registry 记录一致，防止误删其他目录。

#### 查看已注册仓库与运行状态（`iar registry list`）

```bash
iar registry list
```

输出会列出 `~/.iar/config.toml` 中所有已注册仓库，并显示每个仓库的 `daemon` / `review-daemon` 是否在运行，以及对应的进程 ID：

```
                            Registered repositories
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ repo_id               ┃ display... ┃ path                   ┃ daemon  ┃ review-daemon ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ zata-zhangtao-fsense  │ fsense     │ /Users/.../fsense      │ running │ running       │
│                       │            │                        │ (p123)  │ (p124)        │
│ another-owner-repo    │ another    │ /Users/.../another     │ stopped │ stopped       │
└───────────────────────┴────────────┴────────────────────────┴─────────┴───────────────┘
```

这可以帮助你确认哪些仓库当前正在后台跑 agent，以及是否重复启动了 daemon。

> **Managed vs Unmanaged**：
> - 通过 `iar registry start` / console / `iar takeover` 启动的 daemon 是**托管进程**，会写入 `~/.iar/processes.json`，状态显示为 `running (<process_id>)`，可用 `iar registry stop` 停止。
> - 直接在命令行执行 `iar daemon` / `iar review-daemon` 启动的进程是**未托管进程**。`iar registry list` 会通过扫描系统进程把它们识别出来，状态显示为 `running (unmanaged)`，但**不会**被 `iar registry stop` 停止，也没有独立的日志文件被 `registry` 命令管理。
> - 同时存在托管与未托管进程时，列表优先显示托管状态。

> **不要混用**：同一时间、同一仓库，建议要么只使用 `iar registry start` 管理 daemon，要么只手动运行 `iar daemon`。混用可能导致两个进程同时 claim 同一仓库的 Issues，且 `registry stop` 不会清理手动启动的进程。

> **单实例保护（self-guard）**：`iar daemon` / `iar daemon run` 启动时会按 `repo_id` 获取单实例锁（`~/.iar/daemon-locks/<repo_id>.lock`）。若该仓库已有存活的 daemon（无论托管还是手动启动），新进程会**直接拒绝启动并返回非零退出码**，而不会与既有 daemon 并发轮询、重复 claim 同一仓库的 Issues。不同 `repo_id` 的 daemon 互不影响、可并行运行。被 `kill -9` 等异常终止后残留的过期锁，会在下次启动时自动回收（按记录的 PID 判活，死进程的锁可被抢占）。该保护用于防止反复执行 `iar daemon` 堆积出大量并发实例、成倍消耗 agent 调用与 token 预算。

#### 查看 daemon 进程明细（`iar daemon status`）

`iar registry list` 只显示每个仓库 daemon / review-daemon 的汇总状态。如果你需要查看具体进程的 PID、启动时间、可执行路径、命令行，以及该进程是托管还是未托管，使用：

```bash
# 在当前仓库目录下查看当前仓库
iar daemon status

# 查看指定仓库
iar daemon status --repo-id keda-main

# 查看所有 enabled 注册仓库
iar daemon status --all
```

输出示例：

```
                                 Daemon status
┏━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ repo_id ┃ kind          ┃ status        ┃  pid ┃ process_id ┃ started_at ┃ executable ┃ command              ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ keda-m… │ daemon        │ managed run…  │ 1234 │ abc123def  │ 2026-06-2… │ iar        │ iar daemon --repo-i… │
│ keda-m… │ review_daemon │ unmanaged r…  │ 5678 │ unmanaged… │ 2026-06-2… │ /usr/bin…  │ /usr/bin/iar review… │
└─────────┴───────────────┴───────────────┴──────┴────────────┴────────────┴────────────┴──────────────────────┘
```

- `managed running`：通过 `iar registry start` / `iar takeover` / console 启动的托管进程。
- `unmanaged running`：直接在命令行执行 `iar daemon` / `iar review-daemon` 启动的进程。

`iar daemon` 本身继续作为启动 daemon 的快捷命令，等效于 `iar daemon run`。例如：

```bash
iar daemon --repo-id keda-main --interval 300
# 等效于
iar daemon run --repo-id keda-main --interval 300
```

#### 启动与停止托管 daemon（`iar registry start` / `iar registry stop`）

对于已经在 registry 中注册且已 init 的本地仓库（例如你手动 `iar init` 过的 `keda-main`），可以直接用 `start` / `stop` 管理 daemon 生命周期，无需 `reinit --start-daemons`（后者会重置 `.iar.toml`）：

```bash
# 启动单个仓库的 daemon + review-daemon
iar registry start --repo-id keda-main

# 只启动 daemon（不启动 review-daemon）
iar registry start --repo-id keda-main --no-review-daemon

# 启动所有 enabled 注册仓的 daemon + review-daemon
iar registry start --all

# 停止单个仓库的 daemon + review-daemon
iar registry stop --repo-id keda-main

# 停止所有 running 的 daemon + review-daemon
iar registry stop --all
```

`start` 会把进程登记到 `~/.iar/processes.json`，因此 `iar registry list` 会显示 `running`；`stop` 会从 processes.json 读取 running 记录并优雅停止对应进程。连续两次 `start --repo-id` 会因"已存在 running 进程"失败，需先 `stop`。

> **与 `reinit --start-daemons` / `takeover` 的区别**：`registry start` 不修改 `.iar.toml`，不重新初始化仓库配置，仅负责 spawn / stop 托管进程。适合本地开发目录或已经 init 过的仓库。

> **重启后恢复**：`iar` 不会自动复活重启前的 daemon。把 `iar registry start --repo-id <id>` 写进 macOS `launchd` plist 或 Linux systemd service，可实现开机自启。

## 全局多仓库接管（`iar takeover`）

全局安装 `iar` 后，你可以从任意目录直接接管 GitHub 仓库，无需手动 `git clone`、`iar init` 和编辑 registry。

### 前置条件

- 已全局安装 `iar`（见上文"安装"）。
- 已登录 GitHub CLI：`gh auth login -h github.com`。
- `iar` 会在首次需要时自动把默认配置复制到 `~/.iar/config.toml`，作为全局配置源。

### 交互式接管

```bash
# 列出当前 gh 用户可见的仓库，勾选后自动接管
iar takeover
```

流程：

1. 检查 `gh` 登录状态。
2. 调用 `gh repo list` 拉取仓库列表（默认 100 条）。
3. 过滤掉已经注册且本地路径存在的仓库。
4. 在终端展示 checkbox 多选界面，输入编号 toggle、`all` 全选、`none` 清空、`done` 确认、`quit` 取消。
5. 对每个选中的仓库：
   - `gh repo clone <owner>/<repo> ~/.iar/repos/<owner>/<repo>`
   - 在新 clone 的仓库执行 `iar init`
   - 写入 `~/.iar/config.toml` 的 `[agent_runner.repositories.<repo_id>]`
6. 默认启动 `iar daemon` 和 `iar review-daemon` 两个托管子进程（在目标仓库路径下启动，因此只监控该仓库）。

### 非交互式与批量接管

```bash
# 直接指定仓库，适合脚本
iar takeover --repos owner/repo-a owner/repo-b

# 指定组织或用户
iar takeover --owner myorg --limit 200

# 指定 clone 根目录（默认 ~/.iar/repos）
iar takeover --clone-root ~/iar-repos

# 只接管，不启动 daemon
iar takeover --repos owner/repo-a --no-start

# 预览将要执行的操作，不写入任何文件
iar takeover --repos owner/repo-a --dry-run
```

### 全局配置与进程日志

接管后的仓库注册在 `~/.iar/config.toml` 中，托管进程使用：

- `~/.iar/console.db`：运行历史与审计日志。
- `~/.iar/processes.json`：托管进程 pidfile registry。
- `~/.iar/process-logs/<repo_id>/`：daemon / review-daemon 的 stdout/stderr 日志。

你可以通过现有 HTTP 管理终端查看、停止、重启这些进程（console 子命令可通过 FastAPI 服务或已暴露的 Typer 子命令访问，具体取决于部署方式）。

### 接管后的日常命令

```bash
# 查看所有托管进程状态（通过 HTTP API）
curl http://localhost:8000/api/v1/agent-runner/console/processes

# 停止某个托管进程
# 先在 list 响应中找到 process_id，再调用 stop 端点
curl -X POST http://localhost:8000/api/v1/agent-runner/console/processes/<process-id>/stop

# 查看进程日志
curl 'http://localhost:8000/api/v1/agent-runner/console/processes/<process-id>/logs?offset=0'

# 手动对某个接管的仓库跑一次
iar run --repo-id owner-repo-a
```

## 状态流转与两阶段审查

### 完整状态机

```text
agent/ready
    → claim → agent/running
    → implementation agent commit
    → Issue comment: Implementation Complete
    → push implementation branch to remote
    → pre-PR review (仍在 agent/running; reviewer 修复也会 push)
    → Draft PR creation
    → agent/supervising
    → Issue comment: Draft PR Created
    → post-PR supervisor cycle
    → supervisor approve → agent/review
    → supervisor repair/rebase → agent/running (existing PR branch rework)
    → supervisor human-input-needed → agent/blocked
    → supervisor failed → agent/failed
```

### Pre-PR Review

`iar run` 在实现 agent 完成并提交后会先 `git push` 推送到远程分支，再在 Draft PR 创建之前执行一次 pre-PR AI code review：

1. Runner 写 Issue comment `Implementation Complete`
2. Runner 立即调用 `push_changes()` 把当前 feature branch 推送到配置 remote（push 不再被 review 阻塞）
3. Runner 构建 review packet（Issue、PRD、diff、changed paths、verification results、AI standards、review workflow）
4. **Reviewer 必须通过 Skill 工具调用 `code-reviewer` skill** 并把 skill 输出的 findings 写进响应的 `findings` JSON 数组；review packet 默认模板（`agent_review.DEFAULT_REVIEW_PROMPT_TEMPLATE`）会显式提示 reviewer 这一行为，并提供 findings schema（`category` / `severity` / `file` / `line` / `title` / `description` / `recommendation`）。仓库可在 `[agent_runner.pre_pr_review].review_prompt_template` 覆盖该模板，未配置时回退到代码默认。
5. 打开新的 AI session 执行 review，并使用 `[agent_runner.pre_pr_review].timeout_seconds` 限制最长运行时间
6. Reviewer 可直接修改 worktree；修改后写入 `.agent-runner/commit-request.json`
7. Runner 通过 commit proxy 提交 reviewer 修改、重新运行 `verification_commands` 并立即 `push_changes()` 把修复推送到远程分支
8. Runner 写 Issue comment `Pre-PR Review Result`（含 findings 表格）
9. Review 通过后才调用 `create_draft_pr()` 创建 Draft PR；review 未收敛时跳过 PR 创建，runner 软失败并写 comment

review packet 现在是 **修复-再审查收敛模式**：轮数由 `[agent_runner.pre_pr_review].max_attempts` 控制，默认 2 轮。每一轮 reviewer 都可以通过 `commit-request.json` 自修复（runner 仅负责 commit proxy + verification 重新执行 + push callback 把修复推送到远程）。如果 reviewer 在一轮内报出了 findings 却未写 `commit-request.json`，runner 会追加一条提醒并把该轮内重新调用 reviewer 最多 `[agent_runner.pre_pr_review].commit_request_reminder_attempts` 次（默认 1 次），让 reviewer 有机会把 findings 落实为补丁，而不是直接放弃。最后一轮结束后若仍未 `approved` 但 reviewer 已写最终修复 commit request，runner 接受该最终修复并继续发布；否则写一条 findings 评论并走软失败路径（runner 不再抛出硬错误，但调用方会按 `agent/failed` 处理）。Reviewer 解析器会基于 findings 数组重新统计 `critical`/`high`/`medium`/`low` 计数，避免 reviewer 自填数字被信任；若 verdict 为 `approved` 但 findings 非空，verdict 会被降级为 `changes_requested` 以避免漏报。

Pre-PR review 不产生独立的 durable label，整个过程仍在 `agent/running` 内。Runner 会记录 review start、cycle、reviewer exit code、parsed verdict、commit-request 处理、push callback、findings 计数和 result comment 写入等日志；底层进程 runner 对长时间运行的 agent 命令每 60 秒输出一次 heartbeat，并在达到 timeout 时终止子进程。

> **空 commit request 行为**：当 reviewer 写出了 `.agent-runner/commit-request.json` 但工作树已无任何可提交改动（例如 reviewer 的建议与现状一致，或上一轮 cycle 已经提交过修复），runner 会按 reviewer 解析出的真实 verdict 处理：
>
> - `approved` → 写一条 `Pre-PR Review Result` 评论（action summary 为 `reviewer approved with an empty commit request`），循环正常收敛。
> - `changes_requested` → 写一条评论（action summary 为 `reviewer requested changes but produced no committable diff`）并继续下一轮 cycle；用尽 `max_attempts` 后走 `Pre-PR review did not approve after N attempt(s): ...` 软失败路径。
>
> 若 reviewer stdout 没有可解析的 JSON verdict，runner 会在 commit request 中读取可选的 `verdict`、`summary`、`findings_high`、`findings_medium`、`findings_low` 元数据作为兜底。空提交信号由 `EmptyCommitRequestError`（`RuntimeError` 的子类，message 保持 `"Agent requested a commit but produced no file changes."`）承载，因此 `is_recoverable_commit_request_error(...)` 仍把它分类为可恢复，且不会被升级为 `Pre-PR review repair failed` 硬失败。

> **approved + 非空补丁行为**：当 reviewer verdict 为 `approved` 且写出了非空 `.agent-runner/commit-request.json` 时，runner 通过 commit proxy 提交补丁、重跑 `verification_commands` 并通过 push callback 把修复 push 到远程，成功后该轮直接收敛通过（action summary 为 `reviewer approved and runner committed follow-up patch`），不会被降级为 `changes_requested` 后在最后一轮硬失败。若 verdict 为 `changes_requested` 且补丁提交成功，则继续下一轮 cycle；用尽 `max_attempts` 时，软失败信息反映最后一轮的实际结果（`reviewer patched and runner committed follow-up changes`），不会显示更早 cycle 的陈旧摘要。

### Post-PR Supervisor

Draft PR 创建后，Issue 先进入 `agent/supervising`，并立即运行至少一次 supervisor cycle：

1. Runner 写 Issue comment `Draft PR Created`
2. Supervisor 收集 PR context、Issue comments、PR comments、base branch 状态、CI/check 状态、diff、verification results
3. 如果 worktree 在只读 supervisor cycle 开始前仍有未提交变更，runner 会自动 `git stash push -u` 把它们临时存起来，cycle 结束后再根据 supervisor 决策恢复（pop）或继续保留（`wait_for_checks`）
4. Supervisor 输出结构化 action：
   - `approve_for_human_review` → 恢复 stash 后若 worktree 干净则进入 `agent/review`，仍有未提交变更则进入 `agent/blocked`
   - `repair_pr_branch` / `resolve_conflict` → 恢复 stash 后进入 `agent/running` 做现有 PR branch 修复
   - `rebase_pr_branch` → 恢复 stash 后进入 `agent/running` 做 rebase
   - `wait_for_checks` → 保持 stash 与 `agent/supervising`，等待 PR checks 完成
   - `request_human_input` → 恢复 stash 后进入 `agent/blocked`，但必须带有可操作 summary
   - `mark_failed` → 恢复 stash 后进入 `agent/failed`

5. 需要代码修改时，runner 先写 `post_pr_rework_requested` event marker，再切到 `agent/running`
6. 后续 `iar run` 检测到该 pending marker 和 open PR 后，在现有 PR branch 上执行 rework
7. rework 成功后写 `rebase_repair_complete` marker，再进入后续 supervision/review 流程

#### Rebase Conflict Recovery Branch Guard

在 rebase conflict recovery 过程中，runner 会在继续 rebase 之前先校验当前 branch。如果 `git branch --show-current` 返回 PR branch 名称，说明工作区处于正常 branch 上，恢复流程继续执行。如果返回空（表示处于 detached HEAD 的 rebase 中间态），runner 不会仅凭空 branch 名称就继续，而是进一步读取 Git 的 active rebase metadata（`.git/rebase-merge/head-name` 或 `.git/rebase-apply/head-name`），确认 rebase 目标 branch 与预期的 PR branch 一致后才允许继续。

如果 rebase metadata 缺失、目标 branch 未知，或者解析出的目标与预期 PR branch 不匹配，runner 会拒绝继续并抛出带有诊断信息的错误，且**不会自动 abort rebase**。这样可以把工作区保留在冲突状态，供运维人员手动排查。该检查独立于普通的 commit proxy branch validation，专门用于保护 rebase 中间态。

- 正常 branch：直接校验 branch 名称与预期 PR branch 是否一致。
- Detached HEAD rebase：读取 `.git/rebase-merge/head-name` 或 `.git/rebase-apply/head-name` 解析目标 branch。
- 目标未知或不匹配：拒绝继续，保留 rebase 状态，输出诊断错误。

### 持续观察

```bash
# 单次检查所有 supervising/review Issues
uv run iar review

# 常驻 review daemon（默认每 120 秒轮询，可在 config.toml [agent_runner.daemon] 调整）
uv run iar review-daemon
```

`iar review` / `iar review-daemon` 会：
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

例外：最新 supervisor marker 记录的 action 为 `mark_failed` 时（如 agent
基础设施崩溃重试耗尽、或输出不可解析触发 fail-closed），上一轮并没有产出
有效评审结论。此时人工把 label 从 `agent/failed` 拨回 `agent/supervising`
即视为明确的重试请求，即使上下文完全未变化也会重新运行 supervisor cycle，
不需要靠加评论或推新 commit 来"制造变化"。

PR context 读取使用当前 GitHub CLI 支持的 `statusCheckRollup` 字段聚合
checks 状态：

- 任一 check/status 失败时，`checks_state=FAILURE`
- 任一 check/status 仍在 queued、in_progress 或 pending 时，`checks_state=PENDING`
- 所有 check/status 成功、skipped 或 neutral 时，`checks_state=SUCCESS`
- 无 CI/check rollup 时，`checks_state` 为空，不阻断人工 review

即使 supervisor 返回 `approve_for_human_review`，runner 仍会执行确定性门禁：

- PR 当前不可合并或存在冲突时，approval 会被改写为 `rebase_pr_branch`
- PR checks 已失败时，approval 会被改写为 `repair_pr_branch`
- PR checks 仍在进行中（`checks_state=PENDING`）时，approval 会被改写为 `wait_for_checks`，Issue 保持 `agent/supervising` 继续等待 checks 完成，不会消耗 repair 次数
- open PR 存在但完整 PR context 暂时无法读取时，本轮 supervision 会 defer 并保留待观察状态；发布、rework 后续评审和循环内再次评审都不会使用不完整 context 批准进入 review
- supervisor 输出无法解析、返回未知 action，或返回空 summary 的 `request_human_input` 时，本轮会进入 `agent/failed`，不会生成无原因的 `agent/blocked`
- supervisor agent 进程非零退出且 stdout 中识别不到任何 JSON 决策（如 claude CLI 遇到 API / 网络错误崩溃）时，视为基础设施级失败，会在同一 cycle 内重试至多 `max_agent_crash_retries` 次；重试之间指数退避（从 `crash_retry_initial_backoff_seconds` 起每次翻倍，单次等待封顶 `crash_retry_max_backoff_seconds`，默认 30s 起、上限 10 分钟），以扛住分钟级的 API 提供方中断。重试仍失败才进入 `agent/failed`，summary 会标明是 agent infrastructure failure。非零退出但 stdout 中仍能识别出 JSON 决策时直接使用该决策，不消耗重试次数

这样可以避免 `agent/review` label 覆盖仍需 `iar run` 消费的 rework/rebase 状态。

`iar review` 的 CLI 日志会打印本轮 outcome，例如 `queued_rebase_pr_branch`、
`approved_for_human_review` 或 `deferred_pr_context_unavailable`。被 queue 的
rebase/repair 仍由下一次 `iar run` 在 PR branch worktree 中执行。

### Rework Guard

`iar run` 遇到 `agent/running` Issue 时，不会自动视为 rework。只有同时满足以下条件才会进入现有 PR branch rework 路径：

1. Issue comments 中存在尚未被 `rebase_repair_complete`、`draft_pr_created`、`implementation_complete` 或 `publish_recovered` 消费的 `phase=post_pr_rework_requested` marker
2. 该 marker 包含 PR branch
3. 该 PR branch 仍有 open PR，且 marker 中的 `head_sha` 与 open PR 当前 head 一致；不一致说明 PR 已被外部更新，旧的 rework 请求不再安全

后续 `post_pr_supervisor` 这类观察类 marker 不会覆盖 pending rework marker；只有明确的完成/发布类 marker 才会消费旧 rework 请求。

如果 pending rework 存在但对应 worktree 路径已不存在（例如被手动删除），runner 会将 Issue 转入 `agent/blocked` 并写评论说明缺失路径与恢复步骤，而不是在错误的目录上执行 repair/rebase。

否则 `iar run` 会跳过该 Issue，避免抢占另一个 runner 正在首次执行的任务。

## iar issue list

`iar issue list` 是一个**只读**视图命令，用于回答"哪些 issue 已经提交了 PR、哪些还在排队"。它拉取每个目标仓库的 Issue 列表，并补齐每个 Issue 关联的 Pull Request 状态。

### CWD 自动检测

不传 `--repo` / `--repo-id` / `--all-registered` 时，命令根据 `Path.cwd() / ".iar.toml"` 是否存在自动决定单仓 / 全仓模式：

- 存在 `.iar.toml`（即 `IAR_REPOSITORY_CONFIG_FILENAME`）→ 等价 `--repo Path.cwd()`，只列当前仓
- 不存在 → 等价 `--all-registered`，跨 `config.toml` 中所有 enabled 注册仓

### Flag 表

| Flag | 说明 |
|---|---|
| `--repo <path>` | 强制单仓模式，指向任意本地仓库路径 |
| `--repo-id <id>` | 强制单仓模式，指向 `config.toml` 注册项 |
| `--all-registered` | 强制多仓扫描，即使 cwd 是 iAR 项目仓 |
| `--state <open\|closed\|all>` | Issue 状态过滤（默认 `all`） |
| `--label <name>` | 仅显示带该 label 的 Issue |
| `--with-pr` | 仅显示至少有一个 PR 的 Issue |
| `--without-pr` | 仅显示无 PR 的 Issue；与 `--with-pr` 互斥 |
| `--limit <n>` | 每仓最多拉取 Issue 数（默认 100） |
| `--output <table\|json>` | 渲染格式（默认 `table`） |

### 示例：单仓（cwd 是 iAR 项目仓）

```bash
$ cd ~/code/keda
$ iar issue list
  #     TITLE                              LABELS              STATE   PRS
  42    Add issue list command              iar-agent-ready     open    #143 [draft]
  41    Fix label sync                      iar-bug, urgent     open    #140 [merged]
  40    Update docs                          iar-docs            closed  —
```

### 示例：全仓扫描（cwd 不是 iAR 项目仓）

```bash
$ cd /tmp
$ iar issue list
  REPO              #     TITLE                              STATE   PRS
  owner/repo-a      12    Refactor init                       open    #55 [open]
  owner/repo-a      11    Add tests                            open    —
  owner/repo-b      7     Fix crash                            open    #22 [merged]
```

### 示例：JSON 输出（脚本消费）

```bash
$ iar issue list --with-pr --output json | jq -c '.number, .pulls[0].state'
12
open
7
merged
```

JSON 输出每行一个 `IssueWithPulls` 对象，字段稳定：`repo?`、`number`、`title`、`state`、`labels`、`updated_at`、`url`、`pulls[]`（每个 pull 含 `number` / `state` / `url` / `is_draft` / `merged` / `title`）。

### 错误行为

- `--repo` 与 `--repo-id` 同时传 → 退出码非零，提示互斥
- `--with-pr` 与 `--without-pr` 同时传 → 退出码非零，提示互斥
- 单仓调用失败（gh 不可用、网络错误）→ 退出码非零，错误信息包含 repo 路径和错误原因
- 全仓模式下某仓 API 失败不影响其他仓，最终退出码非零，stderr 含该仓错误

## 常用命令

```bash
# 初始化当前目标仓库配置
iar init

# 同步当前仓库 Labels
iar labels sync

# 同步单个配置仓库
iar labels sync --repo-id keda

# 从 PRD 创建 ready Issue（默认发布 PRD）
iar issue create tasks/pending/example.md --repo-id keda --type feature --agent codex --ready

# 一次从多个 PRD 创建 ready Issue（支持 shell glob）
iar issue create tasks/pending/*.md --repo-id keda --type feature --agent codex --ready

# 直接传文件夹，自动展开其中所有 *.md PRD
iar issue create tasks/pending --repo-id keda --type feature --agent codex --ready

# 多个 PRD 时不能共用 --title（每个 PRD 仍从自身的 H1 标题生成 Issue 标题）
# iar issue create tasks/pending/*.md --title "Shared"   # 会报错
# iar issue create tasks/pending --title "Shared"        # 同样会报错

# 单次执行（dry-run 预览）
iar run --dry-run

# 单次执行（当前仓库）
iar run

# 显式处理所有 enabled registry entries
iar run --all

# Daemon 模式（默认每 120 秒轮询一次，仅当前已初始化注册仓库；加 --all 才处理所有 enabled registry entries）
iar daemon

# 单次 review 检查
iar review

# Review daemon 模式（默认每 120 秒轮询一次，仅当前已初始化注册仓库；加 --all 才处理所有 enabled registry entries）
iar review-daemon

# 恢复发布失败（仅用于已完成审查后的 push/PR 收尾失败）
iar recover --issue 5

# 恢复发布失败（显式确认分支名）
iar recover --issue 5 --branch issue-5
```

## REPL 入口

直接运行 `iar`（不带任何子命令）会进入交互式 REPL 入口。底层调用
[`claude` / `codex` / `kimi`](#repl-agent--command-protocol) 等本地
agent，把仓库上下文与自然语言指令直接转成 IAR 子命令并执行。

### 行为差异

| 触发方式 | 行为 |
|---|---|
| `iar`（TTY） | 启动 REPL，循环读取用户输入，调用 agent，把 agent 标注的 `<<IAR_EXEC>>` 命令翻译成 `iar <subcommand>` 并执行 |
| `iar`（非 TTY） | 打印 Typer 帮助文本并以非零退出码失败，避免 CI / pipe 脚本 hang 住 |
| `iar --help` / `iar -h` | 仍然打印帮助（不进入 REPL） |
| `iar repl` | 显式启动 REPL 的子命令形式 |
| `iar repl --agent codex` | 显式覆盖 REPL 默认 agent |

非 TTY 行为保证现有脚本（如 `cd repo && iar --help`）继续工作。

### REPL Agent & Command Protocol

- 默认 agent 来自 `[agent_runner.repl].default_agent`（默认 `claude`）。
- `--agent codex|kimi` 可覆盖；`--agent auto` 在 REPL 入口被拒绝并回退
  到 `[agent_runner.repl].default_agent`（auto 仅用于 `iar run`）。
- agent 的回复里通过标记协议请求执行 IAR 子命令：

  ```
  <<IAR_EXEC>> iar labels sync --dry-run <<END_IAR_EXEC>>
  ```

  REPL 把标记里的命令交给命令执行器，执行器按白名单与确认策略运行：
  - 默认白名单覆盖 `init` / `labels` / `issue` / `run` / `daemon` /
    `review` / `review-daemon` / `recover` / `blocked-continue` / `ask` /
    `deliberate` / `takeover` / `worktree` / `registry` / `workflow` /
    `completion`。
  - 只读 / dry-run 命令自动执行（`labels sync --dry-run`、
    `run --dry-run`、`ask --plan-only` 等）。
  - 写操作 / 高风险命令（`run`、`daemon`、`issue create`、`recover`、
    `blocked-continue`、`worktree create/remove` 等）执行前会询问
    `Execute? [y/N]`。
  - 不在白名单内的命令、含 shell 元字符的请求、`git push` / `git merge`
    / `git reset` 等直接 git 写操作都会被拒绝并反馈给 agent。

每次执行的结果以 `[IAR_EXEC_RESULT]` 块回写到对话历史；agent 在下一轮
回复里就能看到 stdout / stderr / exit_code。

### 退出与审计

- 用户输入 `/exit` 或 `Ctrl+C` / EOF 时退出 REPL，返回码为 0。
- 会话元数据、对话历史、命令执行记录写入
  `logs/agent-runner/repl/<session-id>/`，包含 `session.json`、
  `transcript.md` 与 `commands.json`。可通过 `[agent_runner.repl].default_output_dir`
  覆盖。
- REPL 内部最大 64 轮（防御性封顶），防止 agent 卡在循环里无限增长。

### 与 `iar ask` 的边界

- `iar ask <prompt>` 是单次决策入口：要求 agent 输出结构化 JSON
  DecisionPlan + 受控执行；适合 CI / 一次性自动执行场景。
- `iar`（REPL）是持续多轮对话入口：agent 可以反复请求执行 IAR 子命令，
  每轮都把结果反馈回对话；适合本地探索与人工协作场景。
- 二者共享 `[agent_runner.interactive_decision]` 与
  `[agent_runner.repl]` 配置段，但 settings 完全独立（默认 agent、
  超时、白名单都分开）。

### 示例：同步 Labels 并启动 daemon

```bash
$ cd /path/to/repo
$ iar
iar> sync the labels for me, then start the daemon.
I'll sync the labels.
<<IAR_EXEC>> iar labels sync <<END_IAR_EXEC>>
[Executed] iar labels sync
stdout: ✅ labels synced

Now I'll start the daemon.
<<IAR_EXEC>> iar daemon <<END_IAR_EXEC>>
This command starts a long-running daemon. Execute? [y/N] y
[Executed] iar daemon
stdout: Daemon started with PID 12345

iar> /exit
```

## PRD Rework Workflow

`iar` supports the reverse of `iar issue create`: automatically generating or rewriting a PRD from an existing GitHub Issue. This is useful when an Issue is created directly on GitHub and later needs a canonical PRD, or when an existing Issue receives new comments that require updating its PRD.

### Triggering PRD Rework

To trigger the workflow, add the `agent/rework-prd` label to an open Issue:

```bash
gh issue edit <issue-number> --add-label agent/rework-prd
```

> **Note:** The `agent/rework-prd` label is provisioned automatically by `iar init` and `iar labels sync`. If your repository was initialized before this label was added, run `iar labels sync` once so the label exists before you apply it.

The next daemon pass or `iar run` will detect the label and process the Issue before normal ready-issue execution.

### What Happens During PRD Rework

1. **List**: The runner queries open Issues labeled `agent/rework-prd` (default limit 1 per pass).
2. **Worktree**: It creates or reuses the `issue-<N>` worktree (the same worktree/branch a downstream ready-issue run would use), so the PRD never touches the main working tree.
3. **Collect**: It loads the Issue body and all comments.
4. **Resolve Path** (inside the worktree):
   - If the Issue body already contains a `- PRD path: \`...\`` anchor, the runner rewrites that same file.
   - If no anchor exists, the runner generates a new filename under `tasks/pending/` using the pattern `P<priority>-<TYPE>-YYYYMMDD-HHMMSS-prd-<slug>.md` (priority/type are inferred from `priority/<p>` and `type/<t>` labels, or the `[Type]` title prefix).
5. **Generate**: It calls the configured content generator. In agent mode the prompt is built from the `prd` skill spec (`~/.claude/skills/prd/SKILL.md`, the single source of the PRD methodology and 11-section output contract); if the skill is unreachable it falls back to the configured `prompt` template, then to a minimal fallback PRD.
6. **Write**: The PRD file is written inside the worktree (overwriting existing or creating new).
7. **Commit + Publish**: The PRD is committed to the `issue-<N>` branch and published via `publish_changes` — pushed to the remote and opened (or reused) as a **draft PR**. A regenerated-but-identical PRD that produces no new commit skips PR creation.
8. **Update Issue**:
   - Inserts/updates the `PRD path:` anchor in the Issue body.
   - Removes `agent/rework-prd`.
   - Adds `source/prd`.
   - Optionally adds `agent/ready`. Because the PRD is committed to the `issue-<N>` branch, a downstream ready-issue run reusing that worktree can read it, so `agent/ready` is safe to keep.
9. **Comment**: Posts a success comment with the PRD path, generation source, and the draft PR link.

Because the PRD lands on the `issue-<N>` branch behind a draft PR (instead of being written straight into `main`), the main working tree stays clean and the change is reviewable before merge.

### Label Transitions

```text
agent/rework-prd  →  source/prd (+ agent/ready optional)
```

On failure:

```text
agent/rework-prd  →  agent/failed
```

A failure comment is posted with the error and instructions to re-add `agent/rework-prd` to retry.

### Stopping at the PRD (skip auto-implementation)

By default, generation does **not** stop at the PRD. Step 8 adds `agent/ready`, and within the **same** `iar run` / daemon pass the PRD-rework phase (`process_prd_rework_issues`) runs *before* the ready-issue phase (`run_once`). So the freshly generated Issue can be claimed and implemented in that same pass; the implementation commits land on the same `issue-<N>` branch and accumulate in the same draft PR as the PRD.

If you want the runner to generate the PRD but **hold before implementing** (e.g. to review the PRD on its own first), use the dependency gate. Add an `iar:depends-on` marker to the **Issue body**, pointing at a sentinel Issue you keep open until you are ready:

```text
<!-- iar:depends-on #<sentinel-issue> -->
```

Group form (waits until every Issue labeled `task-group/<name>` is closed):

```text
<!-- iar:depends-on group:<name> -->
```

How it behaves:

- The gate is evaluated in the ready-issue phase (`run_once`), **not** during PRD generation. The PRD is still generated, committed, and published as a draft PR; only implementation is held.
- An unsatisfied dependency adds `agent/waiting` and the Issue is skipped. An Issue dependency is satisfied when the target Issue is **closed**; a group dependency when all members carrying the `task-group/<name>` label are closed. Close the sentinel Issue (or the group members) to release — the next pass clears `agent/waiting` and proceeds to implementation.
- Add the marker to the Issue body **before** the rework pass. Removing `agent/ready` after generation is racy, because generation and the first claim can happen in the same pass; the body marker is evaluated up front and avoids that race. The rework step only edits the `PRD path:` line, so a pre-existing marker is preserved. See the "Issue 依赖门禁（Dependency Gate）" section above for full marker semantics.

This reuses the inter-Issue ordering gate as a manual hold, so it needs a real sentinel Issue or group to point at. If you are fine reviewing the PRD and its implementation together, you do not need to block at all — nothing merges until the draft PR passes human review and validation sign-off.

### Configuration

The PRD-from-Issue generation target is configured under `[agent_runner.generated_content.prd_from_issue]`:

```toml
[agent_runner.generated_content.prd_from_issue]
enabled = true
mode = "agent"
agent = "auto"
timeout_seconds = 120
body_template = "..."
prompt = "..."   # fallback only — used when the prd skill spec is unreachable
```

In `mode = "agent"`, the PRD prompt is built from the `prd` skill spec rather than the inline `prompt`. The skill path is resolved as: explicit override → `IAR_PRD_SKILL_PATH` environment variable → `~/.claude/skills/prd/SKILL.md`. The skill is the single source of the PRD methodology/output contract, so the inline `prompt` is kept only as a fallback for when the skill file cannot be read (e.g. a runner host without the skill installed). Set `IAR_PRD_SKILL_PATH` when the runner runs in a product repo while the skill lives under a different home directory.

See the "Generated Content 配置" section above for the full template variable list and example.

### Failure Recovery

If PRD generation fails (e.g., AI agent error, write permission issue, or invalid output), the runner:

- Removes `agent/rework-prd`.
- Adds `agent/failed`.
- Comments on the Issue with the error and retry instructions.

After fixing the root cause, manually re-add `agent/rework-prd` to retry.

## 失败重跑

Issue 执行失败后会被标记为 `agent/failed`，runner 不会再自动处理。以下是将失败 Issue 重新置为可执行状态的完整流程。

> 非 publish 阶段失败的 `Agent Runner Failed` 评论末尾自带 `How To Recover` 段，包含可直接复制的 relabel 命令和本章节指向；publish 阶段失败的评论则提示 `iar recover`。两类评论的指引与本章节命令保持一致。

### 错误分级与 fallback 链（escalation ladder）

在把 Issue 标成 `agent/failed` 之前，runner 会按错误性质走一条分级阶梯，尽量自动恢复，而不是一遇错就失败：

1. **瞬时网络错误就地重试（Level 1）**：socket 断开（如 `The socket connection was closed unexpectedly`）、连接重置、网关超时、5xx 等传输层抖动，会用**同一个 agent**就地重试 `transient_retry_attempts` 次（间隔 `transient_retry_delay_seconds` 秒）。实现阶段与 Pre-PR Review 阶段共用这套重试，因此一次 review 抖动不再直接判负。
2. **同 agent recovery**：验证失败、未产出 commit、可修复的请求级错误（含 400 / 上下文超窗）走既有的 recovery 循环——带着失败摘要重新调用同一个 agent 修复，最多 `max_recovery_attempts` 轮。
3. **跨 agent fallback（Level 2）**：当某 agent **耗尽 recovery 预算仍失败**，或命中**供应商容量限制**（429 usage limit、529 overloaded——这类同一供应商重试也只会继续失败），runner 会切换到 `agent_fallback_order` 里的下一个 agent，在已落盘的进度上接力。切换次数受 `max_agent_switches` 封顶。配置中列出但本机未安装的 agent（命令不存在）会被自动跳过。
4. **不切换的情况**：安全违规（禁改路径、分支异常）等不可恢复错误换谁都失败，runner 直接停止、不浪费配额。

`agent_fallback_order` 默认包含 `["claude", "kimi", "codex"]`，主 agent 失败后会依次尝试链中的下一个可用 agent。未安装的 agent（命令不存在）会被自动跳过。将 `agent_fallback_order` 设为空列表即可关闭跨 agent fallback，回退到单 agent 行为。所有尝试（含跨 agent）都会汇总进失败评论的 **Attempt History** 表，其中新增的 **Agent** 列标明每次尝试由哪个 agent 执行。

配置示例见上文 `[agent_runner.runner]`：`agent_fallback_order` / `max_agent_switches` / `transient_retry_attempts` / `transient_retry_delay_seconds`。

### 并行处理 Issue（`iar daemon --concurrency`）

默认 `iar daemon` **逐个串行**处理 Issue。机器空闲、队列较多时，可以让同一轮并行跑多个 Issue：

```bash
# 本轮最多并行处理 3 个 Issue（自动领取至多 3 个）
iar daemon --concurrency 3
```

- **取值来源**：未传 `--concurrency` 时回退到 `[agent_runner.runner].max_concurrent_issues`（默认 `1` = 串行，行为与改动前逐字节一致）。
- **领取上限**：并行时单轮领取上限抬到 `max(max_issues, concurrency)`，所以单独一个 `--concurrency N` 即可领到并跑 N 个，无需再调 `--max-issues`。
- **隔离**：每个 Issue 仍各自 worktree / 分支；共享仓库的 worktree 创建被串行化以避开 `.git` 竞争，真正耗时的 agent 执行阶段全程并行。
- **作用范围**：仅 `iar daemon`（含 `iar daemon run`）。多仓库（`--all`）仍逐仓库串行、仓库内 Issue 并行。
- **成本提醒**：`--concurrency N` 即 N 路 agent 同时烧 token，请按额度与机器资源设定。

> “本次优先某个 agent”无需新参数：`iar daemon --agent claude` 已把该 agent 放到 fallback 链首位（见上文 escalation ladder）。

#### 并行时查看每个 Issue 的日志

并行时多个 agent 的输出若都打到同一个终端会交错成乱码，因此 runner 会按 Issue 分流：

- **每 Issue 日志文件**（始终写）：`logs/agent-runner/issues/<repo_id>/issue-<N>-<时间戳>.log`，含该 Issue 的 agent 流式输出与处理日志，可在 detached / 托管模式下 `tail -f` 回看，互不交错。
- **实时看板**（前台 TTY）：在交互终端直接 `iar daemon --concurrency 3` 时，会显示一个仿 `iar deliberate` 的多列实时面板，每个运行中的 Issue 一列；非 TTY（重定向、`iar registry start` 托管、CI）自动退化为按行加 `[issue #N ...]` 前缀的纯文本 + 上述日志文件。

### 进度落盘与跨 claim 续作（checkpoint）

体量较大的 PRD 往往无法在单次 claim 的 `max_recovery_attempts` 轮内完成。为避免每次 claim 都从零开始、永远收敛不了，runner 在一轮实现失败（耗尽重试、交付门禁仍未通过）时，会把 Agent 已经产出的在途改动提交成一个 **WIP checkpoint**：

- 提交信息形如 `[Agent][WIP] Issue #<N> checkpoint (delivery gates not yet satisfied; not for merge)`，使用 `git commit --no-verify`（在途工作可能还不过 lint），但仍执行 forbidden-path 安全校验，绝不提交 `.env` / `secrets/*` 等敏感文件。
- checkpoint 只落在 Issue 本地分支 `issue-<N>` 上，**不会被推送或合入**：发布前的本地 commit 复用检查与 publication 仍会运行 `verification_commands`、PRD 交付门禁和 Realistic Validation 证据门禁。

下一次 claim（重新置为 `agent/ready` 后）会复用该分支：

- 已有提交**已达交付标准**（验证通过、PRD 清单全勾、证据齐备）→ 直接发布，不再调用 Agent。
- 已有提交**尚未达标**（典型：上一次的 WIP checkpoint）→ runner 不再硬失败，而是带着 “continue from committed progress” 的 prompt 重新调用 Agent，在已提交进度上补齐剩余工作。
- 无本地提交 → 全新实现。

因此对体量大的 Issue，反复 `agent/failed → agent/ready` 会让进度逐轮累积，而不是空转；合并时这些 WIP commit 可通过 squash 收敛为干净历史。

> **重跑时不要删除分支**：`git worktree remove` 只删工作树目录、保留 `issue-<N>` 分支上的 checkpoint，是安全的；但**不要删除 `issue-<N>` 分支本身**，否则已落盘的进度会丢失，Agent 又得从零开始。

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

2. **根据失败评论选择恢复命令**

   大多数失败需要回到 `agent/ready` 让 runner 重新认领并继续工作：

   ```bash
   gh issue edit <issue-number> --add-label agent/ready --remove-label agent/failed
   ```

   但如果 Agent 已经完成实现、验证通过，只是在最后的 workflow 标签切换（如 `running → supervising`）时因 GitHub API 临时抖动失败，评论会提示：

   > The agent finished its work, but the final workflow label transition failed.
   > You can retry the transition without re-running the agent:

   此时应直接执行评论中给出的命令，例如：

   ```bash
   gh issue edit <issue-number> --add-label agent/supervising --remove-label agent/failed
   ```

   这样可以避免 runner 从头重跑已经完成的实现流程。如果目标标签是 `agent/review`，则使用对应的 `agent/review` 命令。

   也可以在 GitHub 网页上手动编辑 Issue 标签，移除 `agent/failed` 并添加对应的目标标签。

3. **触发 runner 执行**

   标签改回 `agent/ready` 后，runner 会在下一次轮询时自动拾取：

   ```bash
   # 单次轮询（立即执行）
   iar run

   # 或等待 daemon 下次轮询
   ```

### 状态流转回顾

```
agent/ready  →  agent/running  →  agent/supervising  →  agent/review  →  关闭
      ↑              ↓                                    ↓
      └──────  agent/failed  ←───────────────────────────┘
              （人工修复后改回 ready）

agent/supervising ── supervisor 要求 rework ──→ agent/running ── 修复/rebase ──→ agent/supervising

# recover 恢复路径（supervisor enabled）
agent/failed ── recover ──→ agent/supervising ── supervisor approve ──→ agent/review

# recover 恢复路径（supervisor disabled）
agent/failed ── recover ──→ agent/review

# forbidden path blocked 恢复路径
agent/running ── forbidden path 拦截 ──→ agent/blocked ── blocked-continue ──→ agent/running ── 继续执行 ──→ agent/supervising
```

## Forbidden Path 阻塞恢复

当 Agent 在 commit 阶段触发了 `forbidden_path_patterns`（如修改了 `.env.example`），runner 会将 Issue 标记为 `agent/blocked` 而不是 `agent/failed`，因为人工确认后可以继续完成剩余任务。

### 触发条件

- Agent 的变更中包含匹配 `forbidden_path_patterns` 的文件
- `validate_safe_changes()` 在 `commit_requested_changes()` 阶段拦截
- Issue 进入 `agent/blocked`，评论中包含被拦截的文件列表和恢复命令

### 恢复步骤

1. **查看 blocked 评论**

   在 Issue 评论中找到 `## Agent Runner Blocked`，确认被拦截的文件列表。

2. **在 worktree 中处理 forbidden 文件**

   进入对应 worktree，根据业务需求选择提交、修改或撤销这些文件：

   ```bash
   cd $(iar worktree path --branch issue-<number>)
   git status
   # 处理 forbidden 文件后确保 worktree 干净
   git add -A && git commit -m "resolve forbidden paths"
   ```

3. **运行 blocked-continue 继续执行**

   ```bash
   uv run iar blocked-continue --issue <number>
   ```

   CLI 会依次执行：
   - 校验 worktree 存在且分支正确
   - 校验 worktree 干净（无未提交变更）
   - 校验 pending diff 不再包含 forbidden paths
   - 写入 `blocked_resolution_requested` marker comment
   - 通过 label CAS（compare-and-swap）竞争认领：将 `agent/blocked` 切换为 `agent/running`
   - 认领成功后发送 continuation prompt，让 Agent 继续完成剩余任务

### 竞争安全

多个 runner 同时处理同一个 blocked Issue 时，只有第一个成功执行 label CAS 的 runner 会继续。其他 runner 会收到明确提示并跳过。即使 `iar blocked-continue` 只写了 marker 但 CAS 被其他进程抢占，后续 `iar run` 轮询时也会检测到该 marker 并完成认领。

### 与 run 兜底路径的关系

`iar run` 在消耗完 `agent/ready` 和 `agent/running` 配额后，也会扫描 `agent/blocked` Issue。对带有 `blocked_resolution_requested` marker 的 Issue，它会执行同样的 CAS 竞争认领。这意味着：

- 你可以只写 marker（通过脚本或评论），不运行 `blocked-continue`，由 daemon 自动认领
- 也可以运行 `blocked-continue` 立即触发 continuation

### 状态流转补充

```text
agent/running ── commit 时 forbidden path 拦截 ──→ agent/blocked
agent/blocked ── 人工处理 + blocked-continue ──→ agent/running ── 继续执行 ──→ agent/supervising
```

### 注意事项

- `blocked-continue` 只处理 commit 阶段的 forbidden 拦截，不处理 publish 阶段的拦截
- 继续执行后如果 Agent 再次触发 forbidden 拦截，会重新回到 `agent/blocked`
- worktree 不干净时 `blocked-continue` 会失败，必须先提交或 stash 所有变更
- 被拦截的文件路径会写入 `blocked_resolution_requested` marker，供 continuation prompt 引用

## 发布失败恢复

当 Agent 已完成代码修改、生成本地 commit，并且 runner 已经走到发布阶段（push、PR 创建、label 更新等）后失败时，Issue 会被标记为 `agent/failed`。此时重新运行 Agent 是浪费且可能引入不必要代码变更的。

`iar recover` 命令用于安全、幂等地完成发布收尾，无需重新启动 Agent。

### 何时使用 recover

- Agent 已执行完毕，本地 commit 已存在
- 发布阶段因网络错误、GitHub CLI 认证过期、API 限流等原因失败
- Issue 失败 comment 中包含 `iar recover --issue <number>` 提示
- 该本地 commit 已经由正常 runner 路径完成过配置启用的 pre-PR review，失败点只在 push、PR 创建或 label/comment 更新等发布收尾阶段

### 不适用的情况

- Agent 未产生任何 commit
- 工作区有未提交变更
- 需要修改 Agent 已生成的代码
- 当前分支是 base branch
- forbidden path 在 commit 阶段拦截后，由人工整理并提交了 worktree，但这些提交还没有经过 pre-PR review

> **注意**：`iar labels sync` 只同步 GitHub labels，**不**校验发布环境。`iar run` 在领取 Issue 前会检查 `[agent_runner.git].remote` 是否存在。

### 与 pre-PR review 和 post-PR supervisor 的关系

`iar recover` 只做发布收尾：校验 worktree 干净、校验分支、push、创建或复用 Draft PR、更新 Issue label 和 comment。它不会运行 pre-PR review（因为恢复路径要求本地 commit 已经由正常 runner 路径完成过 pre-PR review）。

**当 `post_pr_supervisor.enabled = true` 时**，成功恢复后会先进入 `agent/supervising` 并运行 post-PR supervisor；只有 supervisor `approve_for_human_review` 后，Issue 才会进入 `agent/review`。

**当 `post_pr_supervisor.enabled = false` 时**，成功恢复后直接移除 `agent/failed` / `agent/running` / `agent/ready`，添加 `agent/review`。

如果失败发生在 `Refusing to publish forbidden paths: ...` 这类 forbidden path 拦截处，并且人工已经确认这些文件可以提交、手动创建了本地 commit，应改走 `agent/running` 的本地 commit 复用路径，让 `iar run` 执行完整的 verification、pre-PR review、publish 和 post-PR supervisor：

```bash
# 1. 在对应 issue worktree 中确认已有本地 commit，且工作区干净
git status --short
git log -1 --oneline

# 2. 将 Issue 改回 running，让 run 通过本地 commit 恢复路径处理
gh issue edit <number> --add-label agent/running --remove-label agent/failed,agent/ready

# 3. 触发一次 runner 轮询；run 没有 --issue 参数，会扫描可处理的 Issues
uv run iar run
```

这条恢复路径要求 worktree 相对配置的 `{remote}/{base_branch}` 有本地 commit，且 `git status --short` 为空。若当前还有 `agent/ready` backlog，runner 会先消耗 ready 配额；必要时提高 `--max-issues`，或在没有 ready backlog 时执行。

### 使用方法

```bash
# 恢复 Issue #5 的发布
uv run iar recover --issue 5

# 如果当前分支名不包含 issue 编号，需要显式确认分支
uv run iar recover --issue 5 --branch feature-xyz
```

### 分支安全与 Issue number 边界

`iar recover` 默认要求当前分支名把 Issue number 当作**完整 token 或路径 segment** 包含在内。以下分支在恢复 Issue #42 时会被**拒绝**：

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
uv run iar recover --issue 42 --branch issue-421
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

`iar recover` **不会**执行以下操作：

- 运行 implementation Agent 命令或 recovery prompt
- 执行 `git add` 或 `git commit`
- 创建新的 worktree
- 合并分支或删除分支
- 推送到非配置 remote

当 `post_pr_supervisor.enabled = true` 时，`iar recover` 会复用现有 supervisor repair loop，但 supervisor 本身仍然是只读审阅；需要代码修改时由现有 repair/rebase commit proxy 处理，不会由 supervisor 直接提交文件。

### 手动恢复回退

当无法使用 `iar recover` 命令、需要人工兜底时，可手动执行以下命令完成恢复。

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
uv run --project /path/to/keda iar issue create tasks/pending/feature-login.md \
  --type feature \
  --agent codex \
  --ready
```

> `--agent` 可选 `codex` / `claude` / `kimi` / `auto` / `none`。`auto` 按 Issue label 自动路由，`none` 不添加 agent 路由 label。推荐在交给 runner 前保持默认 PRD 发布并加 `--ready`，确保 runner 的 base branch 能读取到已回写 Issue URL 的 canonical PRD。

#### PRD 发布边界（`--publish-prd` / `--no-publish-prd`）

`iar issue create` 默认发布 PRD（`--publish-prd` 默认开启）。传入 `--no-publish-prd` 时只创建 Issue 并本地回写 PRD，不执行 `git add`、`git commit` 或 `git push`，转而通过交互式 prompt 询问是否发布。

发布 PRD 时，命令会在 Issue URL 回写到目标 PRD 后执行 PRD-only 发布：只 `git add` 传入的 PRD 文件，只提交该 PRD 文件，然后 push 到 `config.toml` 中 `[agent_runner.git]` 配置的 remote。工作区其他未跟踪或已修改文件不会被加入这个 commit；如果 Git index 里已经 staged 了非目标 PRD 文件，命令会失败，避免把用户已有 staged changes 混入 PRD 发布 commit。

当发布 PRD 且传入 `--ready` 时，创建 Issue 的第一步不会带 `agent/ready`。只有 PRD commit push 成功后，命令才会通过 GitHub API 给 Issue 添加 `agent/ready`。如果 push 失败，命令返回失败，保留已创建但未 ready 的 backlog Issue，runner 不会领取它。

Ready 发布要求当前分支等于 `[agent_runner.git].base_branch`，因为 runner 默认从 base branch 创建 worktree。若当前分支不是 base branch，命令会失败并提示切换到 base branch 或改用 `--no-ready`。

如果 PRD 发布阶段的 Git 命令失败，例如 `git commit` 被 pre-commit hook 拦截，`iar issue create` 会在终端和日志中展示失败命令、退出码以及捕获到的 stdout/stderr，便于直接看到 hook 或 Git 返回的原始错误。

Runner 新建 issue worktree 时，默认会同步 base branch 的远程 tracking ref 作为起点，使新分支基于最新远程提交，而非可能过期的本地 base branch。复用已存在的 worktree 时，runner 会在 agent 执行前自动将当前 worktree 分支与配置的远程同名分支做安全对齐：仅当 worktree 干净且本地分支是远程分支的祖先时执行 fast-forward；本地已有未发布 commit 时保留本地状态；worktree 脏或分支已分叉时显式失败，要求人工处理，而不是自动 rebase、merge 或 reset。

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
uv run --project ~/keda iar run --dry-run

# 真正执行一次（当前仓库）
uv run --project ~/keda iar run --agent codex
```

#### 4. Daemon 常驻模式（生产用）

```bash
cd /path/to/target-repo

# 每 120 秒轮询一次（当前仓库）
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

# 每 120 秒检查一次 supervising/review Issues
uv run --project ~/keda iar review-daemon
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
uv run --project /path/to/keda iar issue create tasks/pending/xxx.md --agent codex --ready

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

> **自动认证检测**：执行 `iar labels sync`、`iar issue create`、`iar run`、`iar daemon`、`iar review`、`iar review-daemon` 等需要 GitHub API 的命令前，`iar` 会自动检测 `gh` 认证状态。如果认证失效，会提示运行 `gh auth login -h github.com` 并以退出码 1 退出，避免暴露原始异常。
>
> 在 CI 或脚本环境中，可设置环境变量跳过该检查：
> ```bash
> IAR_SKIP_GH_AUTH_CHECK=1 iar labels sync
> ```

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
create_command = "iar worktree create --branch issue-{issue_number} --base-branch {base_branch}"
reuse_command = "iar worktree path --branch issue-{issue_number}"
path_command = "iar worktree path --branch issue-{issue_number}"

[agent_runner.runner]
default_agent = "auto"
max_recovery_attempts = 5
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

[agent_runner.pre_pr_review]
enabled = true
review_agent = "auto"
allow_same_agent = true
max_attempts = 2
timeout_seconds = 1800
commit_request_reminder_attempts = 1

[agent_runner.post_pr_supervisor]
enabled = true
supervisor_agent = "auto"
max_repair_attempts = 2
max_agent_crash_retries = 5
crash_retry_initial_backoff_seconds = 30
crash_retry_max_backoff_seconds = 600

[agent_runner.daemon]
# daemon / review-daemon 的默认轮询间隔（秒），CLI --interval 可覆盖
review_interval_seconds = 120
run_interval_seconds = 120
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
enabled = true
fallback = "template"
max_input_chars = 20000
default_agent = "auto"

[agent_runner.generated_content.issue_from_prd]
enabled = true
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
enabled = true
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

### PRD-from-Issue 生成变量

| 变量 | 说明 |
|---|---|
| `{issue_number}` | GitHub Issue 编号 |
| `{issue_title}` | GitHub Issue 标题 |
| `{issue_body}` | Issue 完整正文 |
| `{issue_comments}` | Issue 所有评论按时间顺序拼接 |
| `{existing_prd_text}` | 已有关联 PRD 时的现有 PRD 全文（无则为空字符串） |
| `{repo_structure_summary}` | 仓库结构摘要 |

### PRD-from-Issue 配置示例

```toml
[agent_runner.generated_content.prd_from_issue]
enabled = true
mode = "agent"
output = "markdown"
agent = "auto"
timeout_seconds = 120
include_commit_log = false
include_diff_stat = false
body_template = [
  "# PRD: {issue_title}",
  "",
  "- GitHub Issue: #{issue_number}",
  "",
  "## 1. Introduction & Goals",
  "",
  "{issue_body}",
  "",
  "## 2. Requirement Shape",
  "",
  "- **Actor**: User",
  "- **Trigger**: TBD",
  "- **Expected Behavior**: TBD",
  "- **Scope Boundary**: TBD",
  "",
  "## 3. Acceptance Checklist",
  "",
  "- [ ] Define requirements",
  "- [ ] Implement the feature",
  "- [ ] Run verification",
]
prompt = [
  "You are a technical product manager. Write a comprehensive PRD in Markdown format.",
  "",
  "GitHub Issue #{issue_number}: {issue_title}",
  "",
  "Issue Body:",
  "{issue_body}",
  "",
  "Issue Comments (chronological):",
  "{issue_comments}",
  "",
  "Existing PRD (to be rewritten if present):",
  "{existing_prd_text}",
  "",
  "Repository Structure Summary:",
  "{repo_structure_summary}",
  "",
  "Output only the PRD markdown, no extra commentary.",
]
```

## Issue Comment Event Markers

每个关键状态变化都会向 Issue 写入结构化 Markdown comment，并带隐藏 `iar:event` marker：

```markdown
<!-- iar:event version=1 phase=pre_pr_review cycle=1 head=abc123 -->

## Agent Runner Pre-PR Review

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
- `pre_pr_review`
- `draft_pr_created`
- `publish_recovered`
- `post_pr_supervisor`
- `post_pr_rework_requested`
- `rebase_repair_complete`
- `validation_passed`
- `validation_reset`

## Realistic Validation 证据门禁

PRD 的 Realistic Validation 默认**必须由执行 agent 实跑**，并由人工基于真实证据（截图/输出）签收后才允许合并 PR。完整链路：

```text
iar issue create        PRD 含 Realistic Validation 清单
                        → 物化为 <!-- iar:structured-evidence version=1 language="zh-CN" --> marker
                        → agent 解析每个 item 的格式要求（截图/pdf/txt等）
                        → 物化为 <!-- iar:evidence-format item=N kind=xxx --> marker
                        → Issue body 追加 "## Realistic Validation" 未勾清单
                        （PRD 显式声明 "Validation Waiver: <理由>" 时改为物化
                          <!-- iar:validation-waived --> marker，跳过证据要求；
                          配置 structured_evidence = false 时省略 structured marker）

agent 执行              prompt 强制要求实跑验证计划，证据写入 worktree 的
                        .iar/evidence/（runner 在 worktree 创建时写入
                        git info/exclude，证据永远进不了代码 diff）。
                        带 iar:structured-evidence marker 的 Issue 还必须写
                        .iar/evidence/evidence.json manifest，按 checklist item
                        分组描述命令、关键输出摘要、解释、风险及关联证据文件。

commit 前门禁           要求验证但证据与清单不匹配 → 进入 recovery，
                        重试耗尽后 agent/failed。

                        带 iar:structured-evidence marker 的 Issue 额外校验：
                        - evidence.json 存在且 version = 1
                        - language 与 marker 一致
                        - 每个 checklist item 有且仅有一个 evidence block
                        - 必填字段非空：item_number、item_name、command、
                          evidence_files、output_summary、explanation、risks
                        - evidence_files 中的每个文件存在，且命名匹配
                          rv-<item_number>-* 或 rv-<item_number>.*
                        - runner 计算每个证据文件的 SHA-256

                        未带 marker 的 Issue 保持原有行为：
                        - .iar/evidence/ 非空
                        - 第 n 个清单条目必须有 rv-<n>-* 证据文件
                        - Issue body 含 iar:evidence-format marker 时，
                          按 marker 的 kind 检查后缀（优先于正则匹配）
                        - 无 marker 时回退到正则关键词匹配：
                          截图/screenshot → 图片、pdf → .pdf、txt → .txt/.log、
                          word → .doc/.docx、excel → .xls/.xlsx、csv → .csv、
                          录屏/视频 → .mp4/.mov/.webm/.gif
                        逐项对账可关（关闭后仅要求证据目录非空）：
                        - 全局：validation.evidence_format_check = false
                        - 按任务：PRD 在 Realistic Validation 小节写
                          "Evidence Format Waiver: <理由>"，物化为
                          iar:evidence-format-waived marker

publish                 - diff 混入证据路径 → 拒绝 push（双保险）
                        - PR body 末尾追加 marker 包裹的人工签收清单
                        - 证据经 git plumbing 推送到 orphan 分支
                          iar-evidence/issue-<N>（无父提交、永不合并）
                        - PR 上发证据评论：
                          - 带 structured marker：按 RV-1 / RV-2 分组展示
                            命令、证据文件、SHA-256、关键输出摘要、解释、风险
                          - 未带 marker：按文件名平铺（图片内联 blob 链接、
                            文本内联引用）

人工验收                reviewer 查看 PR 证据评论，对照清单逐项核实后，
                        直接在 PR body 点击 checkbox 打勾。
                        验收重点：命令可复现、输出摘要合理、解释成立、
                        风险说明充分、SHA-256 与本地复现结果一致。

相关 marker 一览（均为 `<!-- iar:... -->` 隐藏注释）：

| Marker | 位置 | 含义 |
|---|---|---|
| `iar:structured-evidence version=1 language="..."` | Issue body | 要求该 Issue 提供结构化 evidence.json manifest |
| `iar:validation-waived reason="..."` | Issue body | operator 显式豁免，跳过证据要求 |
| `iar:evidence-format-waived reason="..."` | Issue body | 按任务关闭逐项格式对账（证据仍必须存在） |
| `iar:evidence-format item=N kind=xxx` | Issue body | agent 解析的格式要求标记（优先于正则） |
| `iar:realistic-validation version=1 total=N` … `iar:realistic-validation-end` | PR body | 人工签收清单区块边界 |
| `iar:validation-evidence version=1 head=<sha> branch=<branch> count=N` | PR comment | 证据评论锚点（head 用于检测勾选后漂移） |
| `iar:event phase=validation_passed` | Issue comment | 人工签收完成审计（按 head 去重） |
| `iar:event phase=validation_reset` | PR comment | 签收因新 push 失效被重置 |

配置（`config.toml` 或 `.iar.toml`）：

```toml
[agent_runner.validation]
enabled = true                    # 关闭后整套门禁退化为不启用
evidence_dir = ".iar/evidence"    # worktree 内证据目录（info/exclude 本地排除）
branch_prefix = "iar-evidence/"   # orphan 证据分支前缀
evidence_format_check = true      # 逐项格式对账；false 退化为仅要求证据非空
parse_evidence_format_with_agent = true  # 用 agent 解析格式要求；false 只用正则
language = "zh-CN"                # 证据 prompt / PR 评论固定标签语言
structured_evidence = true        # 为新的 Realistic Validation Issue 物化 structured marker

[agent_runner.labels]
validation_pending = "validation/pending"
validation_passed = "validation/passed"
```

语言配置只使用现有 TOML 配置体系（`config.toml` / `.iar.toml` 的 `[agent_runner.validation]`），不引入新的 `.iar/config` 文件，避免配置漂移。项目级默认语言写在 `config.toml`，单个仓库可通过 `.iar.toml` 覆盖。

### Structured evidence manifest 格式

带 `iar:structured-evidence` marker 的 Issue 必须在 `.iar/evidence/evidence.json` 提供如下 manifest：

```json
{
  "version": 1,
  "language": "zh-CN",
  "items": [
    {
      "item_number": 1,
      "item_name": "Run worktree preparation 真实验证",
      "command": "uv run pytest tests/test_run_agent.py -k \"worktree_reconcile\" -v",
      "evidence_files": ["rv-1-worktree-reconcile.txt"],
      "output_summary": "pytest 目标用例通过，输出显示 run_once 在 agent 执行前完成远程分支对齐。",
      "explanation": "该用例使用真实 Git 仓库与裸远程，覆盖 remote-tracking ref 与 fast-forward 判定，因此能证明工作树准备路径生效。",
      "risks": "GitHub 与 agent 边界为 fake；该证据不证明 live GitHub API 可用。"
    }
  ]
}
```

规则：

- `version` 必须为 `1`。
- `language` 必须等于 Issue marker 与 config 中的语言。
- `items` 必须覆盖 Realistic Validation checklist 的全部 item，每个 item 出现一次。
- 每个 item 必填字段：`item_number`、`item_name`、`command`、`evidence_files`、`output_summary`、`explanation`、`risks`。
- `evidence_files` 可有多个文件；每个文件必须存在于 `.iar/evidence/`，且文件名匹配 `rv-<item_number>-*` 或 `rv-<item_number>.*`。
- runner 在渲染 PR comment 时重新计算每个证据文件的 SHA-256，展示短 hash 与完整 hash。

### Reviewer 验收流程

1. 在 PR evidence comment 中按 `RV-1 / RV-2` 找到对应 checklist item。
2. 复现 `可复现命令`，确认输出与 `关键输出摘要` 一致。
3. 阅读 `为什么能证明该检查点成立`，判断解释是否合理、无逻辑跳跃。
4. 阅读 `潜在风险 / 不适用说明`，确认已知边界已被披露。
5. 本地计算证据文件 SHA-256，与 comment 中 runner 计算的 hash 核对。
6. 全部确认后在 PR body Realistic Validation checklist 中勾选对应项。

**branch protection 配置（一次性，operator 手动）**：GitHub 仓库 Settings → Branches → 对 `main` 添加/编辑 protection rule → Require status checks to pass → 勾选 `Realistic Validation sign-off`。配置后未全勾的 PR 物理无法合并；在 PR body 点勾会触发 `pull_request: edited` 事件自动重跑 check。

注意事项：

- 私有仓库中证据评论的内联图片可能不渲染，点击评论中的 `Open image` / `Open file` 链接进入 blob 页查看。
- 证据分支与代码历史零共同祖先（`git log iar-evidence/issue-<N>` 只有一个无父提交），永不合并；Issue 关闭后由 daemon 轮询清理。
- 其他目标仓库使用该能力时，需要把 `validation-gate.yml` 复制到该仓库的 `.github/workflows/` 并配置 required check。
- PRD 侧规范：Realistic Validation 默认必做；只有 operator 确认的 PRD 才允许在 `### Realistic Validation` 小节写 `Validation Waiver: <理由>` 行，`iar issue create` 会将其物化为豁免 marker。同一小节写 `Evidence Format Waiver: <理由>` 行则只关闭该任务的逐项格式对账（证据仍必须存在），物化为 `iar:evidence-format-waived` marker。

## 安全边界

- `auto_merge` 固定为 `false`，不会自动合并 PR
- `iar labels sync` 只同步 GitHub labels，不校验发布 remote；`iar run` 在领取 Issue 前会校验 `[agent_runner.git].remote` 必须存在，不存在时直接失败并列出当前可用 remote
- 发布变更前会检查 `forbidden_path_patterns`，拒绝匹配的文件变更
- Agent 执行在隔离 worktree 中进行，不影响主工作区
- Agent 不直接执行 `git add` 或 `git commit`；完成修改后写入 `.agent-runner/commit-request.json` 请求 runner 在 host 侧提交
- `commit-request.json` 必须提供 `commit_message`；pre-PR reviewer 可额外提供 `verdict`、`summary` 和 `findings_*` 元数据作为空提交兜底。runner 会校验当前 branch 未变化、删除请求文件、检查 `forbidden_path_patterns`，再执行 `git add -A` 和 `git commit`
- 不同仓库应在 `verification_commands` 中配置自己的验证命令，例如 `just test`、`npm test`、`pnpm lint` 或 `make test`
- runner 会在提交前先运行一次 `verification_commands`；发现未提交变更并执行 `git add -A` 后，会再次运行同一组验证命令，覆盖依赖 staged 状态的 commit hook 或测试标记
- 如果验证过程中的 formatter 或 lint 自动修复了已跟踪文件，runner 会在安全路径校验后用 `git add -u` 同步这些 tracked 修改，避免 `.last_tested_commit` 指向 working tree 而 commit hook 检查到过期 staged tree
- Agent CLI 非零退出或任一验证失败时，runner 最多按 `max_recovery_attempts` 重新调用同一个 Agent；每次 recovery 前会等待 `recovery_retry_delay_seconds` 秒，并把失败摘要或失败命令的 exit code、stdout、stderr 放入 recovery prompt；Agent 修复后仍只能写 commit request，不能直接提交
- Runner 通过 `classify_failure` 对每次尝试进行分层失败识别，覆盖 `UNCOMMITTED_CHANGES`、`NO_COMMITS`、`VERIFICATION_FAILED`、`AGENT_ERROR`、`UNRECOVERABLE` 等类型；不可恢复错误（如安全路径拦截）会立即终止 retry loop
- 每轮尝试的结果都会记录在 `AttemptResult` 中，最终 Issue comment 包含「Attempt History」表格，展示 attempt_number、failure_type、recovered 状态，便于人工 review 时追踪 Agent 的修复轨迹；Detail 列取每次失败输出的最后一行有效内容（实际报错几乎总在末尾），而不是从头截断的样板文字
- 失败评论会识别已知错误签名：命中 Claude API 用量限额（429 / usage limit）时，在评论顶部输出加粗的 Root cause 摘要并带上限额重置时间；`CalledProcessError` 的命令回显只保留命令名（如 `claude`），不会把完整 agent prompt 打进评论
- 如果 Agent 没有产生任何新 commit 且工作区也没有未提交变更，runner 仍会将 Issue 标记为 `agent/failed`
- Pre-PR reviewer 的修改同样必须通过 `verification_commands` 才能发布
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

## Agent Runner 日志与 Issue 上下文

Agent Runner 在启动和结束一次 agent 执行时，会显式记录当前处理的 GitHub Issue 编号和完整 URL：

```text
Starting agent for Issue #23: https://github.com/zata-zhangtao/fsense/issues/23
Agent finished for Issue #23: https://github.com/zata-zhangtao/fsense/issues/23 (exit_code=0)
```

对于运行时间超过心跳阈值（默认 60 秒）的长命令，process runner 的 watchdog 心跳日志也会携带 Issue 上下文：

```text
Claude stream (Issue #23: https://github.com/zata-zhangtao/fsense/issues/23) still running after 60s: claude --dangerously-skip-permissions ...
```

这样即使命令摘要因 `_summarize_command` 的 240 字符限制被截断，Issue URL 仍以独立字段完整保留，便于从日志直接定位当前处理的 Issue。

### 实现要点

- `IProcessRunner.run` 新增可选 `label` 参数，例如 `"Issue #23: https://github.com/..."`。
- `run_agent_once.run_agent_with_prompt` 在有 `IssueSummary` 时记录启动/结束日志，并把 `issue.number` 与 `issue.url` 作为 `label` 传给 process runner。
- `_ProcessWatchdog` 将 `label` 附加到原有 base label 之后，无 `label` 时保持原有日志格式不变。

## Agent Runner Monitoring Dashboard

Dashboard 路由 `/dashboard`（即 `frontend/src/pages/dashboard-page.tsx`）展示 Agent Runner 的监控视图。运维者打开 Web 就能看到当前队列、PR 状态、事件时间线和异常。监控 API 本身保持只读；写操作（重试 failed、继续 blocked、启停 runner 进程等）由独立的管理终端 API 承载，见下文「Agent Runner 统一管理终端（Operations Console）」一节。

> 历史注记：监控面板最初按"只读、无数据库、无进程管理"交付
> （`tasks/archive/20260524-162356-prd-agent-runner-operations-console.md`）。
> 这三条约束已被统一管理终端 PRD 显式取代。

### 端点

监控面板复用两个只读 API：

- `GET /api/v1/agent-runner/overview` — 按仓库返回健康、队列统计、Issue 摘要、最近事件和异常计数。
- `GET /api/v1/agent-runner/issues/{issue_number}` — 单个 Issue 的 label、PR context、worktree 状态、event timeline、anomalies 和建议 CLI 命令。

两个端点都只读，不暴露任何修改 GitHub label、comment、PR 或 worktree 的能力。

### 异常检测

后端按以下规则对每个 Issue 推导异常，每条异常都带 `severity`、`message` 和 `suggested_cli`：

| 异常类型 | 触发条件 | severity | 推荐 CLI |
|---|---|---|---|
| `label_pr_mismatch` | PR 已创建但 Issue label 不在 `agent/supervising` / `agent/review` / `agent/blocked` / `agent/failed` 中 | warning | `iar labels sync`、`iar review --dry-run` |
| `pr_dirty_in_review` | PR `mergeable_state` 为 dirty/conflicted 且 label 是 `agent/review` | error | `iar review`、`iar run --max-issues 1` |
| `dirty_worktree_mismatch` | worktree 有未提交变更但 label 不是 `agent/running` | warning | `iar run --dry-run`、`git status` |
| `event_label_mismatch` | 最新 `iar:event` phase 隐含的状态与当前 label 不一致 | warning | `iar labels sync` |

Overview 还会按 severity 汇总 `anomaly_count` 和 `anomaly_summary`（`warning` / `error`），并把 `has_anomaly` 标在对应 Issue 行上。

### 事件时间线

`GET /api/v1/agent-runner/issues/{issue_number}` 返回 `timeline`，按时间顺序列出所有 `<!-- iar:event ... -->` 标记，附带 phase、cycle、head SHA、PR branch、checks_state、mergeable、action 等字段。解析复用 `backend.core.use_cases.agent_runner_events`，不会复制 marker parser。

### 建议 CLI 文本

每个 Issue 详情区都会列出当前状态推荐的 `iar` 命令文本（如 `iar review`）。命令旁有**复制**按钮，但**不直接执行**——所有恢复动作仍走 CLI，保留操作审计、避免 UI 端任意 shell。

### 显式非目标

监控 API（`/overview`、`/issues/{n}`）本身保持只读：

- 不暴露任何修改 label、comment、PR、worktree 的 API。
- 不执行任意 shell 命令、不能从 UI 改 label 或触发 agent。
- 不替代 `iar run` / `iar review` / `iar labels sync` 等恢复命令。
- 不新增数据库、后台任务队列或 WebSocket；GitHub label/comment/PR 和本地 worktree 仍是事实来源。
- 不实现自动 rebase 冲突解决；冲突的 Issue 会带 `agent/blocked` 状态出现在监控面板，由人类决定下一步。

写操作统一收敛在管理终端 API（白名单动作 + 审计），见下一节。

## Agent Runner 统一管理终端（Operations Console）

管理终端把多项目的 Agent Runner 运维收敛到一个 Web 界面，四个页面：

| 页面 | 路由 | 能力 |
|---|---|---|
| 总览 | `/dashboard` | 队列监控（原有）+ 每仓库完成度摘要 + failed/blocked Issue 的重试/继续按钮 |
| 进程 | `/processes` | 启停每个仓库的 runner 进程，实时查看进程日志（offset 轮询） |
| 统计 | `/stats` | 实时完成度（GitHub 口径）+ 历史趋势与最近运行记录（本地 SQLite 口径） |
| 项目 | `/repositories` | 仓库 registry 列表 / 添加 / 启停（写回 `config.toml`）+ 审计日志 |

### 信任边界与白名单动作

管理终端按**本机单用户部署**信任边界运行：`/api/auth/*` 返回固定的
本地 operator 会话，不做真实认证。所有写操作只能映射到硬编码白名单
动作，后端从枚举构建命令参数，**永不接受 UI 传入的原始命令字符串**：

| 动作 | 语义 |
|---|---|
| `start_daemon` / `start_review_daemon` | 为某仓库启动常驻 runner 进程（同仓库同类型只允许一个） |
| `run_once` / `review_once` | 启动一次性托管子进程 |
| `stop_process` | SIGTERM 停止托管进程，超时升级 SIGKILL |
| `retry_failed` | 把 failed Issue 的 label 翻转回 ready（与手工操作等价） |
| `blocked_continue` | 启动一次性 `iar blocked-continue` 托管子进程 |
| `registry_add` / `registry_set_enabled` | registry 写回（路径必须存在且为 git 仓库） |

故意不支持：任意 shell 命令、任意 label 编辑、PR merge、worktree 删除。
这些要么风险不可枚举（任意 shell），要么会绕过 workflow 状态机
（任意 label），要么属于必须人工签收的决策（merge）。

所有写操作（含被拒绝的）都会写入审计日志，可在「项目」页或
`GET /api/v1/agent-runner/console/audit` 查看。

### 进程托管与多项目并发

管理终端按 `(repo_id, kind)` 托管 runner 子进程。**每个仓库一个
daemon 进程**即获得多项目并发——不同仓库的 Issue 同时执行，互不阻塞
（CLI 的 `iar daemon` 在 cwd 命中唯一已初始化注册仓时只监控该仓，未命中、未初始化或匹配多个时报错；显式 `--all` 时才监控所有 enabled registry entries，但在单个进程内串行轮询）。

- 子进程以 `start_new_session` 脱离后端进程组：后端重启不影响执行中
  的 runner；重启后从 pidfile registry（`~/.iar/processes.json`）复活
  记录并重新探活。
- 子进程默认以 `uv run iar <command> --repo-id <id>` 启动、cwd 为
  keda 项目根——全局安装的 `iar` 读不到项目本地 `config.toml`，
  必须经 `uv run`。命令前缀可用 `[agent_runner.console] runner_command`
  覆盖。
- 进程 stdout/stderr 写入 `logs/agent-runner/processes/<repo_id>/`，
  面板通过 offset 轮询续读，无 WebSocket/SSE。

### 运行历史与完成度统计

- **实时口径（GitHub）**：`GET /api/v1/agent-runner/console/stats/overview`
  以 `state=all` 查询全部 workflow label 的 Issue 并去重。closed 且不含
  failed/blocked label 计为 `completed`；单 label 查询命中 200 上限时
  响应标记 `truncated: true`。
- **历史口径（本地 SQLite）**：`run_once` 编排在每个 Issue 处理收尾时
  写入一条运行记录（outcome：completed / failed / blocked），CLI 直跑
  与面板托管共用 `~/.iar/console.db`（`history_db_path` 可配）。
  `GET .../console/stats/history` 返回按天聚合趋势。

SQLite 只是旁路记录，**不参与 workflow 状态机决策**——GitHub
labels/comments/PR 与本地 worktree 仍是唯一事实来源；落库失败只产生
日志警告，不会阻断 runner。

### 项目接入

`config.toml` 的 `[agent_runner.repositories.*]` 仍是项目接入的唯一
事实来源。「项目」页通过 tomlkit 做 round-trip 写回（保留注释与
格式），添加前校验 repo_id 格式（`^[a-z0-9][a-z0-9-]*$`）、路径存在
且为 git 仓库。某个已注册路径失效时，监控与统计会跳过该仓库并在
总览页给出醒目警示，不会拖死整个面板。

### Console API 一览

```text
GET    /api/v1/agent-runner/console/processes
POST   /api/v1/agent-runner/console/processes                  {repo_id, kind}
POST   /api/v1/agent-runner/console/processes/{id}/stop
GET    /api/v1/agent-runner/console/processes/{id}/logs?offset=0
POST   /api/v1/agent-runner/console/repositories/{repo}/actions             {action}
POST   /api/v1/agent-runner/console/repositories/{repo}/issues/{n}/actions  {action}
GET    /api/v1/agent-runner/console/stats/overview
GET    /api/v1/agent-runner/console/stats/history?repo_id=&days=30
GET    /api/v1/agent-runner/console/runs?repo_id=&limit=100
GET    /api/v1/agent-runner/console/audit?limit=100
GET    /api/v1/agent-runner/repositories
POST   /api/v1/agent-runner/repositories                       {repo_id, path, display_name}
PATCH  /api/v1/agent-runner/repositories/{repo_id}             {enabled}
```

### 配置

```toml
[agent_runner.console]
history_db_path = "~/.iar/console.db"            # 运行历史与审计 SQLite
process_registry_path = "~/.iar/processes.json"  # 托管进程 pidfile
process_log_dir = "logs/agent-runner/processes"  # 进程日志目录（相对 keda 根）
runner_command = ["uv", "run", "iar"]            # 托管进程启动命令前缀
stop_timeout_seconds = 30                        # SIGTERM → SIGKILL 等待秒数
```

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

若任一参与 agent 或 synthesizer 子进程返回非 0 退出码，默认行为不再整体失败，而是记录失败并继续；`--strict` 可恢复为失败即非 0。

> **失败隔离与回退**：从本 PRD 起，`iar deliberate` 对单个 agent 失败默认隔离并继续。
> - 失败 agent 的 `workspaces/<profile_id>/round-<n>-output.md` 保留 partial 输出。
> - TTY 下会提示选择回退模型，5 分钟无选择自动切换到下一个可用模型。
> - 非 TTY / CI 下直接自动切换，不阻塞。
> - 使用 `--strict` 或设置 `continue_on_agent_error = false` 时，任一 agent 失败即让 CLI 返回非 0。
> - 全部参与 agent 和 synthesizer 均失败时，无论是否 `--strict` 都返回非 0。
>
> 注意：默认 `skeptic` profile 使用 `kimi`，此前 `kimi` deliberation 命令错误地传递了 `--quiet`，`kimi` CLI 不支持该选项。修复后 `kimi` 命令为 `kimi --input-format text`。

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
# 是否对单个 agent 失败继续执行（默认 true）
continue_on_agent_error = true
# TTY 下等待用户选择回退模型的超时秒数（默认 300）
agent_failure_timeout_seconds = 300

[agent_runner.deliberation.profiles.architect]
agent = "claude"
role = "architect"
behavior_prompt = "You are an experienced software architect..."

[agent_runner.deliberation.profiles.skeptic]
agent = "kimi"
role = "skeptic"
behavior_prompt = "You are a skeptical reviewer..."
```

`session.json` 新增 `failed_agents` 字段，记录失败的 `profile_id`、尝试的 agent、最终回退 agent（如有）和失败原因。

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

## `iar ask` 自然语言决策入口

`iar ask` 是受限自然语言决策入口，默认只生成计划并写入审计文件，不产生任何副作用。

### 基本用法

```bash
# 默认只输出计划
uv run iar ask "帮我判断现在应该创建 issue 还是启动任务"

# 显式选择 planner agent（默认 codex）
uv run iar ask "从 pending PRD 中挑一个最适合创建 issue 的任务" --agent codex

# 只打印计划，适合 CI 或脚本验证
uv run iar ask "现在可以跑一个 ready issue 吗" --plan-only

# 进入确认执行流程（TTY 中要求输入 decision_id）
uv run iar ask "从 tasks/pending/example.md 创建 issue" --execute

# 非交互执行（仅允许 low/medium 风险动作）
uv run iar ask "运行一次 dry-run 看看 ready 队列" --execute --yes
```

### 权限边界

- **白名单动作**：`show_status`、`run_deliberation`、`create_issue_from_prd`、`mark_issue_ready`、`run_once_dry_run`、`run_once`、`review_once_dry_run`、`review_once`、`needs_clarification`、`no_op`
- **禁止动作**：`git_push`、`git_merge`、`git_reset`、`daemon`、`review-daemon`、任意 shell 命令、自动 merge、直接关闭 Issue、删除分支等
- **Planner 安全**：只读 planner 必须通过可验证只读命令运行；目前仅 `codex` 被验证为安全（使用 `--sandbox read-only --ask-for-approval never`）。`claude` 和 `kimi` 会 fail fast。

### 确认策略

- `--execute` 在 TTY 中要求输入 `decision_id` 确认写操作
- `--execute --yes` 只允许 low/medium 风险且允许非交互确认的动作
- 高风险动作（如 `run_once`）不允许 `--yes`，必须 TTY 交互确认

### 审计文件

每次计划写入 `logs/agent-runner/decisions/<decision_id>/`：

- `plan.json`：结构化计划
- `plan.md`：人类可读的计划摘要
- `context-summary.json`：决策上下文摘要

执行时还包含：

- `execution.json`：执行结果
- `execution.md`：执行摘要


## 路线图（Roadmap）

管理终端提供 `/roadmap` 页面，以 PRD 文件为粒度展示 `tasks/pending/` 与 `tasks/archive/` 中的任务全景。

### 视图说明

- 默认只显示 `pending` PRD，勾选「显示已归档」后同时展示 `archived` PRD。
- 每个 PRD 卡片展示：标题、当前状态、验收清单进度、关联 Issue、依赖关系与下一步操作。
- 列表视图按优先级（P0 → P3）与更新时间排序；时间轴视图在后续版本中提供。

### 状态映射

PRD 的 GitHub Issue label 被映射为统一状态：

| 状态 | 来源 |
|---|---|
| 未开始 | 无 Issue 或 Issue 无 workflow label |
| 就绪 | `agent/ready` |
| 运行中 | `agent/running` |
| 监督中 | `agent/supervising` |
| 待审阅 | `agent/review` 或存在 open PR |
| 失败 | `agent/failed` |
| 阻塞 | `agent/blocked` |
| 已合并 | Issue 关闭且 PR 已合并 |
| 已归档 | PRD 位于 `tasks/archive/` |
| 等待中 | 依赖未满足 |

### 单个开始

点击 PRD 卡片上的「开始」按钮后，后端会：

1. 若 PRD 无 Issue，调用 `create_issue_from_prd` 的安全路径（`publish_prd=True, queue_ready=True`），在 PRD 成功发布到 base branch 后添加 `agent/ready`。
2. 若 PRD 已有 Issue，直接添加 `agent/ready` 并移除 `agent/failed`。
3. 启动一次 `iar run` 托管进程。

### 全局调度

在控制面板设置并发数（1–10）后点击「全局开始」：

- 系统扫描所有无依赖且可安全进入 ready 的 pending PRD。
- 按优先级排序，同时启动最多 N 个 PRD。
- 超出槽位的 PRD 进入 `roadmap_queue` 等待队列。
- 点击「停止全局调度」可清空等待队列，已运行的进程不会被中断。

### 依赖等待

PRD 的 `Delivery Dependencies` 小节会解析为三种依赖边：

- `Depends on tasks/issues: #42`：等待上游 Issue 关闭。
- `Depends on tasks/issues: tasks/pending/xxx.md`：等待上游 PRD 合并或归档。
- `Depends on tasks/issues: tasks/archive/xxx.md`：视为已完成上游；默认 pending 视图不展示 archived PRD，但仍会用它们解析依赖。
- `Depends on groups: infra`：等待该 group 下所有 Issue 关闭。

存在未满足依赖的 PRD 显示为「等待中」并给出阻塞原因；无法解析的 PRD 引用显示为「依赖未解析」；形成环的依赖会标红提示修正。

## Idea Inbox（想法采集 + 草稿审阅）

`/ideas` 页面把现有的 `tasks/inbox/ideas.md` 原话日志接入管理终端，作为 PRD 路线图的上游：用户先在 inbox 累积想法，AI 生成 PRD 草稿，人确认后才落入 `tasks/pending/`。该能力复用 `agent-runner` 命名空间下的仓库 registry 与 `IContentGenerator`，不引入第二个项目映射表。

### 事实源与目录约定

- `tasks/inbox/ideas.md` 是 append-only 原话日志，AI 永不重写已有条目（仅在末尾追加 `## YYYY-MM-DD HH:MM · <source> · <author> (idea-id)` 块）。
- `tasks/inbox/summary.md` 是 AI 派生的可重写总结，刷新时整文覆盖并在文件头明确标注「事实以 `ideas.md` 为准」。
- `tasks/inbox/prd-drafts/` 存放待审阅的 PRD 草稿；文件名 `<YYYYMMDD-HHMMSS>-<slug>.md`，草稿顶部 metadata 块（`Draft Status: pending-review|approved|rejected`、`Source Idea Refs:` 等）由 `core/use_cases/idea_prd_drafts.py` 注入。
- 草稿经人在 `/ideas` 页面确认后复制到 `tasks/pending/`，命名为 `<PRIORITY>-<TYPE>-<YYYYMMDD-HHMMSS>-<slug>.md`；草稿状态同步改为 `approved`，并把目标 pending 路径写回 metadata。

### API 端点（`/api/v1/agent-runner/idea-inbox/*`）

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/repositories/{repo_id}` | 读取 inbox 快照（ideas / summary / drafts） |
| `POST` | `/repositories/{repo_id}/ideas` | 前端追加想法到 `ideas.md` |
| `POST` | `/repositories/{repo_id}/summary/refresh` | 重写 `summary.md` |
| `POST` | `/repositories/{repo_id}/drafts` | 生成 PRD 草稿到 `prd-drafts/` |
| `POST` | `/repositories/{repo_id}/drafts/{encoded}/approve` | 草稿确认入 pending |
| `POST` | `/inbound` | 外部 IM / webhook 通用入站（签名校验） |
| `GET` | `/metadata` | 列出可用 priority / type 与 inbound 配置 |

### 跨平台接入（inbound）

`POST /api/v1/agent-runner/idea-inbox/inbound` 接受 provider-neutral 负载：

```json
{
  "provider": "feishu",
  "repo_id": "keda-main",
  "sender": "user-or-open-id",
  "text": "想法原文",
  "occurred_at": "2026-06-15T20:15:00+08:00"
}
```

安全要求：

1. 请求必须携带 `X-IAR-Signature` header，值为 `sha256=<HMAC_SHA256(secret, raw_body)>`。secret 来自环境变量 `IAR_IDEA_INBOX_INBOUND_SECRET`，**不写入** `config.toml` / `.iar.toml`。
2. `repo_id` 必须显式给出并能在 registry 中解析；缺少或被禁用则返回 `400`。
3. Feishu 等 provider adapter 只做 payload 转换：消息永远先写入 `ideas.md`（不会被猜项目路由到默认仓库，也不会直接创建 pending PRD）。如果消息只携带 `@项目名` 而没有 `repo_id`，adapter 应当返回明确错误或把消息放进 `unassigned` 队列；当前实现要求 `repo_id` 显式。

签名计算的 body 必须是 HTTP 请求的原始字节（不要重新序列化后再 HMAC），以免空格 / 字段顺序差异导致签名失败。

### 安全边界与已知限制

- secrets 不进 `config.toml` / `.iar.toml`；inbound secret 走环境变量。
- 草稿 AI 生成复用 `IContentGenerator` / `generated_content.py` 模式，不新增 LLM SDK；当 generator 不可用时草稿退化为带原话摘录的 fallback 模板，待人补全。
- Idea → Draft → Pending 三段是单向流：草稿不会自动跑 runner，必须等人在 `/ideas` 确认才进入 pending。
- 飞书自定义机器人 webhook 主要用于向群发送消息，不应被当作入站通道；事件订阅或自建通用 inbound 才是正确路径。
