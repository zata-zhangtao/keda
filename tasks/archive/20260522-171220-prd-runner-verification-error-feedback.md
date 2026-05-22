# PRD: Runner Verification Error Feedback

## 1. Introduction & Goals

当前 `just test` 在内部调用 `just lint --full` 时使用了 `>/dev/null 2>&1`，将 lint 错误输出完全丢弃。这导致 Agent Runner 在 staged verification 失败时，recovery prompt 只能向 agent 传递模糊的 "❌ Lint failed. Fix lint errors before running tests." 信息。agent 看不到具体的 ruff 报错（哪个文件、哪一行、什么规则），只能盲目重试 `just lint --full`，而由于 agent 与 runner 的环境差异（staged archive PRD 触发强制完整 lint、flag 缓存状态等），agent 自行运行的结果可能与 runner 不一致，最终耗尽 recovery 次数后任务失败。

本 PRD 的目标是：移除 `just test` 对 lint 输出的重定向，让 lint 错误能被正常捕获；同时提升 runner recovery output 长度限制，确保详细错误信息能完整进入 recovery prompt，使 agent 收到 recovery 指令后能直接定位并修复问题。

### Measurable Objectives

- `just test` 在 lint 失败时，stdout/stderr 中包含具体的 pre-commit/ruff 错误详情（文件路径、行号、规则代码、错误描述）。
- Agent Runner 的 `run_verification` 捕获到的 `just test` 输出包含可直接操作的错误信息。
- Recovery prompt 中 lint 错误信息不被过早截断。
- pre-commit 的 ANSI color escape codes 不污染 recovery prompt。

## 2. Requirement Shape

| Dimension | Requirement |
|---|---|
| Actor | Agent Runner、`run_agent_once.py`、AI Agent（recovery 场景）、本地开发者（手动运行 `just test`） |
| Trigger | `just test` 内部 `just lint --full` 失败；runner 调用 `commit_requested_changes` 时 verification 失败，进入 recovery 流程 |
| Expected behavior | lint 失败的完整输出通过 stdout/stderr 正常传递；runner 将包含具体错误的输出写进 recovery prompt；agent 能根据错误信息直接修改代码 |
| Explicit scope boundary | 不改 `just lint --full` 的 lint 逻辑；不改 pre-commit hooks 配置；不改 runner 的 recovery 流程结构（只改输入内容的完整度）；不引入新的 just 命令 |

## 3. Repository Context And Architecture Fit

### Current Relevant Modules

| File | Current Responsibility | 改动点 |
|---|---|---|
| `justfile` | `just test` recipe，内部调用 `just lint --full` | 移除 `>/dev/null 2>&1`；可选设置 `PRE_COMMIT_COLOR=never` |
| `src/backend/core/use_cases/run_agent_once.py` | runner verification、recovery prompt 构建、failure summary 截断 | 提升 `_MAX_RECOVERY_OUTPUT_LENGTH` |
| `docs/guides/agent-runner.md` | Agent Runner 使用文档 | 如有涉及 `just test` 输出行为的描述则同步更新 |

### Existing Path

当前 runner 的 staged verification 失败路径：

```text
run_agent_until_committed
  -> commit_requested_changes
    -> run_verification (运行 ["just", "test"])
      -> just test 内部: just lint --full >/dev/null 2>&1
        -> lint 失败，exit 1，但错误详情被丢弃
    -> VerificationFailedError 抛出
      -> format_recovery_failure_summary
        -> 只有 "❌ Lint failed..." 进入 recovery prompt
  -> build_recovery_prompt (agent 收到模糊错误，盲目重试)
```

### Labels Decision

本 PRD 不引入新 label，不修改现有 label 语义。

## 4. Implementation Plan

### 4.1 `justfile` — 移除 lint 输出重定向

**文件**: `justfile:737`

**修改前**:
```bash
echo "🔍 Running full lint checks..."
if ! SKIP=check-test-flag just lint --full >/dev/null 2>&1; then
    echo "❌ Lint failed. Fix lint errors before running tests."
    echo "   Run: just lint --full"
    exit 1
fi
```

**修改后**:
```bash
echo "🔍 Running full lint checks..."
if ! SKIP=check-test-flag PRE_COMMIT_COLOR=never just lint --full; then
    echo "❌ Lint failed. Fix lint errors before running tests."
    echo "   Run: just lint --full"
    exit 1
fi
```

**变更说明**:
- 移除 `>/dev/null 2>&1`，让 `just lint --full` 的完整输出（包括 pre-commit 各 hook 的失败信息、ruff 报错、diff）正常流入 stdout/stderr。
- 增加 `PRE_COMMIT_COLOR=never` 环境变量，禁用 pre-commit 的 ANSI color codes，避免 escape sequences 污染 recovery prompt。
- lint 成功时，`just lint --full` 原有的 `"✅ just lint --full flag updated..."` 输出会正常显示，不影响阅读。

### 4.2 `run_agent_once.py` — 提升 recovery output 截断阈值

**文件**: `src/backend/core/use_cases/run_agent_once.py`

**修改前**:
```python
_MAX_RECOVERY_OUTPUT_LENGTH = 4000
```

**修改后**:
```python
_MAX_RECOVERY_OUTPUT_LENGTH = 12000
```

**变更说明**:
- pre-commit 在 `--show-diff-on-failure` 模式下，多个文件失败时的输出很容易超过 4000 字符。
- 提升到 12000 字符，确保 recovery prompt 能容纳完整的 lint 错误信息，同时避免无限增长。

### 4.3 文档同步

**文件**: `docs/guides/agent-runner.md`

如有描述 `just test` 行为的段落，更新为说明 `just test` 在 lint 失败时会直接输出 lint 错误详情，无需再次手动运行 `just lint --full` 查看。

## 5. Acceptance Checklist

- [x] `justfile` 中 `just test` 调用 `just lint --full` 时不再使用 `>/dev/null 2>&1` 重定向。
- [x] `just test` 在 ruff 失败时显示具体错误（包含文件路径、行号、规则代码、错误描述）。
- [x] `just test` 在 pre-commit 多个 hook 失败时显示所有失败详情（不只是第一个）。
- [x] `just test` 中设置了 `PRE_COMMIT_COLOR=never`（或通过其他方式禁用 pre-commit ANSI color）。
- [x] `run_agent_once.py` 的 `_MAX_RECOVERY_OUTPUT_LENGTH` 提升到至少 12000。
- [x] `just test` 在 lint 成功时的输出不因此变混乱（保留原有 "✅ Lint passed" 提示）。
- [x] `just lint --full` 在手动运行时和 `just test` 内部调用时行为一致（除 flag 跳过逻辑外）。
- [x] `just test` 和 `just lint --full` 均通过本地验证（`just test` 通过）。
- [x] 如有相关文档描述则同步更新。
