# PRD: Agent Runner 单体模块拆分 (`run_agent_once.py` 职责分解)

---

## 1. 引言与目标 (Introduction & Goals)

### 问题说明

`src/backend/core/use_cases/run_agent_once.py` 当前超过 1000 行（1065 行），混合了以下多个职责：

- **Commit Proxy**：读取 agent 提交的 `commit-request.json`、验证分支安全、执行 `git commit`
- **Failure Classification & Formatting**：对每次 agent 尝试的失败进行分类（`classify_failure`），并格式化 recovery prompt 和 GitHub comment
- **Git Utilities & Verification**：获取 HEAD SHA、当前分支、变更文件列表、执行 `lint`/`test` 验证命令
- **Publishing & Safety Validation**：检查 forbidden paths、验证 remote 存在性、push 分支、创建 Draft PR
- **核心编排状态机**：`run_agent_until_committed` 的 recovery 重试循环

根据仓库规范，单代码文件非空行不应超过 1000 行，`just lint` 会对此发出警告。此外，职责混杂导致：
- 单测难以按需加载最小依赖；
- 新开发者难以快速定位代码；
- 不同职责的修改容易互相干扰。

### 目标

按**单一职责原则**将 `run_agent_once.py` 拆分为 4 个独立模块，主模块仅保留核心编排逻辑（`run_agent_until_committed` 状态机与 `run_once` 入口）。

---

## 2. 需求形态 (Requirement Shape)

- **执行者 (Actor)**: 开发者 / 维护者重构代码。
- **触发条件 (Trigger)**: `run_agent_once.py` 超过 1000 行，lint 报警；或需要修改某一职责（如 commit proxy）时难以隔离变更。
- **预期行为 (Expected Behavior)**: 拆分后各模块职责清晰，主模块行数降至 1000 行以内，行为完全无损，现有测试全部通过。
- **范围边界 (Scope Boundary)**: 以代码移动与提取为主，同时修复一个 commit 阶段错误处理的边界漏洞（见第 3.4 节）。

---

## 3. 模块拆分方案 (Module Decomposition)

### 3.1 新模块职责

| 模块 | 职责 | 原位置函数/类 |
|---|---|---|
| `agent_runner_commit.py` | Commit Proxy | `default_commit_message`, `sanitize_commit_message`, `read_commit_request`, `remove_commit_request`, `commit_requested_changes`, `unstage_changes` |
| `agent_runner_failure.py` | Failure Classification & Formatting | `AgentRunnerAttemptError`, `MaxRetriesExceededError`, `UnrecoverableError`, `is_recoverable_commit_request_error`, `classify_failure`, `format_attempt_history`, `format_failure_comment`, `format_recovery_failure_summary`, `format_agent_execution_failure`, `_agent_command_name` |
| `agent_runner_git.py` | Git Utilities & Verification | `get_head_sha`, `get_current_branch`, `run_verification`, `has_changes`, `list_changed_paths`, `list_git_remotes` |
| `agent_runner_publish.py` | Publishing & Safety Validation | `validate_safe_changes`, `validate_publish_remote`, `run_preflight_checks`, `publish_changes` |

### 3.2 主模块保留职责

`run_agent_once.py` 保留：
- `format_command`
- `run_agent`, `run_agent_with_prompt`
- `extract_agent_response_text`, `_append_claude_assistant_text`
- `wait_before_recovery_attempt`
- `run_agent_until_committed`（核心 recovery 状态机）
- `run_once`（单次轮询入口）

### 3.3 依赖方向

```
run_agent_once.py
├── agent_runner_commit.py
│   ├── agent_runner_git.py
│   └── agent_runner_publish.py
├── agent_runner_failure.py
├── agent_runner_git.py
└── agent_runner_publish.py
    └── agent_runner_git.py
```

- 禁止循环依赖。
- `agent_runner_failure.py` 只依赖 `agent_runner_feedback.py`（已有模块）和共享模型。

### 3.4 Commit 阶段错误恢复行为改进

除模块拆分外，本次变更还修复了 commit 阶段的一个错误处理漏洞：

**变更前**：`run_agent_until_committed` 在调用 `commit_requested_changes` 时只捕获 `RuntimeError`。当 `git commit` 因 pre-commit hook 失败、或 git 返回非零退出码（`subprocess.CalledProcessError`）时，异常直接上抛，runner 立即标记为失败并终止，agent 没有机会修复问题。

**变更后**：
1. `run_agent_once.py` 的 commit 阶段捕获扩展为 `except (RuntimeError, subprocess.CalledProcessError) as exc`。
2. `agent_runner_failure.py` 的 `is_recoverable_commit_request_error` 将 `subprocess.CalledProcessError` 也判定为可恢复错误。

这样，pre-commit hook 失败、git commit 被拒绝等 subprocess 错误会进入 recovery 重试循环，agent 收到包含错误输出的 recovery prompt 后，有机会修改代码并重新提交，而不是直接失败。

**不可恢复的错误仍然直接失败**：分支漂移（`Refusing to commit on unexpected branch`）、forbidden paths（`Refusing to publish forbidden paths`）等安全类 `RuntimeError` 不受此变更影响，仍被 `classify_failure` 标记为 `UNRECOVERABLE`。

---

## 4. 兼容性保证 (Compatibility)

- `run_agent_once.py` 的 `__all__` 保留所有对外暴露的符号，通过从新模块 re-export 实现。
- 现有测试导入路径无需修改。
- `process_runner` 接口不变。

---

## 5. 风险评估 (Risks)

| 风险 | 严重程度 | 缓解措施 |
|---|---|---|
| 移动代码时遗漏导入或破坏 `__all__` | 中 | 拆分后运行 `just lint` 和 `just test` |
| 循环依赖 | 低 | 严格按依赖图执行，failure 模块不反向依赖 commit/git/publish |
| 行为漂移 | 低 | 除 3.4 节的 `CalledProcessError` 恢复外，其余逻辑纯提取；该变更已有测试覆盖（`test_scenario_b_precommit_lint_failure_recovery`） |

---

## 6. Acceptance Checklist

- [x] `just lint` 通过，`run_agent_once.py` 行数低于 1000。
- [x] `just test` 全部通过，无回归（328 passed）。
- [x] 各新模块均定义 `__all__`，显式控制公开接口。
- [x] 主模块 `run_agent_once.py` 的 `__all__` 保留原有对外符号，外部导入路径兼容。
- [x] 无循环依赖（已通过 `uv run python -c "from backend.core.use_cases.run_agent_once import *"` 验证）。
- [x] PRD 归档至 `tasks/archive/`。
