# Roadmap

## Vision

构建一个面向人工调度的 AI 工程交互终端。项目本身不自动决定哪些 issue 开始、哪些 issue 暂停，而是在人工选择任务后，辅助完成需求理解、代码修改、验证、code review、提交 pull request，并在主分支、CI、评论或 PR 状态变化后维护已提交 PR 的监督与 rebase 状态。

## Product Boundary

- 当前项目提供交互终端和本地 runner 能力，不提供全自动 issue 调度系统。
- 是否开始处理某个代码任务由人工通过 label 控制（添加 `agent/ready` label，并可结合 `agent/claude`、`agent/codex` 或 `agent/kimi` 指定执行终端）。
- 用户可以直接提交没有 PRD 的 Issue，但这类 Issue 只能进入需求澄清、合议和 PRD 审批流程；在管理员确认前不得被 runner 当作可执行代码任务领取。
- 用户提交 Issue 时有义务尽量把需求、问题背景、复现信息、期望结果和已知约束描述清楚；如果描述不清楚，AI 必须先在 Issue 中反问用户，而不是自行补全关键需求。
- 用户可以在 Issue 中上传图片作为需求上下文，例如界面截图、错误截图、流程图或设计稿；AI 在澄清、合议和 PRD 草稿生成时应把这些图片视为 Issue 上下文的一部分。
- 终端可以展示 issue 信息、辅助分析任务和执行工程动作，但不会主动处理未标记的 issue。
- 一旦 PRD-backed issue 被标记为 `agent/ready`，后续执行链路（修改代码、验证、review、提交 PR、维护 PR 分支）应尽量自动完成；遇到需求不明确、安全门禁、验证失败、发布失败、冲突或高风险 review 发现时安全停止并报告。
- 当前实现优先支持 GitHub Issues / Labels / Pull Requests；其他代码托管平台暂不在当前交付边界内。

## Current Status

截至 2026-06-10，项目已经从概念验证推进到 CLI-first、本地多仓库 agent runner，并补齐了发布恢复、两阶段 AI review、post-PR supervisor、宽上下文 review-daemon、多 agent 只读合议、内置 worktree 管理和 PRD closeout 的基础闭环。当前状态依据来自 `src/backend/` 实现、`docs/guides/agent-runner.md`、`tasks/archive/` 已归档 PRD 和 `tasks/pending/` 待完成 PRD。

### Completed

