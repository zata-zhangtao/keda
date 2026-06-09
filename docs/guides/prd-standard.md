# PRD 编写规范（架构优先版）

本规范用于统一本项目的 PRD 产出质量，目标不是把 PRD 写得更长，而是让方案更贴合现有架构、更少引入冗余实现。
所有 PRD 文件统一保存到 `tasks/[YYYYMMDD-HHMMSS]-prd-[feature-name].md`。

## 核心原则

1. 先看仓库，再谈方案。代码库是第一信源。
2. 先做架构判断，再写图表和模板区块。
3. 默认推荐最小改动方案，而不是最完整、最新潮、最“像架构升级”的方案。
4. 新增模块、服务、表、依赖、页面都必须先过“冗余闸门”。
5. 默认推荐一次性交付目标态，而不是先落一个“阶段一过渡态”再把关键工作丢给后续。
6. Web 搜索是条件能力，只用于补充仓库中不存在的外部事实。

## 必做分析

每份 PRD 在正式成稿前，必须先明确：

- 当前最接近的现有代码路径是什么
- 哪些模块可以直接复用或扩展
- 现有职责边界和依赖方向是什么
- 如果新增抽象，最可能重复掉的现有职责是什么

如果这些问题没有回答清楚，不应直接进入实现方案细节。

## 强制区块

每份 PRD 必须包含以下内容：

1. **Requirement Shape**
2. **Repository Context And Architecture Fit**
3. **Options And Recommendation**
4. **改动清单表（Change Matrix）**
5. **Mermaid 流程图或架构图**
6. **Definition Of Done**
7. **Acceptance Checklist（独立验收清单）**
8. **Non-Goals**
9. **Decision Log**（见下方规则）

以下内容是条件必填：

- **低保真原型图**：仅当需求依赖 UI 布局或多步交互时
- **ER 图**：仅当涉及数据模型或持久化状态变更时
- **External Validation**：仅当确实做了 Web 搜索时
- **Interactive Prototype Change Log**：仅当确实修改了 `docs/prototypes/` 下文件时

## 最小改动优先

每份 PRD 至少要比较两个方案：

1. **最小改动方案**
   直接扩展现有模块、边界和数据流。
2. **更重方案**
   引入新抽象、新层次、新依赖或新入口。

默认推荐最小改动方案。
只有在更重方案明显更安全、更一致、或能避免结构性问题时，才允许推荐它。

如果推荐更重方案，PRD 必须明确写出：

- 为什么现有路径不够用
- 为什么不是在重复已有职责
- 新增复杂度是什么
- 是否能同时删掉旧逻辑或避免并存

## 目标态要求

PRD 默认应描述**完整目标态**，不要把必做迁移、旧入口删除、契约收口这类工作随手丢到 `Phase 2` 或 `Follow-Ups`。

如果确实必须分阶段，至少要写清：

- 为什么现在不能一次完成
- 最终目标态是什么
- 第一阶段后哪些条件必须成立，避免过渡态变成永久遗留层

## Change Matrix 格式规则

Change Matrix 应在 PRD 初稿阶段写入，作为 **Planned Change Matrix** 使用。
它记录实现前预期会修改的领域、文件、动作、摘要和关键子变更，用来提前暴露范围、复用点、依赖边界和评审风险。

### 格式选择

简单、小范围 PRD 可以使用表格格式。
涉及多个目录、多个架构层、数据库迁移、测试和文档联动的 PRD，必须使用树状文本格式。

### 表格格式模板

| 改动对象 | 当前状态 | 目标状态 | 修改方式 | 为什么符合现有架构 | 影响文件 |
|---|---|---|---|---|---|
| `[path/to/file]` | [现状] | [目标] | [怎么改] | [复用/边界说明] | [影响文件] |

### 树状文本格式模板

```text
Domain
├── path/to/file.py
│   [新增|修改|删除|验证]
│   【总结】一句话说明该文件的目标变化
│
│   ├── 关键子变化 1
│   ├── 关键子变化 2
│   └── 关键子变化 3
```

优先按架构或职责分组，而不是按文件名排序。常用分组包括：

- Database
- API
- Core
- Engines
- Infrastructure
- Scripts
- Tests
- Docs

每个文件块必须包含：

