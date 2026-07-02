# PRD 编写规范（keda）

> **PRD 的结构与规则,唯一权威源是 PRD skill：`skills/prd/SKILL.md` 及其模板 `skills/prd/templates/prd-visual-template.md`。**
> 本页不再重复 PRD 的结构定义（两段式 Part A 人审层 / Part B 执行器层、Human Review Map、Realistic Validation 的 YAML oracle 块、Acceptance Evidence Package、Decision Log 等），只保留 keda 仓库特有的**工具与流程约定**,避免两套规范并存漂移。

## 先读权威源

- PRD 结构与产出契约：`skills/prd/SKILL.md`
- 可填充模板：`skills/prd/templates/prd-visual-template.md`
- 系统级架构原则：[`docs/architecture/system-design.md`](../architecture/system-design.md)

PRD 的章节结构、Human Review Map（介入与风险地图）、Realistic Validation 的 **YAML oracle 块**（`id / behavior / real_entry / expected / mock_boundary / negative_control / expected_fail`）、Acceptance Evidence Package、Decision Log 规则等全部以 skill 为准,本页不复制。

## keda 仓库特有约定

### PRD 文件位置与命名

- 草稿 / 进行中：`tasks/pending/`
- 活跃：`tasks/` 根目录
- 已交付：`tasks/archive/`
- 文件名支持旧格式 `*-prd-*.md` 与优先级格式 `P0/P1/P2/P3-<TYPE>-YYYYMMDD-HHMMSS-<slug>.md`。

### Acceptance Checklist 门禁（pre-commit）

本仓库通过 `pre-commit` 本地 hook（`hooks/check_prd_acceptance_checklist.py`）检查 PRD 的 `Acceptance Checklist` 章节是否仍有未勾选项：

- 检查范围：`tasks/` 根目录下的活跃 PRD；新增 / 复制 / 重命名进入 `tasks/archive/` 的归档 PRD。
- `tasks/pending/` 下的草稿不检查；历史 archive PRD 的普通修改不被翻旧账。
- 标题支持 `Acceptance Checklist` / `验收清单` / 双语。
- 交付（归档）前,该章节条目必须全部转为完成态。

### Realistic Validation 由 runner 物化与门禁

keda 的 agent runner 会把 PRD 的 Realistic Validation oracle 物化到 Issue / PR 并做证据门禁（`extract_realistic_validation_items` / `agent_runner_validation.py`）：

- runner **优先确定性解析** skill 产出的 **YAML oracle 块**；无则回退旧式 `### Realistic Validation` 复选框（向后兼容）。
- 证据缺失 / 不达标会打回 runner 重跑（recovery 循环）。
- 进一步的"负控 + keda 复跑命令 + 独立 verifier agent"门禁,见 `tasks/pending/` 中对应的 Realistic Validation 门禁 PRD。

### PRD ↔ 任务同步

复杂任务交付前,把实际结果同步回对应 PRD：优先匹配 `tasks/` 活跃 PRD；归档前校准 Change Impact Tree、补全 Decision Log、勾完 Acceptance Checklist,再移入 `tasks/archive/`。

## 参考

- 技能说明：`skills/prd/SKILL.md`
- 可复用模板：`skills/prd/templates/prd-visual-template.md`
- 架构文档：[`docs/architecture/system-design.md`](../architecture/system-design.md)
- 原型规范入口：[`docs/prototypes/index.md`](../prototypes/index.md)
