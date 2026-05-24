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

截至 2026-05-24，项目已经从概念验证推进到多仓库、CLI-first 的本地 agent runner，并补齐了两阶段 AI review、只读合议和 PRD closeout 的基础闭环。当前状态依据来自 `src/backend/` 实现、`docs/guides/agent-runner.md`、`tasks/archive/` 已归档 PRD 和 `tasks/pending/` 待完成 PRD。

### Completed

- **CLI 基础能力已落地**：`iar labels sync`、`iar issue-from-prd`、`iar run-once`、`iar daemon`、`iar review-once`、`iar review-daemon` 和 `iar deliberate` 已接入项目脚本，并通过 `config.toml` 与环境变量配置。
- **人工准入队列已落地**：runner 只处理带有 `agent/ready` label 的 issue，不会主动挑选未标记任务；支持 `agent/codex`、`agent/claude`、`agent/kimi` 等 agent 路由 label。
- **标准 label 流转已落地**：支持同步和使用 `agent/ready`、`agent/running`、`agent/supervising`、`agent/review`、`agent/failed`、`agent/blocked` 等队列状态。
- **PRD 到 Issue 的发布链路已落地**：`issue-from-prd` 能从 PRD 创建 GitHub Issue、回写 Issue URL，并支持 `--publish-prd` 在 ready 前只提交和推送目标 PRD 文件。
- **多仓库执行链路已落地**：`run-once` 能领取 ready issue、创建或复用 worktree、选择 agent、运行 agent、执行验证、推送分支并创建 Draft PR；未指定仓库时可轮询所有启用仓库。
- **受限提交代理已落地**：agent 不直接 `git add` / `git commit`，而是写入 `.agent-runner/commit-request.json`，由 runner 在 host 侧完成受控提交。
- **本地验证和失败恢复已部分落地**：runner 支持配置化验证命令、失败输出摘要、有限 recovery loop、Claude stream-json 前台过滤和 recovery retry delay。
- **发布前安全检查已落地**：runner 会校验发布 remote、当前分支、禁止路径模式，并在失败时把 issue 标记为 `agent/failed`。
- **prompt template 与 phase 配置已落地**：`config.toml` 支持 `[agent_runner.prompts]` 和 phase 模板，执行 prompt 不再只能通过 Python 硬编码调整。
- **PRD closeout gate 已落地**：PRD-backed Issue 成功发布前会检查 Acceptance Checklist；全部完成后可自动 `git mv` 从 `tasks/pending/` 归档到 `tasks/archive/`，并纳入同一任务 commit。
- **pre-push AI review 已落地**：实现 agent 提交后、push 前会执行独立 review session；reviewer 修改必须通过同一 commit proxy 和验证命令。
- **post-PR supervisor 已落地**：Draft PR 创建后 Issue 进入 `agent/supervising`，supervisor 可批准进入 `agent/review`、请求 repair/rebase、转人工 blocked 或标记 failed。
- **review daemon 已落地基础版**：`review-once` / `review-daemon` 能扫描 `agent/supervising` 和 `agent/review` Issue，并在 head/base context 变化后重新运行 supervisor cycle。
- **基础 API 状态端点已落地**：提供 agent-runner status 和 health 只读端点，用于暴露配置摘要和 `gh` 可用性。
- **文档与测试基础已落地**：已有 Agent Runner 使用指南、配置说明、架构规范、归档 PRD、pytest 覆盖和 `just test` 验证入口。
- **多仓库配置与轮询已落地**：`config.toml` 支持 `[agent_runner.repositories]` 声明多个目标仓库；`labels sync`、`run-once`、`daemon` 在未指定 `--repo` 或 `--repo-id` 时自动处理所有启用仓库；`issue-from-prd` 支持通过 `--repo-id` 解析到唯一目标仓库；API 状态端点返回多仓库配置摘要。
- **新 worktree 默认基于最新远程 base ref 已落地**：`scripts/worktree/create.sh` 在创建前默认 fetch 远程 tracking ref，新增 `KEDA_WORKTREE_SYNC_BASE` 和 `KEDA_WORKTREE_BASE_REMOTE` 环境变量，无远程或同步关闭时回退到本地 base branch。
- **AI 生成 Issue / PR 内容已落地**：`[agent_runner.generated_content]` 支持 template 和只读 agent 两种模式，并对 Issue 的 PRD anchor 与 PR 的 `Closes #...` anchor 做 fallback 校验。
- **只读多 agent 合议基础能力已落地**：`iar deliberate` 能运行 architect / skeptic / implementer 等 profile，输出 event stream、transcript、result、session metadata 和隔离 workspace 原始输出。

