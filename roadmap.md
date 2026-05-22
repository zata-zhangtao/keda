# Roadmap

## Vision

构建一个面向人工调度的 AI 工程交互终端。项目本身不自动决定哪些 issue 开始、哪些 issue 暂停，而是在人工选择任务后，辅助完成需求理解、代码修改、验证、code review、提交 pull request，并在主分支更新后维护已提交 PR 的 rebase 状态。

## Product Boundary

- 当前项目提供交互终端能力，不提供全自动 issue 调度系统。
- 是否开始处理某个 issue 由人工通过 label 控制（添加 `agent/ready` label，并可结合 `agent/claude`、`agent/codex` 或 `agent/kimi` 指定执行终端）。
- 用户可以直接提交没有 PRD 的 Issue，但这类 Issue 只能进入需求澄清、合议和 PRD 审批流程；在管理员确认前不得被 runner 当作可执行任务领取。
- 用户提交 Issue 时有义务尽量把需求、问题背景、复现信息、期望结果和已知约束描述清楚；如果描述不清楚，AI 必须先在 Issue 中反问用户，而不是自行补全关键需求。
- 用户可以在 Issue 中上传图片作为需求上下文，例如界面截图、错误截图、流程图或设计稿；AI 在澄清、合议和 PRD 草稿生成时应把这些图片视为 Issue 上下文的一部分。
- 终端可以展示 issue 信息、辅助分析任务和执行工程动作，但不会主动处理未标记的 issue。
- 一旦 issue 被标记为 `agent/ready`，后续执行链路（修改代码、验证、review、提交 PR、维护 PR 分支）应自动完成，无需逐环节人工授权。
- 当前实现优先支持 GitHub Issues / Labels / Pull Requests；其他代码托管平台暂不在当前交付边界内。

## Current Status

截至 2026-05-22，项目已经从概念验证推进到单仓库、CLI-first 的本地 agent runner。当前状态依据来自 `src/backend/` 实现、`docs/guides/agent-runner.md`、`tasks/archive/` 已归档 PRD 和 `tasks/pending/` 待完成 PRD。

### Completed

- **CLI 基础能力已落地**：`iar labels sync`、`iar issue-from-prd`、`iar run-once` 和 `iar daemon` 已接入项目脚本，并通过 `config.toml` 与环境变量配置。
- **人工准入队列已落地**：runner 只处理带有 `agent/ready` label 的 issue，不会主动挑选未标记任务；支持 `agent/codex`、`agent/claude`、`agent/kimi` 等 agent 路由 label。
- **标准 label 流转已落地**：支持同步和使用 `agent/ready`、`agent/running`、`agent/review`、`agent/failed`、`agent/blocked` 等队列状态。
- **PRD 到 Issue 的发布链路已落地**：`issue-from-prd` 能从 PRD 创建 GitHub Issue、回写 Issue URL，并支持 `--publish-prd` 在 ready 前只提交和推送目标 PRD 文件。
- **单仓库执行链路已落地**：`run-once` 能领取 ready issue、创建或复用 worktree、选择 agent、运行 agent、执行验证、推送分支并创建 Draft PR。
- **受限提交代理已落地**：agent 不直接 `git add` / `git commit`，而是写入 `.agent-runner/commit-request.json`，由 runner 在 host 侧完成受控提交。
- **本地验证和失败恢复已部分落地**：runner 支持配置化验证命令、失败输出摘要、有限 recovery loop、Claude stream-json 前台过滤和 recovery retry delay。
- **发布前安全检查已落地**：runner 会校验发布 remote、当前分支、禁止路径模式，并在失败时把 issue 标记为 `agent/failed`。
- **基础 API 状态端点已落地**：提供 agent-runner status 和 health 只读端点，用于暴露配置摘要和 `gh` 可用性。
- **文档与测试基础已落地**：已有 Agent Runner 使用指南、配置说明、架构规范、归档 PRD、pytest 覆盖和 `just test` 验证入口。

### Partially Completed

- **交互终端能力**：已有 CLI 和少量交互式提示，但还没有完整的 issue 浏览、任务选择、状态追踪和人工介入终端体验。
- **自动恢复能力**：agent 执行、验证和提交请求失败已有有限恢复；发布阶段失败后的显式 resume / recover 命令尚未完成。
- **worktree 管理能力**：集中式 worktree 路径和 issue 分组已完成；新 worktree 默认基于最新远程 base ref 仍在待办。
- **可观测性**：终端前台输出、Issue comment 和健康端点已具备；还缺少统一的执行事件时间线和可审计运行记录。
- **review 能力**：runner 会执行验证和安全检查，并把 PR 标记为 Draft；独立的自动 code review gate 尚未实现。
- **前端能力**：已有基础前端结构和页面骨架；面向 agent runner 的可用操作台尚未完成。

