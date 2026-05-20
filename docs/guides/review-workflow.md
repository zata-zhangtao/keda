# Review Workflow

本仓库的 review 目标不是只找“代码味道”，而是确认一次改动是否真的达到可交付状态。
合并前 review 默认由 `code-reviewer` 作为唯一主 skill 承担，不额外叠加第二个 reviewer skill。

## Review Scope

一次完整 review 至少覆盖四类问题：

1. 需求是否对齐
2. 代码是否安全、稳定、可维护
3. 是否存在足够的验证证据
4. 文档是否与实现同步

如果只检查代码样式，而不检查需求、验证、文档，review 结论通常不够支撑合并决策。

## Recommended Flow

### 1. 收集改动上下文

先读取当前 diff 或最近提交，不要只看单个片段：

- `git diff --staged`
- `git diff`
- 必要时查看最近提交和完整文件上下文

### 2. 收集需求上下文

优先确认本次改动对应的需求来源：

- 用户请求
- ticket
- `tasks/` 下的 PRD 或任务文件
- 若活动任务已归档，再回退检查 `tasks/archive/`

如果需求来源不完整，需要在 review 输出中明确写出假设，而不是默认脑补需求。

### 3. 提炼验收标准

在开始逐行审查前，先用简短列表重写本次改动必须满足的行为：

- 必须实现什么
- 明确不做什么
- 哪些接口、配置或文档是交付边界

这一步用于识别三类偏差：

- 漏实现
- 偏实现
- 超范围实现

### 4. 审查代码质量与风险

在验收标准明确后，再执行常规代码 review：

- 安全问题
- 错误处理
- 类型、命名、结构质量
- 框架与语言模式
- 测试缺口

这部分仍然重要，但不应取代交付级检查。

### 5. 检查接口级验证证据

以下改动默认视为接口边界改动：

- route、handler、controller、page 入口
- request / response schema
- auth、permission、middleware、session
- 数据写路径
- 外部 API 或 webhook 集成

如果触及这些边界，review 时需要查找至少一种接口级证据：

- API / integration test
- HTTP 级测试
- e2e 覆盖
- `curl`、`httpie` 或类似 smoke 测试记录

只有单元测试时，通常不能自动视为通过。

### 6. 检查仓库标准验证命令

review 需要明确披露这些命令是否执行过：

- `just lint`
- `just lint --reuse`（涉及复用、架构、AI 规范入口或重复风险时）
- `just lint --full` 或 `just lint --repo`
- 与改动相关的测试
- `uv run mkdocs build`（涉及文档时使用 `--strict`）

建议使用以下状态：

- `passed`
- `partial`
- `not run`
- `blocked`

不要因为“理论上应该能过”而省略验证状态。

### 7. 检查文档同步

当改动涉及行为、接口、配置或操作命令时，需要同步确认：

- `docs/` 页面是否更新
- 新文档是否加入 `mkdocs.yml`
- 公共 Python API 的 docstring 是否更新
- README、示例命令、环境变量说明是否仍然准确

文档未同步时，应作为独立问题输出，而不是附带一笔带过。

## Output Contract

review 输出应区分四类 finding：

- `requirement`
- `code`
- `validation`
- `docs`

结尾 summary 至少要包含：

- 按 severity 统计的问题数量
- `Requirement Status`
- `Validation Status`
- `Docs Status`
- 已检查的命令及其状态
- 最终 verdict

## Merge Decision Guidance

- `Approve`: 没有 CRITICAL，没有未解决的 HIGH，且 requirement / docs 状态不失败
- `Warning`: 存在 HIGH 或验证仅部分完成，但风险已明确披露
- `Block`: 存在 CRITICAL、需求缺口、或高风险接口改动缺少验证证据

## Non-Goals

本流程不要求：

- 新增并行 reviewer skill
- 新增 CI 工作流
- 引入新的测试框架
- 为了 review 包装一层只会调用现有命令的新脚本
