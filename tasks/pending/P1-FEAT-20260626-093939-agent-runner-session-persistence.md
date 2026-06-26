# PRD: Agent Runner Session Persistence（会话持久化预研）

- GitHub Issue: （待创建，关联 freshai #23 runner 多次失败复盘）
- 阶段：**Research / Exploration**（本 PRD 不强制要求完整实现验收）


## 1. Introduction & Goals

### 问题陈述

keda Agent Runner 当前对每一次尝试都启动一个全新的 agent 子进程（如 `claude`、`codex`、`kimi`），单次调用是**无状态**的：前一次 agent 的中间推理、失败教训、已探索路径、与 runner 的交互历史，在进程退出后全部丢失。

当 verification 失败时，runner 会启动 recovery agent，但 recovery agent 只能拿到 runner 重新构造的 prompt，无法获得前一次 agent 的完整上下文。这种信息断层导致：

1. Recovery agent 重复踩同样的坑，反复尝试已失败的方向。
2. Recovery agent 无法利用前一次 agent 对代码库、约束、验证错误的深入理解。
3. 在 freshai issue #23 等复杂场景中，多轮 recovery 后仍无法收敛，最终因超时或反复失败被标为 `agent/failed`。

本 PRD 不直接承诺某一种实现方案，而是先以**研究为导向**，系统性地评估 Claude、Codex、Kimi 三类 agent 的会话持久化能力，再决定 runner 应该采用哪种架构方向。

### Proposed Solution Summary

**推荐路径**：先由用户/维护者手动测试各 agent CLI/API 的会话延续能力，再形成架构决策记录；若验证可行，再进入原型实现。

1. **Claude 方向**
   - 评估 `--output-format stream-json` 是否能输出足够状态供后续恢复。
   - 评估 Anthropic API 的 `thread_id` / conversation memory 机制是否可复用。
   - 评估 Claude CLI 是否生成可读取的 session/cache 文件。

2. **Codex 方向**
   - 评估 OpenAI Codex CLI 是否内置会话保存/恢复机制。
   - 评估多次调用同一任务时能否延续对话。

3. **Kimi 方向**
   - 评估 Moonshot/Kimi CLI 是否支持会话保存或上下文续用。

4. **架构方向候选（暂不拍板）**
   - **Option A**：Runner 维护一个长驻 agent 进程，通过 IPC 持续喂任务。
   - **Option B**：利用各 agent 支持的 API-level thread/session ID。
   - **Option C**：上下文重放（enhanced prompt injection），在每次调用时把前序关键上下文注入 prompt，作为 fallback。

### 测量目标

1. 明确 Claude/Codex/Kimi 是否支持“第二次调用继续第一次对话”。
2. 若支持，明确所需的命令行参数、环境变量、API 字段、文件路径。
3. 识别各方案在 keda runner 场景下的 blocker：沙箱、token 上限、CLI 限制、并发安全、可观测性。
4. 形成一份带决策记录的 research notes，并决定下一步是否进入原型实现。

### Realistic Validation

本 PRD 属于研究阶段，验收物以**调研产出**为主：

- [ ] **Claude 会话延续调研**：测试第二次 `claude` 调用能否继续第一次对话，记录所需参数/文件/API 字段及限制。
- [ ] **Codex 会话延续调研**：测试第二次 `codex` 调用能否继续第一次对话，记录所需参数/文件/API 字段及限制。
- [ ] **Kimi 会话延续调研**：测试第二次 `kimi` 调用能否继续第一次对话，记录所需参数/文件/API 字段及限制。
- [ ] **架构决策记录（ADR）**：基于调研结果，从 Option A/B/C 中选择推荐方向，说明拒绝其他方案的理由。
- [ ] **可选原型**：若某方案验证可行，给出最小原型设计（不一定实现代码）。

**为什么不需要完整实现验收**：当前连三类 agent 的会话机制是否可行都不确定，强行写实现 PRD 会变成空中楼阁。研究阶段的目标是先回答“能不能做、怎么做、限制是什么”。