### Partially Completed

- **交互终端能力**：已有 CLI 和少量交互式提示，但还没有完整的 issue 浏览、任务选择、状态追踪和人工介入终端体验。
- **自动恢复能力**：agent 执行、验证和提交请求失败已有有限 recovery loop；发布阶段失败后的显式 `recover-publish` CLI 命令尚未完成。
- **可观测性**：终端前台输出、Issue comment、`iar:event` marker、合议日志和健康端点已具备；还缺少面向 operator 的统一监控面板、异常聚合和可审计时间线视图。
- **review 能力**：pre-push review 和 post-PR supervisor 已有基础闭环；高风险 finding 的稳定阻断规则、review-daemon 的更宽上下文检测和冲突修复体验仍需补齐。
- **PR 分支维护能力**：supervisor 可请求现有 PR branch repair/rebase，rebase 有 branch/HEAD 校验和 `--force-with-lease`；持续检测主分支变化、CI/comment/mergeability 变化和冲突自动修复仍不完整。
- **前端能力**：已有基础前端结构和页面骨架；面向 agent runner 的可用操作台尚未完成。

### Not Completed

- 发布失败后的显式 `recover-publish` / resume CLI 命令。
- 无 PRD Issue 的 intake 候选识别、需求澄清、PRD 审批、PRD 落盘和 ready label 闭环。
- 将只读多 agent 合议接入 GitHub Issue intake、PRD 草稿生成和 review 流程。
- 面向 operator 的 Agent Runner 监控面板、异常检测和 Issue 时间线 API。
- PR 创建后的完整持续 rebase、冲突解决、CI/comment/mergeability 变化检测和状态维护。
- 高风险 review 发现阻止 PR 发布或转人工的完整稳定规则。
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
13. runner 做发布前安全检查和 pre-push AI review；reviewer 如需修改，仍通过受限 commit proxy 和配置化验证命令完成。
14. review 通过后，runner 推送任务分支、创建 Draft PR，并把 Issue 移入 `agent/supervising`。
15. post-PR supervisor 检查 PR context、Issue/PR comments、diff 和验证结果；通过后进入 `agent/review`，否则可请求 repair/rebase、转人工 blocked 或标记 failed。
16. `review-once` / `review-daemon` 持续观察已进入 `agent/supervising` 或 `agent/review` 的 Issue；当前基础版主要依赖 head/base 变化，后续补齐 CI、comment 和 mergeability 变化检测。
17. 在需求不明确、PRD 未确认、rebase 冲突、发布失败、验证失败或高风险 review 发现时安全停止，并输出明确的人工处理建议。

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
- 已完成：新 worktree 默认基于最新远程 base ref，避免任务从过期本地分支开始。
- 未完成：完整交互终端中浏览 issue、选择任务、追问需求和展示状态。
- 未完成：直接输入自然语言任务后生成可追踪 PRD / Issue 的完整闭环。
- 未完成：没有 PRD 的用户 Issue 自动进入 intake 候选池，并通过管理员决策转换为 PRD 草稿或转人工结论。
- 未完成：PRD 草稿在管理员确认前只写回 Issue，不落盘到仓库、不触发 `agent/ready`。
- 未完成：在 Issue 描述不清楚时自动生成面向用户的澄清问题，并等待用户补充后再继续。
- 未完成：读取和引用 Issue 图片附件，将截图、设计稿或流程图纳入澄清、合议和 PRD 草稿上下文。