- 文件路径
- 动作标记：`[新增]`、`[修改]`、`[删除]`、`[验证]`
- `【总结】`：一句话说明这个文件为什么要变
- 关键子变更：只写会影响实现、Review 或验收的点，不写流水账

### 生命周期

实现完成并归档 PRD 前，必须校准 Change Matrix：

- 实际未修改的文件应删除，或标记为 `[未改]`
- 实际新增但 PRD 未预判的文件应补入
- 如果实现路径偏离原计划，应同步更新 Change Matrix 和 Decision Log
- Acceptance Checklist 应按校准后的最终状态验收

## 流程图模板

```mermaid
flowchart TD
    A[需求输入] --> B[仓库上下文分析]
    B --> C[识别现有边界与复用点]
    C --> D[最小改动方案]
    D --> E[验证与交付]
```

图的重点应是“改动落在哪个现有边界里”，不是单纯把需求翻译成流程图。

## 原型图规则

低保真原型图不是默认必填项。
只有以下情况才需要补：

- 需求强依赖页面布局
- 需求强依赖多步状态切换
- 没有原型就无法说清范围边界

若不需要，应在 PRD 中明确写出：
- `No low-fidelity prototype required for this PRD.`

## 交互原型（可操作 UI）规则

只有在以下情况才创建或修改 `docs/prototypes/` 下文件：

- 用户明确要求 prototype / wireframe / demo
- 静态图和文字不足以表达待评审行为

禁止因为“这是个 UI 需求”就自动创建交互原型。

如果确实需要：

- 原型页面路径：`docs/prototypes/*.html`
- 资源路径：`docs/prototypes/assets/`
- 若用户明确指定原型页面名称或路径，必须优先使用该目标
- PRD 中必须写出原型入口路径和用途
- PRD 中必须增加 `Interactive Prototype Change Log`

如果没有实际改动原型文件，PRD 中应明确写出：
- `No interactive prototype file changes in this PRD.`

## ER 图触发条件

当存在以下任一情况时，必须提供 Mermaid `erDiagram`：

- 新增实体、表或模型
- 修改字段或关系
- 修改持久化状态结构

若无数据模型变更，需在 PRD 明确写出：
- `No data model changes in this PRD.`

## 验收清单规则

`Definition Of Done` 和 `Acceptance Checklist` 不是一回事：

- `Definition Of Done` 用来描述整体交付闸门与完成态。
- `Acceptance Checklist` 必须作为独立章节存在，用来列出可逐项验证的验收条件。
- 不得用 `Definition Of Done` 代替验收清单。
- 不得只依赖 `User Stories` 下的 `Acceptance Criteria`，然后省略独立验收清单。
- 验收清单默认必须验证最终目标态，而不是只验证 `Phase 1` 是否完成。

推荐使用分组 checklist。对架构调整、重构、目录迁移类 PRD，优先按以下分组：

- `Architecture Acceptance`
- `Dependency Acceptance`
- `Behavior Acceptance`
- `Documentation Acceptance`
- `Validation Acceptance`

每条验收项都应当是可验证的具体结果，优先写：

- 明确文件或目录路径
- 明确 API path / contract / 语义约束
- 明确依赖方向或禁止依赖
- 明确验证命令
- 明确仓库搜索断言（例如确认旧入口、兼容层、重复实现已不存在）

仓库当前会通过 `pre-commit` 的本地 hook 检查 PRD 的 `Acceptance Checklist` 章节是否仍存在未勾选项，因此这里的条目应当在交付前全部转为完成态。
检查范围包括：

- `tasks/` 根目录下的活跃 PRD
- 新增、复制或重命名进入 `tasks/archive/` 的归档 PRD

`tasks/pending/` 下的草稿 PRD 不检查；历史 archive PRD 的普通修改也不会被新规则重新翻旧账。标题可使用 `Acceptance Checklist`、`验收清单` 或双语标题。
检查范围内的 PRD 文件名支持旧格式 `*-prd-*.md` 和优先级格式 `P0/P1/P2/P3-<TYPE>-YYYYMMDD-HHMMSS-<slug>.md`。

格式参考：

```markdown
## 7. Acceptance Checklist

### Architecture Acceptance

- [ ] [明确的目录、边界或唯一入口结果]

### Validation Acceptance

- [ ] `uv run pytest ... -q` 通过
- [ ] 搜索仓库后，不存在旧入口或残留兼容层引用
```

