# PRD: Agent Runner 前台实时输出

## 1. Introduction & Goals

当前 `iar run-once` 运行 AI Agent（Codex / Claude）时，子进程输出被 `SubprocessRunner.run()` 的 `capture_output=True` 完全捕获到内存变量中，终端上看不到任何实时进度。用户无法判断 Agent 是在思考、在编辑文件、还是已经卡死。

本 PRD 目标：让 `run_agent()` 调用 Agent 时**直接继承当前终端的 stdout/stderr**，实现与 `just implement` 一致的前台实时输出体验。

### 可衡量目标

- `iar run-once` 执行期间，Codex / Claude 的实时输出直接显示在终端上
- 不改变其他命令（如 `git status`、`just test` 等验证命令）的现有捕获行为
- `just lint` 和 `pytest` 继续通过
- 向后兼容：默认行为不变，仅 `run_agent()` 显式关闭捕获

---

## 2. Requirement Shape

| 维度 | 内容 |
|------|------|
| **Actor** | 开发者（运行 `iar run-once`） |
| **Trigger** | `run_once` 进入 `run_agent()` 阶段 |
| **Expected behavior** | Codex / Claude 的 stdout/stderr 实时流式输出到终端 |
| **Explicit scope boundary** | 仅修改 `IProcessRunner` 接口及其实现；不改动 Agent 调用逻辑本身 |

---

## 3. Repository Context And Architecture Fit

### 3.1 当前相关模块

```
src/backend/core/shared/interfaces/agent_runner.py   # IProcessRunner 抽象接口
src/backend/infrastructure/process_runner.py         # SubprocessRunner 实现
src/backend/core/use_cases/run_agent_once.py         # run_agent() 调用方
```

### 3.2 架构约束

- `core/use_cases/run_agent_once.py` 属于 `core/` 层，**禁止直接导入 `infrastructure/`**
- `core/` 层通过 `IProcessRunner` 接口与基础设施交互
- 接口变更需要同步更新 `infrastructure/process_runner.py` 和所有测试 mock

---

## 4. Recommendation

### 4.1 Recommended Approach

在 `IProcessRunner.run()` 签名中新增 `capture_output: bool = True` 参数：

- `capture_output=True`（默认）：保持现有行为，`subprocess.run(capture_output=True)`
- `capture_output=False`：设置 `stdout=None, stderr=None`，让子进程直接继承父进程 TTY

`run_agent()` 调用时显式传入 `capture_output=False`。

**为什么这是最佳方案**：
- 改动最小：只改接口签名 + 实现 + 一处调用 + 测试 mock
- 向后兼容：所有现有调用默认行为不变
- 语义清晰：Agent 运行是唯一直接面向用户的长时交互过程，需要前台输出

### 4.2 Alternatives Considered

| 方案 | 说明 | 拒绝原因 |
|------|------|----------|
| 新增 `run_interactive()` 方法 | 在 `IProcessRunner` 上新增独立方法 | 改动面更大，需要改更多测试文件 |
| 全局修改 `SubprocessRunner` 默认不捕获 | 把默认改为 `capture_output=False` | 会破坏 `run_verification`、`has_changes` 等依赖 stdout 解析的逻辑 |
| 在 `run_agent()` 里绕过 `IProcessRunner` 直接 `subprocess.run` | 不经过抽象接口 | 违反依赖注入和架构分层原则 |

---

## 5. Implementation Guide

### 5.1 Core Logic

```python
# infrastructure/process_runner.py
def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
    if capture_output:
        completed = subprocess.run(
            list(command), cwd=cwd, check=False,
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
        stdout = completed.stdout
        stderr = completed.stderr
    else:
        completed = subprocess.run(
            list(command), cwd=cwd, check=False,
            stdout=None, stderr=None, encoding="utf-8", timeout=timeout,
        )
        stdout = ""
        stderr = ""
    result = CommandResult(
        command=tuple(command),
        return_code=completed.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    ...
```

```python
# core/use_cases/run_agent_once.py:116
return process_runner.run(command, cwd=worktree_path, capture_output=False)
```

### 5.2 Affected Files

#### 修改文件

```
src/backend/core/shared/interfaces/agent_runner.py    # IProcessRunner.run() 加 capture_output 参数
src/backend/infrastructure/process_runner.py          # SubprocessRunner.run() 实现
src/backend/core/use_cases/run_agent_once.py          # run_agent() 调用传 capture_output=False
tests/conftest.py                                     # FakeProcessRunner.run() 同步更新签名
tests/test_run_agent.py                               # 如有直接断言 run() 调用参数的测试需同步
```

### 5.3 Change Matrix

```text
src/backend/core/shared/interfaces/agent_runner.py
  [修改]
  【总结】IProcessRunner.run() 新增 capture_output: bool = True 参数

  └── 签名变为：
      def run(self, command, *, cwd, check=True, timeout=None, capture_output: bool = True) -> CommandResult

src/backend/infrastructure/process_runner.py
  [修改]
  【总结】SubprocessRunner.run() 实现 capture_output 分支逻辑

  ├── capture_output=True（默认）：保持 subprocess.run(capture_output=True)
  ├── capture_output=False：stdout=None, stderr=None，让输出直达终端
  └── capture_output=False 时返回空字符串 stdout/stderr

src/backend/core/use_cases/run_agent_once.py
  [修改]
  【总结】run_agent() 调用 process_runner.run() 时传入 capture_output=False

  └── 第 116 行：process_runner.run(command, cwd=worktree_path, capture_output=False)

tests/conftest.py
  [修改]
  【总结】FakeProcessRunner.run() 同步更新签名，支持 capture_output 参数（默认 True）

tests/test_run_agent.py
  [修改]
  【总结】如有 mock 断言验证 run() 调用参数，需同步期望 capture_output=False
```

### 5.4 Flow

```
run_once()
  └── run_agent(codex, issue, worktree_path, process_runner)
        └── process_runner.run(command, cwd=worktree_path, capture_output=False)
              └── subprocess.run(..., stdout=None, stderr=None)
                    └── Codex stdout/stderr → 终端 TTY（实时可见）
```

---

## 6. Definition Of Done

- [x] `iar run-once` 执行期间，Codex / Claude 的实时输出直接显示在终端上
- [x] `iar run-once --dry-run` 行为不变（不调用 run_agent）
- [x] `just lint` 通过
- [x] `pytest` 全部通过
- [x] 向后兼容：不涉及 run_agent 的其他 `process_runner.run()` 调用默认行为不变

---

## 7. Acceptance Checklist

### Behavior Acceptance

- [x] `run_agent()` 调用 `process_runner.run()` 时传入 `capture_output=False`
- [x] `capture_output=True`（默认）时，`SubprocessRunner.run()` 仍捕获 stdout/stderr 到 `CommandResult`
- [x] `capture_output=False` 时，子进程 stdout/stderr 直接输出到终端，返回值中 stdout/stderr 为空字符串
- [x] `FakeProcessRunner` 在测试中支持新的 `capture_output` 参数

### Validation Acceptance

- [x] `pytest` 全部通过
- [x] `just lint` 通过
