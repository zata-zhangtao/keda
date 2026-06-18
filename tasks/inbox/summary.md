# Idea Inbox — 总结（AI 派生，可重写；事实以 ideas.md 为准）

_最后更新：2026-06-17_

## 主题聚类
- **前端 PRD 路线图可视化** — 在前端查看 PRD 的执行情况、完成/未完成状态、依赖关系、顺序与路线图；默认重点看未归档 PRD，同时可通过开关查看全部 PRD（来源：2026-06-14 19:07）
- **路线图上的 PRD 执行编排** — 路线图不只是展示，还需要支持点击开始 PRD、进入 review 时高亮、跳转 PR、合并后推进下一个；开始 PRD 应真正触发工作，包括 GitHub label/level 状态变化与本地 worktree 创建（来源：2026-06-14 19:41、2026-06-14 19:45）
- **人机分工与现有 iar 封装** — 该工作流应封装现有 iar / PR 自动化流程，由 Agent 驱动实现、review/PR 等状态流转；人主要负责确认开始和审阅 PR，而不是手动处理每个内部步骤（来源：2026-06-14 19:46、2026-06-14 19:48、2026-06-14 19:54）
- **依赖感知的全局/单个启动模式** — 支持全局开始与单个开始两种方式：全局开始可设置并行数量，无依赖 PRD 可并行执行，有依赖 PRD 等上游审阅并合并到主分支后再开始；单个 PRD 也可由用户手动选择启动（来源：2026-06-14 19:53）
- **Idea Inbox 前端化与跨平台采集** — 将 idea_inbox 放到前端里，作为 PRD Roadmap 的上游入口，并与 Roadmap 分开；后续接入 Cloud/Codex 类交互能力和飞书等跨平台入口，让想法能自动记录到对应项目，并进一步创建 PRD（来源：2026-06-14 20:15）
- **Realistic Validation Evidence 可信度增强** — 当前证据 comment 不够直观：证据没有明确关联到具体检查项，语言默认英文而非项目语言/中文，且缺少"为什么该证据能证明检查项成立"的解释，存在虚假或弱证据通过的风险（来源：2026-06-14 20:24）
- **PR 后自动部署与审阅预览** — PR 提交后可把应用部署到用户配置的远程服务器，供人在审阅时通过对应网址查看真实运行效果；服务器密钥可通过环境变量提供，部署可考虑模板化并传入 commit 等信息（来源：2026-06-14 21:16）
- **默认 Docker + Traefik 部署模板** — 部署默认可采用 Docker + Traefik；可参考/复用 `zata-ops` 项目中的部署脚本，将其封装为当前项目可用的部署工具（来源：2026-06-14 21:17）
- **审阅环节邮件通知** — 当 PRD/Issue 进入审阅环节时自动发送邮件通知用户（来源：2026-06-15 00:41）
- **CLI 运营输出透传** — 当前对原始 CLI（agent/iar）的封装看不到实时运营输出，需要改进输出透传与展示方式（来源：2026-06-15 00:48）
- **路线图依赖图/流程图视图** — 在路线图页面新增拓扑视图，用节点和连线展示 PRD 依赖关系，支持主链路与并行分支，作为现有列表/时间轴视图的补充（来源：2026-06-15 00:50）
- **Agent 上下文注入可视化与可控性** — 用户当前看不到 runner agent / supervisor / review 等各类 agent 在执行时实际被注入的上下文（system prompt、PRD 摘要、PR 上下文等），需要在终端/日志/前端给出注入上下文的可审计视图，避免"黑盒执行"（来源：2026-06-15 09:30）
- **模型额度触底时的自动切换** — Claude Code plan 额度触顶后所有任务失败；希望 iar / agent-runner 在检测到额度错误时能通过 cc-switch（或等价机制）自动切换到备用模型继续执行（来源：2026-06-17 09:44）

## 可执行候选
- ~~前端路线图 + 交互式 PRD 执行编排~~ → **已归档**：`tasks/archive/P1-FEAT-20260614-200054-frontend-prd-roadmap.md`（PR #81，2026-06-14 合并），路线图展示、开始按钮、review 高亮、依赖调度与 iar 封装属于同一条 PRD 生命周期体验
- ~~Idea Inbox 前端化与跨平台采集~~ → **已归档**：`tasks/archive/P1-FEAT-20260614-203810-frontend-idea-inbox-cross-platform.md`（PR #89，2026-06-14 合并）
- ~~Realistic Validation Evidence 结构化可信度增强~~ → **已归档**：`tasks/archive/P1-FEAT-20260614-203811-structured-validation-evidence.md`（PR #88，2026-06-14 合并）
- ~~PR 审阅预览部署能力（含 Docker+Traefik 部署模板首切片）~~ → **已归档**：`tasks/archive/P1-FEAT-20260614-224914-pr-preview-deployment.md`（PR #91，2026-06-14 合并）
- 部署脚本模板/工具封装 → 已并入上一条 PRD 作为首个交付切片（`deploy/vps-traefik/` 复用既有约定），理由：用户已给出偏好"默认 Docker + Traefik"并指出 `zata-ops` 有可复用脚本；先做模板化封装可降低后续多项目部署接入成本（来源：2026-06-14 21:17）
- 审阅环节邮件通知 → 建议 PRD：P?-FEAT，理由：与 review 工作流、PR 生命周期耦合，范围小但涉及外部邮件服务/模板/配置，适合独立 PRD 或并入 PRD 生命周期 PRD 作为通知切片（来源：2026-06-15 00:41）
- CLI 运营输出透传与封装改进 → 建议 PRD：P?-FEAT，理由：影响 `process_runner`、`transcript_runner`、`iar` CLI 以及可能的前端日志拉取，需要先明确是透传原始输出还是结构化展示再成 PRD（来源：2026-06-15 00:48）
- 路线图依赖图/流程图视图 → 建议并入现有 Roadmap PRD 或作为独立 UI 增强 PRD，理由：是 Roadmap 的第三种视图（与列表/时间轴并列），依赖已有 PRD 数据模型，适合作为 Roadmap PRD 的后续切片（来源：2026-06-15 00:50）
- Agent 上下文注入可视化 → 建议 PRD：P?-FEAT，理由：用户痛点是"黑盒执行"，现有 unified-ops-console PRD 只覆盖了运行日志/进程/审计，但没有暴露"实际注入到 runner / supervisor / review 的 prompt 与上下文内容"；需要在 `transcript_runner` / `process_runner` 增加上下文快照落盘，并在 ops console 或独立页面给出可审计视图；同时需定义"上下文快照"的脱敏边界（来源：2026-06-15 09:30）
- cc-switch 模型自动切换 → 建议 PRD：P?-FEAT，理由：解决 code plan 额度触底后整体失败的硬性问题，需要先调研 cc-switch 是否提供 CLI / API 接口（如无接口，需评估"调用 `cc-switch` CLI"或"通过 `config.toml` 配置多个模型 fallback"两种替代路径），再决定落地方案；建议作为独立 PRD 而非塞进 ops console（来源：2026-06-17 09:44）

