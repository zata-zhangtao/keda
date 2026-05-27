# PRD: 恢复路径 Code Review 修复 & 默认 Agent 调整

## 1. Introduction & Goals

### 1.1 问题一：恢复路径跳过 Code Review

**Problem**: 当 `iar run-once` 发现 running 状态的 Issue 已有本地 commit 时，走 `_process_running_publish_recovery` 路径。该路径调用 `_finish_existing_commit_publication`，但该函数跳过了 `run_pre_push_review` 步骤，直接添加 `review` 标签并完成。这意味着从恢复路径发布的 PR 没有经过 AI Agent 的代码评审流程。

**Goal**: 修复 `_finish_existing_commit_publication`，使其与 `_finish_implementation_publication` 保持一致，在发布前执行 code review，并走 PR 后监督循环。

### 1.2 问题二：默认 Agent 从 codex 改为 claude

**Problem**: `auto` agent 选择逻辑中，当没有 label 信号且 default_agent 也是 `auto` 时，fallback 到 `codex`。

**Goal**: 将 fallback 默认值从 `codex` 改为 `claude`，与当前项目主要使用的 AI Agent 保持一致。

### Realistic Validation

- [x] **恢复路径执行 Code Review**: 通过 mock 测试验证 `_finish_existing_commit_publication` 调用了 `run_pre_push_review`
- [x] **恢复路径走监督循环**: 通过 mock 测试验证 `_run_supervisor_with_repair_loop` 被正确调用，supervising 标签被添加
- [x] **auto 默认选择 claude**: 运行 `choose_agent` 测试，验证 `auto` 参数返回 `claude`

---

## 2. Requirement Shape

### 2.1 问题一：恢复路径 Code Review

| 元素 | 内容 |
|------|------|
| **Actor** | Agent Runner |
| **Trigger** | `iar run-once` 发现 running Issue 有已就绪的本地 commit |
| **Expected Behavior** | 调用 `run_pre_push_review` 进行 code review，然后执行监督循环，最后进入 `review` 标签 |
| **Files Changed** | `src/backend/core/use_cases/agent_runner_publication.py` (从 orchestrate.py 拆分) |

### 2.2 问题二：默认 Agent 调整

| 元素 | 内容 |
|------|------|
| **Actor** | Agent Runner |
| **Trigger** | `choose_agent` 函数处理 `auto` 参数且无 label 信号 |
| **Expected Behavior** | fallback 到 `claude` 而非 `codex` |
| **Files Changed** | `src/backend/core/use_cases/run_agent_once.py`、`tests/test_run_agent.py` |

---

## 3. Repository Context And Architecture Fit

### 3.1 问题一相关文件

| 文件 | 作用 | 改动点 |
|------|------|--------|
| `src/backend/core/use_cases/agent_runner_publication.py` | 发布流程（从 orchestrate.py 拆分） | `_finish_existing_commit_publication` 新增 `run_pre_push_review` 调用和监督循环 |
| `src/backend/core/use_cases/agent_runner_supervisor.py` | PR 监督循环（新增） | 监督循环逻辑独立为单独模块 |

### 3.2 问题二相关文件

| 文件 | 作用 | 改动点 |
|------|------|--------|
| `src/backend/core/use_cases/run_agent_once.py` | Agent 选择逻辑 | `choose_agent` fallback 从 `codex` 改为 `claude` |
| `tests/test_run_agent.py` | Agent 测试 | 更新 `test_choose_agent_defaults_to_codex` 为 `test_choose_agent_defaults_to_claude`（函数名已改为 `_to_claude`）|

### 3.3 架构一致性

- **Code Review 流程**: 恢复路径现在与新实现路径保持一致，都会经过 `run_pre_push_review` → `publish_changes` → `_run_supervisor_with_repair_loop` 流程
- **Agent 选择**: fallback 值调整不影响有 label 信号的 Issue 选择逻辑

---

## 4. Implementation Summary

### 4.1 问题一修复

```python
# _finish_existing_commit_publication 新增逻辑：
# 1. 调用 run_pre_push_review（新增）
final_sha, _final_verification_results = run_pre_push_review(...)

# 2. 发布后进入 supervising 而非直接 review（修改）
github_client.edit_issue_labels(
    issue.number,
    add=[config.labels.supervising],
    remove=_workflow_state_labels(config),
)

# 3. 执行监督循环（新增）
if supervisor_config.enabled:
    _run_supervisor_with_repair_loop(...)
else:
    github_client.edit_issue_labels(
        issue.number,
        add=[config.labels.review],
        remove=[config.labels.supervising],
    )
```

### 4.2 问题二修复

```python
# run_agent_once.py
return (
    config.runner.default_agent
    if config.runner.default_agent != "auto"
    else "claude"  # 从 "codex" 改为 "claude"
)
```

---

## 5. Acceptance Checklist

- [x] `_finish_existing_commit_publication` 调用 `run_pre_push_review`
- [x] `_finish_existing_commit_publication` 进入 `supervising` 标签状态
- [x] `_finish_existing_commit_publication` 执行监督循环（如果启用）
- [x] `choose_agent` 在 `auto` + 无信号时返回 `claude`
- [x] 更新相关测试
- [x] 所有 328 个测试通过

## 6. 真实验证记录

**验证日期**: 2026-05-27

### 验证 1: 恢复路径执行 Code Review
```
✅ run_pre_push_review 被调用
```
通过 mock 测试验证 `_finish_existing_commit_publication` 确实调用了 `run_pre_push_review` 函数。

### 验证 2: 恢复路径走监督循环
```
✅ _run_supervisor_with_repair_loop 被调用
✅ supervising 标签被添加
```
通过 mock 测试验证监督循环被正确调用，supervising 标签被添加到 Issue。

### 验证 3: auto 默认选择 claude
```
✅ auto 默认选择 claude，实际返回: claude
```
通过单元测试验证 `choose_agent(issue, config, "auto")` 返回 `claude`。

### 附加说明
- 原文件 `agent_runner_orchestrate.py` (1348 行) 已拆分为三个文件：
  - `agent_runner_orchestrate.py` (660 行) - Issue 发现、路由、入口函数
  - `agent_runner_publication.py` (549 行) - 发布流程、评论构建、本地 commit 复用
  - `agent_runner_supervisor.py` (243 行) - PR 事后监督循环