### Not Completed

- 多目标仓库配置、轮询和状态汇总。
- prompt template 与 phase 配置化。
- 发布失败后的 `recover-publish` / resume 命令。
- 新 worktree 默认使用最新远程 base ref。
- 多 agent 合议会话。
- 无 PRD Issue 的合议澄清、PRD 审批、PRD 落盘和 ready label 闭环。
- PR 创建后的自动 rebase、重新验证和状态维护。
- 自动 code review gate，以及高风险问题阻止 PR 发布的完整机制。
- 非 GitHub 平台适配层。

## Target Workflow

1. 用户可以直接创建 GitHub Issue，也可以先写 PRD 后通过 `iar issue-from-prd` 发布 Issue；直接创建 Issue 时，用户应尽量写清需求、问题、复现路径、期望结果和约束，并可上传图片附件补充说明。
2. `iar` 读取 Issue 正文、评论和图片附件等上下文，判断任务是否已有足够信息进入后续流程。
3. 如果 Issue 描述不清楚，AI 先在 Issue 中反问用户，等待用户补充关键需求；在关键问题未回答前，不创建 PRD、不添加 `agent/ready`。
4. 如果 Issue 已有关联 PRD 且需求明确，管理员通过添加 `agent/ready` label（可结合 `agent/claude`、`agent/codex` 或 `agent/kimi` 指定终端）标记 AI 介入。
5. 如果 Issue 没有关联 PRD 但描述已足够进入分析，`iar` 将其视为 intake candidate，只允许进入需求澄清和合议流程，不允许直接执行代码任务。
6. `iar` 调用多 agent 合议能力，对 Issue 背景、目标、风险、实现边界、验收标准和图片上下文进行讨论；公开 transcript、synthesis 和建议动作写回 Issue comment。
7. 管理员在 Issue 中决定是否需要创建 PRD；若不需要 PRD，则明确关闭、转人工或按轻量任务规则处理。
8. 管理员决定创建 PRD 后，`iar` 根据 Issue 与合议结果生成 PRD 草稿，并把草稿内容或草稿链接写回 Issue，等待管理员确认。
9. 管理员确认 PRD 后，`iar` 才把 PRD 真实写入仓库 `tasks/pending/`，在 PRD 与 Issue 之间建立双向链接，并把 Issue label 更新为 `agent/ready`。
10. runner 基于仓库现有架构与规范创建隔离 worktree，并把 issue、PRD 和执行规则传给 agent。
11. agent 修改代码、测试和必要文档；runner 通过受限 commit proxy 完成本地提交。
12. runner 执行配置化本地验证，失败时把日志摘要交回 agent 做有限次数恢复。
13. runner 做发布前安全检查，推送任务分支并创建 Draft PR。
14. 自动 code review gate 对最终 diff 做独立检查，识别潜在 bug、回归风险、缺失测试和文档同步问题。
15. 监听主分支更新，在 PR 分支落后时自动 rebase，并重新执行必要验证。
16. 在需求不明确、PRD 未确认、rebase 冲突、发布失败、验证失败或高风险 review 发现时安全停止，并输出明确的人工处理建议。

## Milestones

### M0: CLI And Queue Foundation

Status: Completed.

- 注册 `iar` CLI。
- 支持标准 label 同步。
- 使用 GitHub Issues / Labels 作为队列状态源。
- 通过 `config.toml` 管理 runner、labels、git、worktree 和 safety 配置。

### M1: Human-Gated Task Intake

Status: Partially completed.

- 已完成：通过 `agent/ready` label 和 agent 路由 label 控制 issue 是否进入 runner。
- 已完成：从 PRD 创建 Issue，并在 Issue body 中保留 canonical PRD 路径和验收摘要。
- 未完成：完整交互终端中浏览 issue、选择任务、追问需求和展示状态。
- 未完成：直接输入自然语言任务后生成可追踪 PRD / Issue 的完整闭环。
- 未完成：没有 PRD 的用户 Issue 自动进入 intake 候选池，并通过管理员决策转换为 PRD 草稿或转人工结论。
- 未完成：PRD 草稿在管理员确认前只写回 Issue，不落盘到仓库、不触发 `agent/ready`。
- 未完成：在 Issue 描述不清楚时自动生成面向用户的澄清问题，并等待用户补充后再继续。
- 未完成：读取和引用 Issue 图片附件，将截图、设计稿或流程图纳入澄清、合议和 PRD 草稿上下文。