### M2: Code Change Agent

Status: Completed for the basic code-change path; deeper planning remains future work.

- 已完成：runner 能创建或复用 issue worktree，并启动 Codex、Claude 或 Kimi；新 worktree 默认基于最新远程 base ref。
- 已完成：prompt 会要求 agent 读取 `AGENTS.md`、遵守仓库规范、修改代码、测试和必要文档。
- 已完成：runner 通过 commit request 文件完成受限提交。
- 已完成：prompt template / phase 配置化，避免每次调整 execution prompt 都改 Python 代码。
- 未完成：任务前的结构化规划、影响面识别和验收标准校验仍主要依赖 agent 自身执行。

### M3: Verification And Review

Status: Partially completed.

- 已完成：自动运行配置化验证命令。
- 已完成：验证失败、agent CLI 异常、commit request 错误和零提交场景已有有限 recovery loop。
- 已完成：失败时把 issue 标记为 `agent/failed` 并写入失败 comment。
- 已完成：pre-push AI review gate 会在 push 前独立检查最终 diff；未批准时不会发布 PR。
- 已完成：post-PR supervisor 会在 Draft PR 创建后至少运行一次，并支持 approve、repair、rebase、human-input-needed 和 failed 结果。
- 已完成：关键阶段写入 `iar:event` marker，形成基础审计 cursor。
- 未完成：把高风险 finding count 转换为稳定阻断规则或转人工规则。
- 未完成：review-daemon 对 CI/check、Issue/PR comment、mergeability 的上下文变化检测仍需扩展。
- 未完成：更细粒度的失败分类、恢复策略和面向 operator 的审计时间线视图。

### M4: Pull Request Automation

Status: Partially completed.

- 已完成：自动推送任务分支并创建 Draft PR。
- 已完成：PR body 包含 `Closes #<issue-number>`，Issue comment 记录分支、PR URL 和验证结果。
- 已完成：Draft PR 创建后 Issue 从 `agent/running` 流转到 `agent/supervising`，supervisor 通过后再进入 `agent/review`。
- 已完成：可通过 `[agent_runner.generated_content]` 生成更完整的 Issue / PR Markdown，并在锚点缺失时 fallback。
- 已完成：PRD-backed Issue 发布前会强制检查 Acceptance Checklist，并在完成时归档 PRD。
- 已完成：发布前会校验 remote、branch 和 forbidden paths，降低错误发布风险。
- 未完成：发布失败后的显式 `recover-publish` / resume CLI 命令。
- 未完成：PR 正文质量仍依赖 template / agent 生成质量，还缺少强 schema 化的实现摘要、验证详情和残余风险校验。

### M5: Multi-Repository Runner

Status: Completed.

- 已完成：在 `config.toml` 中通过 `[agent_runner.repositories]` 声明多个目标仓库。
- 已完成：`labels sync`、`run-once` 和 `daemon` 在未指定 `--repo` 或 `--repo-id` 时自动处理所有启用仓库。
- 已完成：`issue-from-prd` 在多仓库配置存在时通过 `--repo-id` 解析到唯一目标仓库。
- 已完成：API 状态端点返回多仓库配置摘要和基础健康信息。

### M6: Main Branch Rebase Automation

Status: Partially completed.

- 已完成：post-PR supervisor 可请求 `rebase_pr_branch`，runner 会校验当前 branch 和 HEAD、fetch base、rebase、重新运行验证，并用 `--force-with-lease` 推送 PR branch。
- 已完成：`review-once` / `review-daemon` 已有按 Issue label 轮询的基础入口。
- 未完成：稳定检测所有已打开 PR 是否落后于主分支，并避免重复 supervisor cycle。
- 未完成：rebase 冲突后的 agent 辅助解决、验证和失败分类仍需补齐。
- 未完成：CI/check、Issue/PR comment、mergeability 变化触发 supervisor 的完整上下文检测仍需补齐。