### Delivery Dependencies

- Group: agent-runner-session-persistence
- Depends on groups:
  - none
- Depends on tasks/issues:
  - freshai #23（复盘参考，非硬依赖）
- Gate type: research-gate（需先完成调研，再决定是否进入实现）
- Notes: 本 PRD 当前只产生文档与决策，不修改生产代码。


## 2. Requirement Shape

### Actor

- **AI agent**：Claude / Codex / Kimi，负责处理 GitHub Issue。
- **Agent Runner**：当前为每次尝试启动独立 agent 进程；未来可能维护长驻进程或复用 thread ID。
- **研究者/维护者**：手动测试各 agent 的会话延续能力，整理 research notes 与 ADR。
- **开发者**：若进入实现阶段，依据 ADR 修改 runner 调用方式。

### Trigger

1. Runner 第一次调用 agent 处理 issue，agent 产生代码但 verification 失败。
2. Runner 启动第二次调用（recovery agent），但缺少第一次调用的完整上下文。
3. 研究者需要判断：是否可以让第二次调用复用第一次的会话状态。

### Expected Behavior

1. 研究者分别对 Claude、Codex、Kimi 执行两次有上下文的调用，观察第二次调用是否能自然延续第一次。
2. 若某 agent 支持会话延续，记录最小可复现命令、环境变量、API 字段、session 文件位置。
3. 若不支持，记录官方文档/实验证据，并标注为 blocker。
4. 基于调研结果，输出 ADR，明确推荐 Option A/B/C 之一或组合。

### Explicit Scope Boundary

- 本阶段**不修改** runner 生产代码。
- 本阶段**不承诺**最终采用 Option A/B/C 中的任何一个。
- 调研范围限定在 Claude、Codex、Kimi 三种 agent；其他 agent 可后续补充。
- 只关注“会话/上下文延续”这一单一问题，不扩展到通用状态机、数据库持久化等方向。


## 3. Repository Context And Architecture Fit

### 当前相关模块/文件

| 关注点 | 位置 | 说明 |
|---|---|---|
| Runner 核心编排 | `src/backend/core/use_cases/run_agent_once.py` | 含 `run_agent_until_committed` recovery loop，每次尝试启动新 agent 进程。 |
| Agent 调用抽象 | `src/backend/engines/agent_runner/` | 含具体 agent CLI 的调用封装。 |
| 配置模型 | `src/backend/infrastructure/config/settings.py` | `AgentRunnerRunnerSettings`，未来可能新增 session 相关配置。 |
| 领域模型 | `src/backend/core/shared/models/agent_runner.py` | `AttemptResult` 等，未来可能新增 session metadata。 |

### 既有架构模式（需遵循）

- 依赖方向保持 `api → core → engines/infra`。
- 当前 agent 调用通过 `engines` 层封装，runner 不直接依赖 CLI 细节。
- 任何会话机制的实现，都应通过 `engines` 层适配，保持 `core` 层抽象不变。

### 所有权与依赖边界

| 关注点 | 责任归属 |
|---|---|
| 调研 Claude 会话机制 | 研究者/维护者 |
| 调研 Codex 会话机制 | 研究者/维护者 |
| 调研 Kimi 会话机制 | 研究者/维护者 |
| 架构决策记录 | 研究者/维护者 |
| 后续实现（若进入） | `src/backend/engines/agent_runner/` 与 `src/backend/core/use_cases/run_agent_once.py` |

### 运行时/测试/工作流约束

- 本阶段不引入代码变更，因此不触发单测约束。
- 调研笔记建议存放于 `docs/research/agent-runner-session-persistence/` 或 `tasks/loops/`（待项目组统一）。

### Existing PRD Relationship（必填）

检索 `tasks/pending/` 与 `tasks/archive/`：