- **CLI 基础能力已落地**：`iar init`、`iar labels sync`、`iar issue-from-prd`、`iar run-once`、`iar daemon`、`iar review-once`、`iar review-daemon`、`iar recover-publish`、`iar deliberate` 和 `iar worktree` 已接入项目脚本，并通过 `.iar.toml`、`config.toml` 与环境变量配置。
- **仓库本地配置已落地**：`iar init` 能在目标仓库生成 `.iar.toml`，全局 `config.toml` 主要保留默认值、环境级设置和 legacy repository registry。
- **人工准入队列已落地**：runner 只处理带有 `agent/ready` label 的代码任务，不会主动挑选未标记任务；支持 `agent/codex`、`agent/claude`、`agent/kimi` 等 agent 路由 label。
- **标准 label 流转已落地**：支持同步和使用 `agent/ready`、`agent/running`、`agent/supervising`、`agent/review`、`agent/failed`、`agent/blocked` 等队列状态。
- **PRD 到 Issue 的发布链路已落地**：`issue-from-prd` 能从 PRD 创建 GitHub Issue、回写 Issue URL，并支持 `--publish-prd` 在 ready 前提交和推送目标 PRD 文件；不带 `--publish-prd` 的 `--ready` 交互路径会延迟添加 ready label，避免 PRD 尚未发布时被 runner 领取。
- **多仓库目标解析已落地**：当前目录、`--repo`、`--repo-id` 和 `--all` 均可解析目标仓库；多仓库 registry 继续兼容，但未指定目标时默认处理当前 Git 仓库，不再隐式轮询所有 enabled registry entries。
- **隔离 worktree 执行链路已落地**：`run-once` 能领取 ready issue、创建或复用 worktree、选择 agent、运行 agent、执行验证、推送分支并创建 Draft PR。
- **内置 worktree 管理已落地**：`iar worktree create/path/remove` 统一管理 `.iar-worktrees/<branch>`，新 `iar init` 默认使用内置命令，`create_or_reuse_worktree` 会在返回前校验路径存在并输出三段命令诊断。
- **受限提交代理已落地**：agent 不直接 `git add` / `git commit`，而是写入 `.agent-runner/commit-request.json`，由 runner 在 host 侧完成受控提交。
- **本地验证和失败恢复已部分落地**：runner 支持配置化验证命令、失败输出摘要、有限 recovery loop、Claude stream-json 前台过滤和 recovery retry delay；commit request、验证失败、agent CLI 异常和 pre-commit 失败等可恢复错误会进入修复循环。
- **发布前安全检查已落地**：runner 会校验发布 remote、当前分支、禁止路径模式，并在失败时把 issue 标记为 `agent/failed`。
- **显式发布恢复已落地**：`iar recover-publish` 能复用已有干净本地 commit 完成 push、Draft PR 创建或复用、label/comment 收尾；`run-once` 也能识别 ready/running issue 已有 clean local commit 的恢复路径。
- **prompt template 与 phase 配置已落地**：`config.toml` / `.iar.toml` 支持 `[agent_runner.prompts]` 和 phase 模板，执行 prompt 不再只能通过 Python 硬编码调整。
- **PRD closeout gate 已落地**：PRD-backed Issue 成功发布前会检查 Acceptance Checklist；全部完成后可自动 `git mv` 从 `tasks/pending/` 归档到 `tasks/archive/`，并纳入同一任务 commit。
- **pre-push AI review 已落地**：实现 agent 提交后、push 前会执行独立 review session；reviewer 修改必须通过同一 commit proxy 和验证命令；空 commit request 会按 reviewer verdict 收敛或软失败，不再被误判为硬失败。
- **post-PR supervisor 已落地**：Draft PR 创建后 Issue 进入 `agent/supervising`，supervisor 可批准进入 `agent/review`、请求 repair/rebase/resolve-conflict、转人工 blocked 或标记 failed。
- **review daemon 宽上下文检测已落地**：`review-once` / `review-daemon` 能扫描 `agent/supervising` 和 `agent/review` Issue，并基于 head/base、CI checks、mergeability、Issue comments 和 PR comments 变化重新运行 supervisor cycle；supervisor 自写评论不会触发无限自循环。
- **rebase 冲突 agent 解决基础能力已落地**：post-PR supervisor 的 rebase 路径遇到冲突时可调用 agent 处理冲突、运行验证并用 `--force-with-lease` 推送。
- **AI 生成 Issue / PR 内容已落地**：`[agent_runner.generated_content]` 支持 template 和只读 agent 两种模式，并对 Issue 的 PRD anchor 与 PR 的 `Closes #...` anchor 做 fallback 校验。
- **只读多 agent 合议基础能力已落地**：`iar deliberate` 能运行 architect / skeptic / implementer 等 profile，输出 event stream、transcript、result、session metadata 和隔离 workspace 原始输出。
- **Issue 依赖门禁已落地**：PRD `Delivery Dependencies` 小节在 `iar issue create` 时被物化为 `iar:depends-on` marker 和 `task-group/` label；runner 领取 `agent/ready` Issue 前实时判定依赖满足状态，未满足时叠加 `agent/waiting` label 并写去重 comment；支持 Issue 编号依赖和 group 依赖，空 group 防护，上游 failed/blocked 点名提示。
- **文档与测试基础已落地**：已有 Agent Runner 使用指南、配置说明、架构规范、归档 PRD、pytest 覆盖和 `just test` 验证入口。

### Partially Completed