### M7: Deliberation And Review Intelligence

Status: Partially completed.

- 已完成：支持 `iar deliberate` 多 agent 合议会话，用于需求澄清、方案争议和复杂设计评估。
- 已完成：支持只读 transcript、最终 synthesis、事件流、session metadata 和隔离 workspace 输出。
- 已完成：合议能力与 issue runner 解耦，不修改代码、不创建 branch、不创建 PR。
- 未完成：把面向 Issue 的合议 transcript、关键分歧、推荐结论和后续动作写回 Issue comment。
- 未完成：把合议结果接入 task intake、PRD 草稿生成或 code review，但不暴露隐藏思维链。

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

1. 完成发布失败后的显式 `recover-publish` CLI 命令，支持已有本地 commit 的任务安全 resume。
2. 完成 rebase 冲突 agent 辅助解决，以及 review-daemon 对 checks、comments、mergeability 的上下文扩展。
3. 完成 Agent Runner operator 监控面板、异常检测和 Issue 时间线 API。
4. 完成合议实时输出增强，确保合议过程中各 agent 原始输出文件可持续观察。
5. 基于合议能力补齐无 PRD Issue intake：写回 Issue 讨论、管理员 PRD gate、确认后落盘 PRD 并添加 `agent/ready`。
6. 补齐高风险 review finding 的稳定阻断规则，并定义哪些风险必须转人工。
7. 补齐发布失败、验证失败、supervisor failed 和 blocked 状态的显式恢复操作手册或 CLI。
8. 在 CLI 和监控能力稳定后，再完善更完整的交互终端体验。

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
- [x] 能够处理多个目标仓库。
- [x] 能够通过配置化 prompt template 管理不同执行阶段。
- [x] 能够在提交 PR 前完成独立 pre-push code review。
- [x] 能够在 Draft PR 创建后运行 post-PR supervisor，并在通过后进入人工 review。
- [x] 能够在 PRD-backed Issue 完成时检查 Acceptance Checklist 并归档 PRD。
- [x] 能够运行只读多 agent 合议并输出 transcript、synthesis 和事件记录。
- [x] 能够生成包含 `Closes #<issue-number>` 锚点的 PR body，并支持 template / agent 生成模式。
- [ ] 能够通过完整交互终端浏览、选择 issue 或输入明确任务。
- [ ] 能够在需求不明确时主动追问，而不是直接启动任务。
- [ ] 能够接收没有 PRD 的用户 Issue，并将其限制在 intake / 合议 / 审批流程中。
- [ ] 能够在用户 Issue 缺少关键需求、问题背景或复现信息时，在 Issue 中提出澄清问题并等待用户补充。
- [ ] 能够读取用户上传到 Issue 的图片附件，并把图片上下文纳入澄清、合议和 PRD 草稿生成。
- [ ] 能够把无 PRD Issue 的合议 transcript、synthesis 和建议动作写回 Issue comment。
- [ ] 能够在管理员确认前只生成 PRD 草稿，不把 PRD 落盘到仓库。
- [ ] 能够在管理员确认后把 PRD 写入 `tasks/pending/`、双向链接 Issue，并把 Issue 标记为 `agent/ready`。
- [ ] 能够用稳定规则阻止带有高风险问题的 PR 自动发布或自动转人工。
- [ ] 能够强校验 PR 正文包含完整实现摘要、验证结果和残余风险。
- [ ] 能够在发布失败后通过显式恢复命令继续已有本地成果。
- [ ] 能够在主分支更新后稳定检测并自动维护已提交 PR。
- [ ] 能够在 rebase 冲突时调用 agent 尝试解决，失败或验证失败时安全停止并报告原因。

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