## PRD 完成同步

复杂任务完成前，必须把实际交付结果同步回对应 PRD：

- 优先在 `tasks/` 中查找匹配的 active PRD
- 只有当 archived PRD 明确就是对应任务记录时，才把 `tasks/archive/` 作为主目标
- 如果找到匹配 PRD，应更新实际交付内容、执行过的验证命令、偏离原计划的地方和必要 follow-up
- 如果没有匹配 PRD，应创建 `tasks/[YYYYMMDD-HHMMSS]-prd-[feature-name].md`
- PRD 归档前必须校准 Change Matrix；如果实现路径变化，也必须同步更新 Decision Log
- Acceptance Checklist 应按最终实现状态全部完成后再归档

## Decision Log 规则

Decision Log 是本项目替代独立 ADR 目录的决策记录机制。
PRD 归档后，Decision Log 随之永久保存在 `tasks/archive/`，成为持久参考。

### 为什么放在 PRD 里而不是单独的 ADR 文件

任务驱动的项目中，决策和实现是同一件事的两面。把决策记录嵌入 PRD，可以：

- 保留完整上下文（决策 + 方案 + 实现）
- 避免维护两套文档（PRD 和独立 ADR 目录）
- 随任务归档自然沉淀，不需要额外管理

### 格式

```markdown
## 12. Decision Log

| # | 决策问题 | 选择 | 放弃的方案 | 理由 |
|---|---|---|---|---|
| D-01 | [决策问题] | [最终选择] | [放弃的方案] | [一句话具体理由] |
```

### 填写规则

- **每个 Section 4 的 Option A/B 取舍必须对应至少一行。**
- **选择**列必须与 Section 4 的推荐项一致。
- **放弃的方案**必须具名，不得写"其他方案"。
- **理由**必须具体，不得写"符合架构"——应写"因为 X 无法满足 Y，所以选 Z"。
- ID 从 D-01 开始顺序编号，同一 PRD 内不重复。

### 示例

| # | 决策问题 | 选择 | 放弃的方案 | 理由 |
|---|---|---|---|---|
| D-01 | 架构模式 | Clean Architecture 四层 | 扁平 utils 工具集 | 需要支持多模型/多技能编排，扁平结构无法隔离业务规则与具体实现 |
| D-02 | 部署单元 | 模块化单体 | 微服务拆分 | 当前团队规模和流量不需要独立部署，过早拆分只增加运维成本 |

### 与架构文档的关系

Decision Log 记录**某次功能变更时做了什么决策**。
系统级架构原则记录在 [`docs/architecture/system-design.md`](../architecture/system-design.md)。
两者互补：前者是时间轴上的决策快照，后者是当前架构的权威描述。

---

## Web 搜索使用规则

Web 搜索不是默认步骤，只在这些场景启用：

- 第三方 API、SDK、云产品能力确认
- 安全规范、标准、法规
- 依赖版本行为或平台能力差异
- 用户明确要求竞品或行业方案调研

搜索时应遵守：

- 优先官方文档和一手资料
- 搜索结果只用于补充约束、兼容性和风险
- 不得用外部“最佳实践”覆盖仓库已有合理模式
- 所有外部结论必须在 PRD 中注明来源和检查日期

如果不需要搜索，应在 PRD 中明确写出：
- `No external validation required; repository evidence was sufficient.`

## 推荐流程

1. 先把需求改写成明确的行为声明。
2. 扫描代码结构，识别最接近的现有路径和复用点。
3. 先比较最小改动方案与更重方案，再决定推荐项。
4. 确认是否真的需要原型、ER 图、或 Web 搜索。
5. 写 Planned Change Matrix、流程图、Definition Of Done、Acceptance Checklist、User Stories、FR 和 Non-Goals。
6. **填写 Decision Log**：将第 3 步的每个 Option A/B 取舍转换为一行记录。

## 参考

- 技能说明：`skills/prd/SKILL.md`
- 可复用模板：`skills/prd/templates/prd-visual-template.md`
- 示例 PRD：`tasks/archive/prd-visual-change-spec.md`
- 架构文档：[`docs/architecture/system-design.md`](../architecture/system-design.md)
- 原型规范入口：[`docs/prototypes/index.md`](../prototypes/index.md)