- **交互终端能力**：已有 CLI、少量交互式提示和基础前端结构，但还没有完整的 issue 浏览、任务选择、状态追踪和人工介入终端体验。
- **自动恢复能力**：执行、验证、提交请求、已有本地 commit 发布恢复、repair/rebase 已有基础闭环；发布恢复后重新进入 supervisor 的安全闭环、blocked/forbidden resolution、CI rework 状态恢复、rebase detached HEAD guard 仍待补齐。
- **可观测性**：终端前台输出、Issue comment、`iar:event` marker、合议日志、review-daemon cursor 和健康端点已具备；还缺少面向 operator 的统一监控面板、异常聚合和可审计时间线视图。
- **review 能力**：pre-push review、post-PR supervisor、宽上下文 review-daemon 已有闭环；高风险 finding 的稳定阻断规则、PR 正文 schema 校验和部分 supervisor 安全边界仍需补齐。
- **PR 分支维护能力**：supervisor 可请求现有 PR branch repair/rebase/resolve-conflict，review-daemon 可感知 base、checks、comment 和 mergeability 变化；detached HEAD rebase 中间态识别、恢复后 supervisor 闭环和 CI rework 状态恢复仍不完整。
- **前端能力**：已有基础前端结构和页面骨架；面向 agent runner 的可用操作台尚未完成。
- **Issue-first PRD 能力**：PRD -> Issue 已完成，Issue -> PRD / PRD rewrite / PRD review deliberation 仍在 pending PRD 中。

### Not Completed

- 无 PRD Issue 的 intake 候选识别、需求澄清、PRD 审批、PRD 落盘和 ready label 闭环。
- `agent/rework-prd` 驱动的 Issue -> PRD 自动生成、已有 PRD 重写和多 agent PRD review。
- 将只读多 agent 合议接入 GitHub Issue intake、PRD 草稿生成和 review 流程。
- 面向 operator 的 Agent Runner 监控面板、异常检测和 Issue 时间线 API。
- 发布恢复后先进入 `agent/supervising` 并运行 post-PR supervisor 的完整安全闭环。
- rebase conflict 阶段 detached HEAD / active rebase target 的安全识别。
- forbidden path blocked resolution、CI rework state recovery 和 process runner 错误可诊断性增强。
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
15. post-PR supervisor 检查 PR context、Issue/PR comments、diff、验证结果、checks 和 mergeability；通过后进入 `agent/review`，否则可请求 repair/rebase/resolve-conflict、转人工 blocked 或标记 failed。
16. `review-once` / `review-daemon` 持续观察已进入 `agent/supervising` 或 `agent/review` 的 Issue；当 PR head/base、CI/check、Issue/PR comment 或 mergeability 变化时重新运行 supervisor cycle。
17. 发布阶段失败但本地 commit 已存在时，operator 可用 `iar recover-publish` 完成发布收尾；目标状态应与普通 Draft PR 发布一致，进入 supervisor 闭环后再交给人工 review。
18. 在需求不明确、PRD 未确认、rebase 冲突无法安全确认、发布失败、验证失败或高风险 review 发现时安全停止，并输出明确的人工处理建议。

## Milestones

### M0: CLI And Queue Foundation

Status: Completed.

- 注册 `iar` CLI。
- 支持仓库本地 `.iar.toml` 初始化。
- 支持标准 label 同步。
- 使用 GitHub Issues / Labels 作为队列状态源。
- 通过 `.iar.toml`、`config.toml`、环境变量管理 runner、labels、git、worktree、safety、prompts、review、supervisor 和 generated content 配置。

### M1: Human-Gated Task Intake

Status: Partially completed.

- 已完成：通过 `agent/ready` label 和 agent 路由 label 控制 issue 是否进入 runner。
- 已完成：从 PRD 创建 Issue，并在 Issue body 中保留 canonical PRD 路径和验收摘要。
- 已完成：`issue-from-prd --ready` 在 PRD 未发布前不会提前添加 ready label。
- 未完成：完整交互终端中浏览 issue、选择任务、追问需求和展示状态。
- 未完成：直接输入自然语言任务后生成可追踪 PRD / Issue 的完整闭环。
- 未完成：没有 PRD 的用户 Issue 自动进入 intake 候选池，并通过管理员决策转换为 PRD 草稿或转人工结论。
- 未完成：PRD 草稿在管理员确认前只写回 Issue，不落盘到仓库、不触发 `agent/ready`。
- 未完成：在 Issue 描述不清楚时自动生成面向用户的澄清问题，并等待用户补充后再继续。
- 未完成：读取和引用 Issue 图片附件，将截图、设计稿或流程图纳入澄清、合议和 PRD 草稿上下文。

