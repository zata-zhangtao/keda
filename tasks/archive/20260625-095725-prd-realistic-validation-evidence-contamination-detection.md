# PRD: Realistic Validation Evidence Contamination Detection

- GitHub Issue: （由失败复盘触发，参考 freshai issue #19 的证据串味问题）

## 1. Introduction & Goals

### Problem Statement

当前 Agent Runner 的 Realistic Validation 门禁主要检查：

1. 证据目录非空。
2. 每个 checklist item 都有命名符合 `rv-<item_number>-<slug>.<ext>` 的证据文件。
3. `evidence.json` manifest 字段完整、item 覆盖、文件存在且编号一致。

但它**不检查证据文件内部内容**。在 freshai issue #19 的实践中，agent 使用 bash 的 `exec > >(tee -a ...)` 全局 stdout 重定向生成证据，结果 `rv-1`、`rv-2`、`rv-3` 三个文件内容互相串味——每个文件都包含了 Item 1/2/3 的全部输出。runner 在发布前没有拦截，Pre-PR Review 阶段才被发现，导致 agent 需要二次修复并重新验证，最终因修复过程中异常退出而标为 `failed`。

本 PRD 的目标是在 keda runner 层增加**证据内容串味检测**：在 `ensure_validation_evidence_ready` 阶段就识别出文件混入其他 item 输出的情况，拒绝发布并触发 recovery，让 agent 当场重跑，而不是把脏证据带到 review。

### Goals

- 阻止不同 Realistic Validation item 的输出混入同一个证据文件。
- 在 prompt 里明确告诉 agent 正确的证据生成方式，避免使用全局 stdout 重定向。
- 保持对现有合法证据的兼容（短文本、无章节头等不受影响）。
- 降低 Pre-PR Review 阶段因证据质量问题返工的概率。

## 2. Requirement Shape

- **Actor**：Agent Runner 的验证门禁（`agent_runner_validation.py` / `agent_runner_structured_evidence.py`）。
- **Trigger**：Issue 带有 `iar:structured-evidence` marker 且进入 `ensure_validation_evidence_ready`。
- **Expected Behavior**：
  - 读取每个证据文件内容，查找 `[Item N]` / `[Item Nc]` 形式章节头。
  - 如果 `rv-1-*.txt` 里出现了 `Item 2` 或 `Item 3` 的章节头，判定为串味，抛 `ValidationEvidenceError`。
  - 如果文件只包含 foreign item 的章节头、没有自己的章节头，同样判定为串味。
  - 合法文件应只包含自身 item 的章节头，或完全不包含章节头（短 capture）。
  - 错误信息要指出具体文件名和混入的 item 编号，并提示 agent 避免 shell exec 重定向。
  - prompt suffix 增加一段中文/英文警告，明确要求每个文件只放对应 item 输出，不要用 `exec > >(tee -a ...)`。
- **Scope Boundary**：
  - 只改 keda runner 的验证与 prompt，不改目标仓库业务代码。
  - 仅对带 structured-evidence marker 的 Issue 生效；旧 Issue 保持兼容。
  - 检测基于简单的章节头正则，不做自然语言语义分析。

## 3. Repository Context And Architecture Fit

### 相关模块

| 文件 | 职责 | 改动类型 |
|---|---|---|
| `src/backend/core/use_cases/agent_runner_structured_evidence.py` | 结构化证据 manifest 解析、校验、渲染 | 新增内容串味检测函数；更新 prompt suffix |
| `tests/test_agent_runner_structured_evidence.py` | 结构化证据测试 | 新增串味场景测试与 prompt 警告测试 |

### 架构约束

- 检测逻辑属于验证层，放在 `agent_runner_structured_evidence.py` 的 `_validate_evidence_file` 调用链中，与现有文件名校验、SHA-256 计算同层。
- 不引入外部依赖，只使用标准库 `re` 和 `pathlib`。
- 保持 `ValidationEvidenceError` 统一出口，便于 runner 进入 recovery。