- **未发现重复 PRD**：没有 pending/archive PRD 以“agent session persistence”或“agent 会话延续”为目标。
- **密切相关（pending）**：
  - `tasks/pending/P1-FEAT-20260626-015233-agent-runner-recovery-friction-reduction.md` —— 降低 recovery 摩擦；若会话持久化落地，可进一步减少 recovery 的信息损失。
- **结论**：本 PRD 是独立研究任务，可为 future recovery 优化提供输入，但当前无硬依赖。

### Potential Redundancy Risks

- 风险：与 recovery friction reduction PRD 的目标重叠。规避：本 PRD 聚焦“能否保留会话”，不直接修改 recovery 流程；两者可并行推进。
- 风险：研究范围无限扩大。规避：仅评估三类 agent 的会话延续能力，不扩展到通用 agent 平台。


## 4. Recommendation

### Recommended Approach（研究路径）

1. **Claude 调研**
   - 测试目标：第二次调用能否读取第一次的上下文。
   - 候选机制：
     - `--output-format stream-json`：观察输出中是否含 conversation state。
     - API `thread_id`：若 runner 改用 Anthropic API，是否可复用 thread。
     - CLI session/cache 文件：检查 `~/.claude/` 或项目级 cache 是否有可复用文件。
   - 记录：最小命令、环境变量、是否跨 worktree 有效、token 影响、沙箱限制。

2. **Codex 调研**
   - 测试目标：Codex CLI 是否有 `--continue`、session id、或自动保存的对话文件。
   - 候选机制：
     - CLI 内置 session 保存/恢复。
     - OpenAI API `thread_id`（若未来切到 API 模式）。
   - 记录：命令、环境变量、限制。

3. **Kimi 调研**
   - 测试目标：Kimi/Moonshot CLI 是否支持会话延续。
   - 候选机制：
     - 官方 CLI session 参数。
     - 本地对话缓存文件。
   - 记录：命令、环境变量、限制。

4. **架构方向对比（研究阶段只做记录，不决策）**

   | 方向 | 适用场景 | 主要风险 |
   |---|---|---|
   | Option A：Runner 长驻 agent 进程 + IPC | 所有 agent 都不支持原生会话时；需要完整控制上下文 | 进程管理复杂、agent 侧沙箱可能不支持、单点故障 |
   | Option B：API-level thread/session ID | Claude/Codex/Kimi 中任意一家支持稳定 thread 复用 | 需要切换到 API 调用、CLI 与 API 行为可能不一致、session 有生命周期 |
   | Option C：上下文重放（enhanced prompt injection） | 作为 fallback，不依赖 agent 原生能力 | token 消耗高、prompt 过长导致质量下降、无法恢复隐式状态 |

### 为什么最适合当前架构

- 研究先行，避免在机制未明时写大量代码。
- 保持 runner 现有抽象不变，调研结果通过 ADR 沉淀后再进入实现。
- 三种 agent 差异大，统一方案不可行；先收集事实再决策。

### Alternatives Considered

| 方案 | 说明 | 拒绝/暂缓原因 |
|---|---|---|
| 直接实现 Option A 长驻进程 | 跳过调研，统一用 IPC 维持 agent | 工程量大，且不清楚 agent CLI 是否支持被外部进程长期持有 |
| 直接实现 Option B API thread | 跳过调研，统一用各厂商 API | 需要 runner 切换到 API 模式，可能破坏现有 CLI 抽象；先验证可行性 |
| 直接实现 Option C 上下文重放 | 不调研，直接用超长 prompt | token 成本高且效果未必好，应作为 fallback 而非首选 |
| 同时调研所有主流 agent | 不止 Claude/Codex/Kimi | 范围过大；先聚焦 runner 已支持的三种 agent |


## 5. Implementation Guide

> 本节在研究阶段描述的是**调研执行计划**，而非生产代码实现指南。如进入实现阶段，再补充具体代码变更。

### Core Logic（调研流程）