### M2: Code Change Agent

Status: Completed for the basic code-change path; deeper planning remains future work.

- 已完成：runner 能创建或复用 issue worktree，并启动 Codex、Claude 或 Kimi。
- 已完成：内置 `iar worktree` 管理 `.iar-worktrees/<branch>`，并在 worktree 路径漂移时 fail fast。
- 已完成：prompt 会要求 agent 读取 `AGENTS.md`、遵守仓库规范、修改代码、测试和必要文档。
- 已完成：runner 通过 commit request 文件完成受限提交。
- 已完成：prompt template / phase 配置化，避免每次调整 execution prompt 都改 Python 代码。
- 未完成：任务前的结构化规划、影响面识别和验收标准校验仍主要依赖 agent 自身执行。

### M3: Verification And Review

Status: Partially completed.

- 已完成：自动运行配置化验证命令。
- 已完成：验证失败、agent CLI 异常、commit request 错误、pre-commit 失败和零提交场景已有有限 recovery loop。
- 已完成：失败时把 issue 标记为 `agent/failed` 并写入失败 comment。
- 已完成：pre-push AI review gate 会在 push 前独立检查最终 diff；未批准时不会发布 PR。
- 已完成：空 commit request 在 pre-push review 中按 reviewer verdict 收敛或软失败，不再误报为 hard fail。
- 已完成：post-PR supervisor 会在 Draft PR 创建后至少运行一次，并支持 approve、repair、rebase、resolve-conflict、human-input-needed 和 failed 结果。
- 已完成：review-daemon 能检测 head/base、checks、comments 和 mergeability 变化，并避免 supervisor 自写评论触发自循环。
- 已完成：关键阶段写入 `iar:event` marker，形成基础审计 cursor。
- 未完成：把高风险 finding count 转换为稳定阻断规则或转人工规则。
- 未完成：PR 正文实现摘要、验证详情和残余风险仍缺少强 schema 化校验。
- 未完成：更细粒度的失败分类、恢复策略和面向 operator 的审计时间线视图。

### M4: Pull Request Automation

Status: Partially completed.

- 已完成：自动推送任务分支并创建 Draft PR。
- 已完成：PR body 包含 `Closes #<issue-number>`，Issue comment 记录分支、PR URL 和验证结果。
- 已完成：Draft PR 创建后 Issue 从 `agent/running` 流转到 `agent/supervising`，supervisor 通过后再进入 `agent/review`。
- 已完成：可通过 `[agent_runner.generated_content]` 生成更完整的 Issue / PR Markdown，并在锚点缺失时 fallback。
- 已完成：PRD-backed Issue 发布前会强制检查 Acceptance Checklist，并在完成时归档 PRD。
- 已完成：发布前会校验 remote、branch 和 forbidden paths，降低错误发布风险。
- 已完成：`iar recover-publish` 支持发布阶段失败后的显式恢复命令，并可复用已有 open Draft PR。
- 未完成：发布恢复成功后仍需与普通 Draft PR 发布保持同样的 post-PR supervisor 安全闭环。
- 未完成：默认分支 token 匹配、发布失败阶段分类和只读 supervisor dirty guard 仍在 pending PRD 中。

### M5: Multi-Repository Runner

Status: Completed.

- 已完成：在 `config.toml` 中通过 `[agent_runner.repositories]` 声明多个目标仓库，用于 legacy registry 和 `--repo-id` / `--all`。
- 已完成：`labels sync`、`run-once`、`review-once` 和 `daemon` 支持当前仓库、`--repo`、`--repo-id` 和 `--all` 目标解析。
- 已完成：`issue-from-prd` 在多仓库配置存在时通过当前仓库、`--repo` 或 `--repo-id` 解析到唯一目标仓库。
- 已完成：API 状态端点返回多仓库配置摘要和基础健康信息。

### M6: Main Branch Rebase Automation

Status: Partially completed.