## 4. Recommendation

### Recommended Approach：章节头交叉检测 + Prompt 预防

1. 在 `agent_runner_structured_evidence.py` 增加正则：

   ```python
   _EVIDENCE_ITEM_SECTION_PATTERN = re.compile(
       r"\[\s*Item\s+(?P<item>\d+)(?P<sub>[a-z]?)\s*\]",
       re.IGNORECASE,
   )
   ```

2. 新增 `_validate_evidence_file_content(file_path, expected_item_number, file_name)`：
   - 读取文件文本。
   - 用正则找出所有 `[Item N]` 章节头对应的 item 编号集合。
   - 若自身 item 不在集合中且存在其他 item → 报错（整文件被其他 item 污染）。
   - 若自身 item 在集合中但存在其他 item → 报错（混入了其他 item）。
   - 若集合为空或只有自身 item → 通过。

3. 在 `validate_evidence_manifest` 的文件校验流程中调用上述函数。

4. 在 `build_structured_evidence_prompt_suffix` 中追加显式警告：
   - 每个证据文件只能包含对应 item 的输出。
   - 禁止使用 `exec > >(tee -a ...)` 等全局 stdout 重定向。

### 为什么这是最佳方案

- **低开销**：正则检测成本极低，不会显著增加验证时间。
- **高准确**：`[Item N]` 是 agent 生成证据时常用的章节头，串味时几乎必然出现；误报率低。
- **早拦截**：在 `ensure_validation_evidence_ready` 阶段就失败，runner 会触发 recovery，agent 当场重跑，不会把脏证据带到 PR。
- **向后兼容**：不含章节头的短文本证据不会被误伤。
- **Prompt 双保险**：既在机制上拦截，也在生成阶段提醒 agent。

### Alternatives Considered

| 方案 | 说明 | 拒绝原因 |
|---|---|---|
| 完全禁用 agent 自己写 bash 脚本生成证据 | 强制用 runner 提供的固定脚本 | 限制过强，不同任务证据生成方式差异大，维护成本高 |
| 用文件大小/行数异常检测串味 | 文件过大或行数过多时报警 | 阈值难定，容易误报或漏报 |
| 语义模型判断内容归属 | 用 LLM 判断文件内容属于哪个 item | 成本高、延迟大、不稳定 |
| 在 Pre-PR Review 阶段才检查证据内容 | 让 reviewer 发现串味 | 太晚了， already caused failed run |

## 5. Implementation Guide

### Core Logic

```
validate_evidence_file(file_name, expected_item_number):
    validate file name pattern
    compute sha256
    validate file content:
        found_items = extract [Item N] headers from file text
        if expected_item not in found_items and found_items is not empty:
            raise ValidationEvidenceError("file contains foreign item headers only")
        if found_items contains items other than expected_item:
            raise ValidationEvidenceError("file mixes output from multiple items")
    return EvidenceFileInfo
```

### Change Impact Tree

```text
src/backend/core/use_cases/agent_runner_structured_evidence.py
[修改]
├── 新增 _EVIDENCE_ITEM_SECTION_PATTERN 正则
├── 新增 _validate_evidence_file_content 函数
└── _validate_evidence_file 调用内容校验
└── build_structured_evidence_prompt_suffix 追加防串味警告

tests/test_agent_runner_structured_evidence.py
[修改]
├── 新增 test_build_structured_evidence_prompt_suffix_warns_against_exec_redirection
├── 新增 test_ensure_validation_evidence_ready_rejects_foreign_item_headers
└── 新增 test_ensure_validation_evidence_ready_rejects_file_with_only_foreign_headers
```

## 6. Definition Of Done