```
for agent in [claude, codex, kimi]:
    1. 构造两段有上下文的对话输入 A 和 B
    2. 第一次调用 agent 输入 A，记录输出 O1 和会话状态 S1
    3. 第二次调用 agent 输入 B，尝试复用 S1
    4. 观察输出 O2 是否体现对 A/O1 的记忆
    5. 记录支持/不支持、命令/参数/文件、限制
end
整理 ADR，选择 Option A/B/C
```

### Change Impact Tree

```text
.
└── docs/research/agent-runner-session-persistence/
    ├── claude-session-notes.md   [新增]
    ├── codex-session-notes.md    [新增]
    ├── kimi-session-notes.md     [新增]
    └── adr-xxx-session-persistence.md [新增]
```

> 本阶段不修改 `src/` 与 `tests/`。

### Executor Drift Guard

调研期间可用以下命令定位 runner 当前调用方式，作为后续实现参考：

```bash
# 1. 定位 agent 调用封装
rg -n "claude|codex|kimi" src/backend/engines/agent_runner/

# 2. 定位 runner recovery loop
rg -n "run_agent_until_committed|run_agent_with_prompt_resilient" src/backend/core/use_cases/run_agent_once.py

# 3. 定位配置映射
rg -n "AgentRunnerRunnerSettings|AppConfig" src/backend/infrastructure/config/settings.py src/backend/core/shared/models/agent_runner.py
```

### Realistic Validation Plan

| Behavior | Real Entry Point | Test Layer | Mock Boundary | Data/Env Needed | Command Or Procedure | Required For Acceptance |
|---|---|---|---|---|---|---|
| Claude 会话延续 | 本地手动调用 `claude` CLI 或 API | manual | 无 | Claude CLI/API key | 记录最小复现命令与输出 | Yes |
| Codex 会话延续 | 本地手动调用 `codex` CLI 或 API | manual | 无 | Codex CLI/API key | 记录最小复现命令与输出 | Yes |
| Kimi 会话延续 | 本地手动调用 `kimi` CLI 或 API | manual | 无 | Kimi CLI/API key | 记录最小复现命令与输出 | Yes |
| 架构决策记录 | `docs/research/agent-runner-session-persistence/adr-*.md` | document review | 无 | 调研笔记 | 维护者评审 | Yes |
| 回归（代码无变更） | — | — | — | — | — | N/A |

**Failure Triage Notes**

- 若某 agent 第二次调用无法延续上下文 → 记录为 blocker，并在 ADR 中说明是否排除该方案。
- 若某 agent 需要切换为 API 模式才能复用 thread → 评估对现有 CLI 抽象的破坏程度。
- 若 session 文件仅在特定目录/沙箱内有效 → 评估 runner worktree 模式下的可用性。

### Low-Fidelity Prototype

不需要交互原型；调研阶段可产出最小脚本或命令片段，但不进入仓库。

### ER Diagram

No data model changes in research phase.

### Interactive Prototype Change Log

No interactive prototype file changes in this PRD.

### External Validation

需要访问 Claude、Codex、Kimi 的官方文档与 CLI 行为；必要时查询官方文档或社区 issue。


## 6. Definition Of Done

- 完成 Claude 会话延续调研笔记。
- 完成 Codex 会话延续调研笔记。
- 完成 Kimi 会话延续调研笔记。
- 输出架构决策记录（ADR），从 Option A/B/C 中给出推荐方向及理由。
- 若推荐方向可行，给出最小原型设计或下一步实现 PRD 的入口。
- 本 PRD 的 Acceptance Checklist 全部勾选后，可进入实现阶段或归档为 research-done。


## 7. Acceptance Checklist

### Research Acceptance
- [ ] Claude 调研笔记存在，明确回答“第二次调用能否延续第一次对话”。
- [ ] Codex 调研笔记存在，明确回答“第二次调用能否延续第一次对话”。
- [ ] Kimi 调研笔记存在，明确回答“第二次调用能否延续第一次对话”。
- [ ] 每份笔记包含：最小命令/参数/API 字段、环境变量、限制与 blocker。
- [ ] ADR 存在，且从 Option A/B/C 中选择了推荐方向（或明确选择暂缓）。