- 已完成：post-PR supervisor 可请求 `rebase_pr_branch`，runner 会校验当前 branch 和 HEAD、fetch base、rebase、重新运行验证，并用 `--force-with-lease` 推送 PR branch。
- 已完成：rebase 冲突时可调用 agent 辅助解决，解决后继续 verification 和 push。
- 已完成：`review-once` / `review-daemon` 已按 Issue label 轮询，并能检测 base、checks、comments 和 mergeability 变化。
- 未完成：rebase 冲突中 Git 进入 detached HEAD 时，runner 还不能可靠识别 active rebase target 并保持分支安全门禁。
- 未完成：CI rework state recovery 和恢复后的 supervisor 状态机仍需补齐。
- 未完成：冲突失败分类、blocked/forbidden resolution 和 operator 操作建议仍需收敛。

### M7: Deliberation And Review Intelligence

Status: Partially completed.

- 已完成：支持 `iar deliberate` 多 agent 合议会话，用于需求澄清、方案争议和复杂设计评估。
- 已完成：支持只读 transcript、最终 synthesis、事件流、session metadata 和隔离 workspace 输出。
- 已完成：合议能力与 issue runner 解耦，不修改代码、不创建 branch、不创建 PR。
- 未完成：把面向 Issue 的合议 transcript、关键分歧、推荐结论和后续动作写回 Issue comment。
- 未完成：把合议结果接入 task intake、PRD 草稿生成、PRD review 或 code review，但不暴露隐藏思维链。

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

### M9: Operations Console

Status: Not completed.

- 提供只读 Agent Runner 监控面板，展示队列统计、仓库健康、Issue 状态、PR context、worktree 状态和最新事件。
- 提供 Issue 时间线 API，复用 `iar:event` marker、Issue comments、PR comments 和本地 worktree 状态。
- 检测 label 与 PR/worktree 状态不一致、failed/blocked、dirty worktree、stale PR、checks failed 和缺失 supervisor event 等异常。
- 所有恢复操作继续通过 CLI 执行；面板不暴露写 GitHub label/comment/PR 或修改 worktree 的 API。

## Near-Term Delivery Order

1. 完成发布恢复后的 supervisor 安全闭环：恢复成功后先进入 `agent/supervising`，supervisor approve 后再进入 `agent/review`，并修正分支 token 匹配与发布失败阶段分类。
2. 完成 rebase detached HEAD branch guard，确保 active rebase target 可确认时允许继续，无法确认时安全停止并输出可诊断错误。
3. 完成 CI rework state recovery、blocked/forbidden resolution 和 process runner 错误可诊断性增强。
4. 完成 Agent Runner operator 监控面板、异常检测和 Issue 时间线 API。
5. 基于现有 generated content 和合议能力补齐 Issue -> PRD / PRD rewrite：`agent/rework-prd`、管理员 PRD gate、确认后落盘 PRD 并添加 `agent/ready`。
6. 把多 agent deliberation 接入 PRD review，生成结构化 verdict、finding、risk 和后续动作 comment。
7. 补齐高风险 review finding 的稳定阻断规则，并定义哪些风险必须转人工。
8. 补齐 PR 正文 schema 校验，强制包含实现摘要、验证结果和残余风险。
9. 在 CLI、监控能力和 Issue-first PRD gate 稳定后，再完善更完整的交互终端体验。

## Acceptance Checklist