- [x] `_EVIDENCE_ITEM_SECTION_PATTERN` 能匹配 `[Item 1]`、`[Item 2c]` 等常见章节头。
- [x] `_validate_evidence_file_content` 检测并拒绝混入 foreign item 章节的证据文件。
- [x] `_validate_evidence_file_content` 检测并拒绝只包含 foreign item 章节的证据文件。
- [x] 不含章节头的合法短证据文件不会被误报。
- [x] `build_structured_evidence_prompt_suffix` 中英文版本都包含禁止 `exec > >(tee -a ...)` 的警告。
- [x] 新增单元测试覆盖上述三种场景。
- [x] `uv run pytest tests/test_agent_runner_structured_evidence.py -q` 全部通过。
- [x] `uv run pytest tests/ --no-testmon -q` 无回归失败（1380 passed）。
- [x] `just test` 通过。

## 7. Acceptance Checklist

### Architecture Acceptance

- [x] 内容校验逻辑位于 `agent_runner_structured_evidence.py`，与现有文件名校验同层。
- [x] 不引入新外部依赖。
- [x] 统一使用 `ValidationEvidenceError` 作为失败出口。

### Behavior Acceptance

- [x] `rv-1-*.txt` 中出现 `[Item 2]` 时，`ensure_validation_evidence_ready` 报错。
- [x] `rv-1-*.txt` 中只有 `[Item 2]` 时，同样报错。
- [x] `rv-1-*.txt` 中只有 `[Item 1]` 时通过。
- [x] `rv-1-*.txt` 中没有章节头时通过。
- [x] 错误信息包含文件名、混入的 item 编号、以及避免 shell exec 重定向的提示。

### Documentation Acceptance

- [x] PRD 已写入 `tasks/pending/`。
- [x] Prompt suffix 中英文均更新。

### Validation Acceptance

- [x] 新增测试通过。
- [x] 全量测试无回归。
- [x] `just test` / lint 通过。

## 8. Functional Requirements

- **FR-1**: 证据文件内容必须与其声明的 item 编号一致，不得包含其他 item 的 `[Item N]` 章节头。
- **FR-2**: 当检测到串味时，必须抛出 `ValidationEvidenceError`，触发 runner recovery。
- **FR-3**: Prompt 必须明确告知 agent：每个证据文件只放对应 item 输出，禁止使用全局 stdout 重定向（如 `exec > >(tee -a ...)`）。
- **FR-4**: 对不含章节头的短文本证据保持兼容，不强制要求文件必须包含自身 item 章节头。

## 9. Non-Goals

- 不做自然语言语义分析来判断内容归属。
- 不强制统一证据生成脚本或禁止 agent 使用 bash。
- 不影响不带 `iar:structured-evidence` marker 的旧 Issue。

## 10. Risks And Follow-Ups

| 风险 | 缓解措施 |
|---|---|
| agent 使用非 `[Item N]` 格式标题导致漏检 | 当前只覆盖最常见格式；如未来出现新格式，可扩展正则 |
| 合法证据中引用了其他 item 的标题 | `[Item N]` 作为章节头是强信号，正常说明文字中极少使用；如误报可豁免或放宽 |
| agent 用更隐蔽方式串味 | prompt 警告 + 人工 review 作为最终兜底 |

## 11. Decision Log

| ID | Decision | Chosen | Rejected | Rationale |
|---|---|---|---|---|
| D-01 | 检测位置 | `ensure_validation_evidence_ready`（发布前） | 放到 Pre-PR Review | 越早拦截，recovery 成本越低 |
| D-02 | 检测方式 | 正则匹配 `[Item N]` 章节头 | 语义模型 / 文件大小阈值 | 简单、快速、准确、成本低 |
| D-03 | 失败处理 | 抛 `ValidationEvidenceError` 触发 recovery | 直接标 failed | 给 agent 一次重跑机会，减少人工介入 |
| D-04 | Prompt 策略 | 中英文 suffix 都追加明确警告 | 只改代码不提醒 agent | 双保险，降低再犯概率 |