## 待澄清问题
- CLI 输出封装的目标：是透传原始 agent CLI 输出，还是提供结构化运营进度？是否需要输出到前端实时展示？（来源：2026-06-15 00:48）
  - **已确认**：先提供"结构化运营进度"通道，原始输出作为可切换的调试模式。短期在终端/日志中输出带时间戳的工具调用、当前文件、验证结果等关键事件；中长期通过统一输出通道推送到前端，让 roadmap/管理端能实时订阅。
- 邮件通知：支持哪些阶段，邮件服务用哪种方式？（来源：2026-06-15 00:41）
  - **已确认**：覆盖 review/failed/blocked 三个阶段；邮件服务使用 SMTP，通过环境变量配置。
- 依赖图视图：节点是否包含已归档 PRD？是否允许在图上直接触发开始/继续操作？连线是否需要区分依赖类型？（来源：2026-06-15 00:50）
  - **已确认**：节点默认只展示未归档 PRD，提供"显示已归档"开关；图上允许直接触发开始/继续（与列表视图行为一致）；连线先统一表达 `delivery_dependencies`，后续如果加入 group/depends_on_group 等更复杂的依赖类型再考虑样式区分。
- Agent 上下文注入可视化的范围与脱敏边界：需要展示哪些 agent（runner / supervisor / review / verification / rebase …）？完整 prompt 还是仅结构化元数据？是否需要在落盘前对 secrets / token 做脱敏？（来源：2026-06-15 09:30，未确认）
- cc-switch 切换能力：cc-switch 是否提供受支持的接口（CLI 子命令 / HTTP / 配置文件约定）？如无接口，是否接受"在 iar 配置中声明多个模型 fallback 链 + 命中额度错误自动切换"的替代方案？切换是否需要保留同一会话的上下文（来源：2026-06-17 09:44，未确认）

## 已归档（本周期完成的 PRD）
- 前端 PRD 路线图与交互工作流 → `tasks/archive/P1-FEAT-20260614-200054-frontend-prd-roadmap.md`（PR #81，2026-06-14 合并；路线图展示、全局/单个开始、依赖调度、review 高亮、iar 封装；30+ 项 Acceptance 全部完成）
- Idea Inbox 前端化与跨平台采集 → `tasks/archive/P1-FEAT-20260614-203810-frontend-idea-inbox-cross-platform.md`（PR #89，2026-06-14 合并；append-only inbox、AI 草稿生成、前端草稿确认；29 项 Acceptance 全部完成）
- Realistic Validation 结构化证据可信度增强 → `tasks/archive/P1-FEAT-20260614-203811-structured-validation-evidence.md`（PR #88，2026-06-14 合并；证据按检查项结构化、中文/项目语言、理由说明；30 项 Acceptance 全部完成）
- PR 审阅预览部署能力（含 Docker+Traefik 部署模板首切片）→ `tasks/archive/P1-FEAT-20260614-224914-pr-preview-deployment.md`（PR #91，2026-06-14 合并；每 PR 一套临时 preview 栈、sticky 评论回传预览 URL；23 项 Acceptance 全部完成）

## 历史升级记录
- ~~前端 PRD 路线图与交互工作流 → `tasks/pending/P1-FEAT-20260614-200054-frontend-prd-roadmap.md`（来源：2026-06-14 19:07、2026-06-14 19:41、2026-06-14 19:45、2026-06-14 19:46、2026-06-14 19:48、2026-06-14 19:53、2026-06-14 19:54）~~ → 已合并归档，详见上节
- ~~Idea Inbox 前端化与跨平台采集 → `tasks/pending/P1-FEAT-20260614-203810-frontend-idea-inbox-cross-platform.md`（来源：2026-06-14 20:15）~~ → 已合并归档，详见上节
- ~~Realistic Validation 结构化证据可信度增强 → `tasks/pending/P1-FEAT-20260614-203811-structured-validation-evidence.md`（来源：2026-06-14 20:24）~~ → 已合并归档，详见上节
- ~~PR 审阅预览部署能力（含 Docker+Traefik 部署模板首切片）→ `tasks/pending/P1-FEAT-20260614-224914-pr-preview-deployment.md`（来源：2026-06-14 21:16、2026-06-14 21:17）~~ → 已合并归档，详见上节