### M2: Code Change Agent

Status: Partially completed.

- 已完成：runner 能创建或复用 issue worktree，并启动 Codex、Claude 或 Kimi。
- 已完成：prompt 会要求 agent 读取 `AGENTS.md`、遵守仓库规范、修改代码、测试和必要文档。
- 已完成：runner 通过 commit request 文件完成受限提交。
- 未完成：prompt template / phase 配置化，避免每次调整 prompt 都改 Python 代码。
- 未完成：任务前的结构化规划、影响面识别和验收标准校验仍主要依赖 agent 自身执行。

### M3: Verification And Review

Status: Partially completed.

- 已完成：自动运行配置化验证命令。
- 已完成：验证失败、agent CLI 异常、commit request 错误和零提交场景已有有限 recovery loop。
- 已完成：失败时把 issue 标记为 `agent/failed` 并写入失败 comment。
- 未完成：独立自动 code review gate。
- 未完成：把高风险 review 发现转换为阻止 PR 发布或转人工的稳定规则。
- 未完成：更细粒度的失败分类、恢复策略和审计时间线。

### M4: Pull Request Automation

Status: Partially completed.

- 已完成：自动推送任务分支并创建 Draft PR。
- 已完成：PR body 包含 `Closes #<issue-number>`，Issue comment 记录分支、PR URL 和验证结果。
- 已完成：成功后 issue 从 `agent/running` 流转到 `agent/review`。
- 未完成：PR 正文中的实现摘要、验证详情和残余风险仍不够完整。
- 未完成：发布失败后的幂等恢复命令。
- 未完成：创建 PR 前的独立 review gate。

### M5: Multi-Repository Runner

Status: Not completed.

- 支持在 `config.toml` 中声明多个目标仓库。
- `labels sync`、`run-once` 和 `daemon` 在未指定单一仓库时处理所有启用仓库。
- `issue-from-prd` 在多仓库配置存在时必须解析到唯一目标仓库。
- API 状态端点返回多仓库配置摘要和基础健康信息。

### M6: Main Branch Rebase Automation

Status: Not completed.

- 监听主分支更新事件。
- 检测已打开 PR 是否落后于主分支。
- 自动 rebase PR 分支并重新运行验证。
- 处理成功后更新 PR 状态，遇到冲突或测试失败时请求人工介入。

### M7: Deliberation And Review Intelligence

Status: Not completed.

- 支持多 agent 合议会话，用于需求澄清、方案争议和复杂设计评估。
- 支持只读 transcript、最终 synthesis 文件和终端实时输出。
- 支持把面向 Issue 的合议 transcript、关键分歧、推荐结论和后续动作写回 Issue comment。
- 把合议能力与 issue runner 解耦，第一版不修改代码、不创建 branch、不创建 PR。
- 后续把合议结果接入 task intake 或 code review，但不暴露隐藏思维链。

### M8: Issue-First PRD Gate

Status: Not completed.

- 支持用户直接提交没有 PRD 的 GitHub Issue，并由 `iar` 识别为不可直接执行的 intake candidate。
- 要求无 PRD Issue 在进入合议前具备基本需求描述；如果关键信息缺失，AI 先写入澄清问题并等待用户回复。
- 支持读取 Issue 中的图片附件，并在合议与 PRD 草稿中引用这些图片所表达的界面状态、错误信息或设计约束。
- 对无 PRD Issue 启动多 agent 合议，讨论需求清晰度、实现边界、验收标准、风险和是否需要 PRD。
- 将公开讨论 transcript、最终 synthesis、PRD 建议和待管理员决策项写入 Issue comment，形成可审计记录。
- 管理员在 Issue 中决定是否创建 PRD；管理员未确认前，Issue 不得被自动加上 `agent/ready`。
- 管理员决定创建 PRD 后，`iar` 先生成 PRD 草稿并写回 Issue，等待管理员确认草稿内容。
- 管理员确认后，`iar` 把 PRD 落盘到 `tasks/pending/`，在 PRD 中记录关联 Issue，在 Issue 中记录 PRD 路径，并将 label 更新为 `agent/ready`。
- runner 只在 PRD 已确认且 Issue 进入 `agent/ready` 后开始执行代码任务。

