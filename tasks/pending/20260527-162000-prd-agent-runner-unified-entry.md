# PRD: Agent Runner 自然语言总入口 (`iar ask`) —— **1 个月内不实现，无无法忍受痛点不做**

---

## 1. 引言与目标 (Introduction & Goals)

### 问题说明

当前 Agent Runner 的操作入口全部依赖精确的 CLI 命令（如 `iar run`、`iar deliberate`、`iar status`、`iar recover` 等），新用户需要记忆子命令、参数、flag 的组合方式，学习成本较高。团队认同需要一个自然语言入口来降低使用门槛，但对入口形态、安全边界和实现顺序存在根本性分歧。

### 目标（如未来实现）

- 提供一个自然语言入口 `iar ask "<自然语言指令>"`，将用户意图路由到已有的 use case（`run_once`、`deliberate`、`status`、`recover` 等）。
- 入口必须是**受限的**，不允许任意 shell 命令，只能执行白名单内的结构化动作。
- 不新增平行执行路径，所有操作走已有 use case。
- 默认展示计划（dry-run），高风险操作需要显式确认或 `--yes`。
- 每轮 `iar ask` 完全独立，不引入 Memory / 会话状态。

### 为什么现在不做

三个角色（Architect、Skeptic、Implementer）在 deliberation 中达成共识：

1. **Ops Console 是自然语言入口的正确载体**，CLI chat 是模态错配。当前 Ops Console PRD 正在编写中，应优先完成。
2. **当前提案的风险收益比失衡**：LLM 路由的误操作风险（参数推断不可预测、dry-run 虚假安全感、责任归属模糊）高于学习成本节省的收益。
3. **需要 3 个月只读模式数据**：在扩展任何写操作权限前，需先验证只读查询的误路由率是否可接受。

---

## 2. 需求形态 (Requirement Shape)

- **执行者 (Actor)**: 终端用户，通过 CLI `iar ask` 发起自然语言指令。
- **触发条件 (Trigger)**: 用户输入自然语言描述其意图（如"查看 runner 状态"、"让 architect 和 skeptic 讨论这个需求"）。
- **预期行为 (Expected Behavior)**: 系统解析意图，映射到白名单 use case，展示执行计划，等待确认后调用已有编排逻辑。
- **范围边界 (Scope Boundary)**: 仅作为意图路由层；不替代现有 CLI 命令；不引入新执行路径；不维护会话状态。

---

## 3. 共识与分歧记录 (Consensus & Disagreements)

### 3.1 共识

| 维度 | 共识 |
|---|---|
| **不做通用执行器** | 入口 agent 必须是受限的，不允许任意 shell 命令 |
| **白名单动作** | 只能执行枚举的、结构化的动作（status、deliberate、run_once 等） |
| **复用 use_cases** | 不新增平行执行路径，所有操作走已有 use case |
| **dry-run 支持** | 默认展示计划，不直接执行 |
| **写操作确认** | 高风险操作需要显式确认或 `--yes` |

### 3.2 分歧

| 议题 | Architect | Skeptic | Implementer |
|---|---|---|---|
| **是否现在做** | 做 | **暂缓**，先跑 3 个月只读模式 | 做 MVP |
| **第一版范围** | CLI `iar ask` | **只做只读查询**，等 Ops Console | CLI `iar ask`，写操作需确认 |
| **LLM 角色** | 仅做实体抽取 fallback | 规则优先，不依赖 LLM | 可选 LLM fallback，但输出校验 |
| **Memory** | 短期会话上下文 | **不做，每轮独立** | V1 不做 |
| **交互模态** | Phase 1 CLI，Phase 2 WebSocket | **等 WebSocket 接 Ops Console** | CLI 优先 |
| **daemon 类操作** | 可进白名单 | V1 不自动执行 | V1 不自动执行 |

**当前决策**：采纳 Skeptic 的立场——**暂缓实现**，优先完成 Ops Console PRD，收集 3 个月只读模式数据后再评估。

---

## 4. 风险评估 (Risks)

| 风险 | 严重程度 | 来源 |
|---|---|---|
| **误路由导致真实副作用** | 高 | LLM 把"查看状态"误解为"批量执行 ready issues" |
| **dry-run 虚假安全感** | 高 | 用户无法验证 LLM 生成的 `run_once(agent="auto", max_issues=5)` 是否正确 |
| **参数推断不可预测** | 高 | "帮我跑一下 PRD" → 需要推断 repo_id、agent、max_issues，LLM 猜错率高 |
| **CLI + 自然语言反模式** | 中 | 同时失去 CLI 精确性和 GUI 直观性 |
| **责任归属模糊** | 中 | 出错时用户/LLM/工具三方互责，无法审计 |
| **渐进式复杂度陷阱** | 中 | `iar ask` 存在后用户要求多轮对话 → 记忆 → session store → 变成 ChatOps 平台 |
| **维护耦合** | 中 | 每新增 CLI 命令需同步更新 intent rules，拖慢迭代 |
| **测试不可 bounded** | 中 | 自然语言测试集无限膨胀，中文歧义尤其难覆盖 |

---

## 5. 后续行动建议 (Next Actions)

**如未来重新评估此 PRD，建议按以下顺序推进：**

1. **先完成 Ops Console PRD（进行中）** — Web UI 是自然语言入口的正确载体，CLI chat 是模态错配。

2. **如果坚持做 CLI 入口，只做只读查询模式**：
   ```bash
   iar ask "查看 runner 状态"
   iar ask "有哪些 failed 的 issue"
   iar ask "让 architect 和 skeptic 讨论这个需求"
   ```
   零写操作，无确认疲劳，无状态机腐蚀风险。

3. **如果需要写操作，明确默认值策略**（这是当前提案最大缺口）：
   - `repo_id`：当前 git remote？还是默认仓库？还是必须显式指定？
   - `agent`：默认 auto？上次用户用过的？
   - `max_issues`：默认 1？
   - `ready`：默认 true 还是 false？

4. **不引入 Memory**，每轮 `iar ask` 完全独立，无会话状态。

5. **收集 3 个月只读模式误路由数据**，评估是否扩展写操作权限。

---

## 6. Acceptance Checklist

- [ ] **决策节点**：Ops Console 完成并上线后，评估是否仍有 CLI 自然语言入口的需求。
- [ ] **数据驱动**：收集至少 3 个月的只读模式使用数据（如通过 Ops Console），确认误路由率低于阈值。
- [ ] **默认值策略**：明确 `repo_id`、`agent`、`max_issues`、`ready` 等关键参数的默认值或显式策略。
- [ ] **白名单固化**：所有允许的自然语言意图必须映射到已有的、经过测试的 use case。
- [ ] **dry-run 可审计**：任何写操作在确认前展示完整的参数和预期效果，用户可独立验证。
- [ ] **Memory 决策**：明确否决或接受短期会话上下文，不接受渐进式引入。

---

> **本文档状态**：基于 deliberation `20260527-080939-116` 的共识输出，当前决策为**暂缓实现**，作为未来需求重新评估时的参考基线。