### Architecture Acceptance
- [ ] ADR 中说明推荐方向如何适配现有 `engines` 层抽象。
- [ ] ADR 中说明推荐方向不破坏依赖方向 `api → core → engines/infra`。
- [ ] 若推荐方向涉及切换 API 调用，评估对现有 CLI 配置与沙箱的影响。

### Documentation Acceptance
- [ ] 调研笔记存放在项目约定目录（如 `docs/research/agent-runner-session-persistence/`）。
- [ ] ADR 引用所有三份调研笔记。

### Validation Acceptance
- [ ] 调研命令可被复现（笔记中包含完整输入输出示例）。
- [ ] ADR 经过维护者评审并通过。


## 8. Functional Requirements

- **FR-1**：Claude 调研必须覆盖 `--output-format stream-json`、API `thread_id`、CLI session/cache 文件三种候选机制。
- **FR-2**：Codex 调研必须覆盖 CLI 内置 session 机制与 API `thread_id` 两种候选机制。
- **FR-3**：Kimi 调研必须覆盖官方 CLI session 参数与本地对话缓存文件两种候选机制。
- **FR-4**：每份调研笔记必须记录：是否支持延续、最小命令/API 字段、环境变量、token/沙箱/lifetime 限制。
- **FR-5**：ADR 必须对比 Option A/B/C，并给出明确推荐或“暂缓”结论。
- **FR-6**：ADR 必须说明推荐方案在 runner worktree 模式下的可行性。
- **FR-7**：若推荐方案需要后续实现，ADR 必须列出进入实现阶段的最小前置条件。


## 9. Non-Goals

- 本 PRD 不直接修改 runner 生产代码。
- 本 PRD 不实现完整的 session persistence 功能。
- 本 PRD 不扩展到 Claude/Codex/Kimi 以外的 agent。
- 本 PRD 不设计通用数据库/队列/持久化层。
- 本 PRD 不要求一次性同时支持三种 agent；可选择其中可行的一家先行验证。
- 本 PRD 不替代 recovery friction reduction PRD 的工作。


## 10. Risks And Follow-Ups

| 风险 | 影响 | 缓解措施 | Follow-Up |
|---|---|---|---|
| 三类 agent 均不支持稳定会话延续 | 高 | Option C 上下文重放作为 fallback | 评估超长 prompt 的 token 成本与效果 |
| API thread 模式与当前 CLI 抽象冲突 | 中 | 在 engines 层新增 API adapter，保持 core 层不变 | 进入实现阶段后补充接口设计 |
| 长驻进程方案受 agent 沙箱限制 | 中 | 先验证 agent CLI 是否允许被外部进程持有 | 测试进程生命周期管理 |
| 调研周期过长阻塞其他优化 | 低 | 设置时间盒；允许“暂缓”结论 | 在 ADR 中明确下一步时间点 |
| 会话状态跨 worktree 不一致 | 中 | 调研时同步测试 worktree 场景 | 在 ADR 中标注适用边界 |


## 11. Decision Log

| ID | 决策问题 | Chosen | Rejected | Rationale |
|---|---|---|---|
| D-01 | 是否直接实现 | 先研究调研 | 直接写代码 | 三类 agent 会话机制差异大，机制未明时实现风险高 |
| D-02 | 调研范围 | Claude / Codex / Kimi | 所有主流 agent | 聚焦 runner 当前支持的三种 agent，控制范围 |
| D-03 | 调研交付物 | 笔记 + ADR | 直接产代码 PRD | 研究阶段先沉淀事实与决策，再决定是否进入实现 |
| D-04 | 架构候选 | A/B/C 并存 | 预先选定单一方案 | 在事实清楚前不做不可逆架构承诺 |
| D-05 | 是否允许“暂缓”结论 | 允许 | 必须给出实现方案 | 若三类 agent 均不可行，应诚实记录并转向 fallback |