- [x] 不会自主决定开始处理哪些 issue。
- [x] 能够通过 GitHub label 接收人工选择的 issue。
- [x] 能够从 PRD 创建可追踪的 GitHub Issue。
- [x] 能够在 PRD 发布前延迟 ready label，避免 runner 读取过期 PRD。
- [x] 能够从人工选定的 issue 中提取 PRD 路径和验收摘要。
- [x] 能够在隔离 worktree 中启动指定 agent。
- [x] 能够用内置 `iar worktree` 管理默认 worktree 路径，并在路径漂移时 fail fast。
- [x] 能够自动运行配置化验证命令。
- [x] 能够自动提交受控变更、推送任务分支并创建 Draft PR。
- [x] 能够在验证失败、agent 执行失败、commit request 错误或 pre-commit 失败时做有限恢复。
- [x] 能够在失败时停止并报告原因到 Issue comment。
- [x] 能够处理多个目标仓库，并支持当前仓库、`--repo`、`--repo-id` 和 `--all` 解析。
- [x] 能够通过配置化 prompt template 管理不同执行阶段。
- [x] 能够在提交 PR 前完成独立 pre-push code review。
- [x] 能够在 pre-push review 空 commit request 时按 reviewer verdict 收敛或软失败。
- [x] 能够在 Draft PR 创建后运行 post-PR supervisor，并在通过后进入人工 review。
- [x] 能够通过 review-daemon 感知 head/base、checks、comments 和 mergeability 变化。
- [x] 能够避免 supervisor 自写 comment 导致 review-daemon 自循环。
- [x] 能够在 PRD-backed Issue 完成时检查 Acceptance Checklist 并归档 PRD。
- [x] 能够运行只读多 agent 合议并输出 transcript、synthesis 和事件记录。
- [x] 能够生成包含 `Closes #<issue-number>` 锚点的 PR body，并支持 template / agent 生成模式。
- [x] 能够在发布失败后通过显式 `iar recover-publish` 继续已有本地成果。
- [x] 能够在 rebase 冲突时调用 agent 尝试解决，失败或验证失败时安全停止并报告原因。
- [ ] 能够在发布恢复后进入与普通 Draft PR 相同的 supervisor 安全闭环。
- [ ] 能够在 rebase detached HEAD 中间态正确识别 active rebase target。
- [ ] 能够通过完整交互终端浏览、选择 issue 或输入明确任务。
- [ ] 能够在需求不明确时主动追问，而不是直接启动任务。
- [ ] 能够接收没有 PRD 的用户 Issue，并将其限制在 intake / 合议 / 审批流程中。
- [ ] 能够在用户 Issue 缺少关键需求、问题背景或复现信息时，在 Issue 中提出澄清问题并等待用户补充。
- [ ] 能够读取用户上传到 Issue 的图片附件，并把图片上下文纳入澄清、合议和 PRD 草稿生成。
- [ ] 能够把无 PRD Issue 的合议 transcript、synthesis 和建议动作写回 Issue comment。
- [ ] 能够在管理员确认前只生成 PRD 草稿，不把 PRD 落盘到仓库。
- [ ] 能够在管理员确认后把 PRD 写入 `tasks/pending/`、双向链接 Issue，并把 Issue 标记为 `agent/ready`。
- [ ] 能够从 Issue 生成或重写 PRD，并在多 agent PRD review 后等待人类确认。
- [ ] 能够用稳定规则阻止带有高风险问题的 PR 自动发布或自动转人工。
- [ ] 能够强校验 PR 正文包含完整实现摘要、验证结果和残余风险。
- [ ] 能够提供只读 operator 监控面板和 Issue 时间线 API。

## Open Questions

- 是否只支持 GitHub，还是需要预留 GitLab 等平台适配层。
- 除了 issue label 准入控制外，是否还需要在某些高风险操作（如 rebase、force-push）前增加二次确认。
- PR 自动 rebase 后需要运行哪些最小验证集合。
- 何种风险级别必须转人工 review。
- 多仓库 daemon 是否始终顺序轮询，还是允许受限并发。
- 交互终端和前端 Dashboard 的边界如何划分。
- 无 PRD Issue 的 intake 状态使用哪些 label 表达，例如 `agent/needs-triage`、`agent/needs-prd`、`agent/rework-prd` 或仅依赖 comment command。
- 管理员确认 PRD 草稿的交互方式是 Issue comment command、label、CLI 选择，还是三者都支持。
- 合议 transcript 写入 Issue 时的长度限制、摘要策略和敏感信息过滤规则。
- Issue 图片附件需要支持哪些格式、大小限制、下载缓存策略和隐私处理规则。
- AI 澄清问题的等待状态如何表达，例如 label、comment command、check run 或独立 intake state。
- 发布恢复命令是否应默认运行 post-PR supervisor，还是提供 operator 显式 `--no-supervisor` 逃生选项。
- review-daemon 对 comment 数量的 cursor 是否足够，还是需要后续记录评论 ID / 更新时间以区分编辑和删除。
