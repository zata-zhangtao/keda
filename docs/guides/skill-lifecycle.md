# Skill Lifecycle

本仓库对 skill 的默认治理规则是：一个阶段只允许一个主 skill。
目的不是限制工具使用，而是避免在同一阶段持续叠加 skill，最后让职责边界变得模糊。

## Default Policy

1. 一个阶段只允许一个主 skill。
2. 只有当前 skill 明确无法覆盖该阶段职责时，才考虑未来拆分。
3. “新增了一个检查项”本身不是新增 skill 的理由。
4. 先约束阶段边界，再讨论是否需要新的 skill。

## Phase Map

| 阶段 | 主 skill | 进入条件 | 退出产物 | 默认不串用 |
|---|---|---|---|---|
| Pre-planning | `requirement-sanity-reviewer` | 需求有歧义、冲突、边界不清、验收缺失 | 风险、假设、澄清结论 | `code-reviewer` |
| Planning | `prd` | 需要形成可执行方案或 PRD | `tasks/[YYYYMMDD-HHMMSS]-prd-[feature-name].md` | `code-reviewer` |
| Implementation | 无 | 需求和方案已明确，进入编码 | 代码、测试、文档改动 | 默认不引入额外主 skill |
| Pre-merge review | `code-reviewer` | 实现完成，准备合并前审查 | review findings、severity verdict、requirement/validation/docs 状态 | 第二个 reviewer skill |

## Phase Details

### Pre-planning

当问题首先出在需求本身时，优先用 `requirement-sanity-reviewer`。
这一阶段的目标不是设计实现，而是识别：

- 歧义
- 自相矛盾
- 隐含信任边界
- 缺失的验收标准
- 不现实的范围假设

如果这些问题没有先解决，后续 planning 和 implementation 通常会建立在错误前提上。

### Planning

当需求已足够清晰，需要形成实施方案时，使用 `prd`。
这一阶段应输出可执行的 PRD，而不是直接进入代码实现。

规划阶段的目标：

- 找到最接近的现有路径
- 比较最小改动方案与更重方案
- 定义影响文件、边界、验证、非目标

### Implementation

实现阶段默认不要求额外主 skill。
重点是保持主路径流畅，完成：

- 代码改动
- 测试补充
- 文档同步

只有在任务本身明确命中某个专用 skill 的职责时，才按需使用该 skill，而不是为了“更保险”不断叠加。

### Pre-merge Review

合并前审查默认只使用 `code-reviewer` 作为主 skill。
这是交付闸门，而不是需求澄清或方案设计阶段的延续。

`code-reviewer` 在该阶段需要同时检查：

- requirement alignment
- code quality and security
- validation evidence
- docs synchronization

## When To Split A Skill

以下情况同时持续出现时，才考虑把一个阶段拆成新的稳定 skill：

- 现有 skill 已明显过长且执行不稳定
- 新职责已经形成独立、重复出现的阶段
- 新 skill 的输入、输出、触发条件都能单独定义
- 拆分后能减少混乱，而不是制造新的职责重叠

## Current Repository Decision

当前仓库的 reviewer 路线是：

- 保持 `code-reviewer` 作为 pre-merge review 的唯一主 skill
- 在该 skill 内补齐 requirement、validation、docs 三类 gate
- 不新增并行的 `delivery-reviewer`

这能以最小改动提升交付完整性，同时避免 skill 体系膨胀。