## Near-Term Delivery Order

1. 完成 worktree 默认远程新鲜基线，避免新任务从过期本地 base branch 开始。
2. 完成发布失败恢复命令，支持已有本地 commit 的任务安全 resume。
3. 完成 prompt template 与 phase 系统，降低 runner 行为调整成本。
4. 完成只读多 agent 合议基础能力，为需求澄清和 Issue-first intake 提供讨论引擎。
5. 基于合议能力补齐无 PRD Issue intake：写回 Issue 讨论、管理员 PRD gate、确认后落盘 PRD 并添加 `agent/ready`。
6. 完成多仓库 runner，支持一个执行端轮询多个目标仓库。
7. 补齐自动 code review gate，并定义哪些风险必须转人工。
8. 补齐 PR rebase 维护链路。
9. 在 CLI 能力稳定后，再完善前端或交互终端体验。

## Acceptance Checklist

- [x] 不会自主决定开始处理哪些 issue。
- [x] 能够通过 GitHub label 接收人工选择的 issue。
- [x] 能够从 PRD 创建可追踪的 GitHub Issue。
- [x] 能够从人工选定的 issue 中提取 PRD 路径和验收摘要。
- [x] 能够在隔离 worktree 中启动指定 agent。
- [x] 能够自动运行配置化验证命令。
- [x] 能够自动提交受控变更、推送任务分支并创建 Draft PR。
- [x] 能够在验证失败、agent 执行失败或 commit request 错误时做有限恢复。
- [x] 能够在失败时停止并报告原因到 Issue comment。
- [ ] 能够通过完整交互终端浏览、选择 issue 或输入明确任务。
- [ ] 能够在需求不明确时主动追问，而不是直接启动任务。
- [ ] 能够接收没有 PRD 的用户 Issue，并将其限制在 intake / 合议 / 审批流程中。
- [ ] 能够在用户 Issue 缺少关键需求、问题背景或复现信息时，在 Issue 中提出澄清问题并等待用户补充。
- [ ] 能够读取用户上传到 Issue 的图片附件，并把图片上下文纳入澄清、合议和 PRD 草稿生成。
- [ ] 能够把无 PRD Issue 的合议 transcript、synthesis 和建议动作写回 Issue comment。
- [ ] 能够在管理员确认前只生成 PRD 草稿，不把 PRD 落盘到仓库。
- [ ] 能够在管理员确认后把 PRD 写入 `tasks/pending/`、双向链接 Issue，并把 Issue 标记为 `agent/ready`。
- [ ] 能够在提交 PR 前完成独立 code review。
- [ ] 能够阻止带有高风险问题的 PR 自动发布。
- [ ] 能够创建包含完整实现摘要、验证结果和残余风险的 PR。
- [ ] 能够在发布失败后通过显式恢复命令继续已有本地成果。
- [ ] 能够在主分支更新后自动 rebase 已提交 PR。
- [ ] 能够在 rebase 冲突或验证失败时安全停止并报告原因。
- [ ] 能够处理多个目标仓库。
- [ ] 能够通过配置化 prompt template 管理不同执行阶段。

## Open Questions

- 是否只支持 GitHub，还是需要预留 GitLab 等平台适配层。
- 除了 issue label 准入控制外，是否还需要在某些高风险操作（如 rebase、force-push）前增加二次确认。
- PR 自动 rebase 后需要运行哪些最小验证集合。
- 何种风险级别必须转人工 review。
- 多仓库 daemon 是否始终顺序轮询，还是允许受限并发。
- 交互终端和前端 Dashboard 的边界如何划分。
- 无 PRD Issue 的 intake 状态使用哪些 label 表达，例如 `agent/needs-triage`、`agent/needs-prd` 或仅依赖 comment command。
- 管理员确认 PRD 草稿的交互方式是 Issue comment command、label、CLI 选择，还是三者都支持。
- 合议 transcript 写入 Issue 时的长度限制、摘要策略和敏感信息过滤规则。
- Issue 图片附件需要支持哪些格式、大小限制、下载缓存策略和隐私处理规则。
- AI 澄清问题的等待状态如何表达，例如 label、comment command、check run 或独立 intake state。
