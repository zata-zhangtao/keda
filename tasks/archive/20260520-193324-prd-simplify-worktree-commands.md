# PRD: 简化 Agent Runner worktree 命令，消除 git_worktree.sh 依赖

## 1. Introduction & Goals

Agent Runner 当前依赖 `scripts/git_worktree.sh` 来获取 worktree 路径，且 `just worktree` 命令使用 `--issue` 长选项参数风格。这些依赖增加了维护成本和间接层。

本次改动目标是：
- 将 `reuse_command` 和 `path_command` 改为内联 bash 命令，消除对 `scripts/git_worktree.sh` 的依赖
- 统一 `create_command` 的 `just worktree` 参数风格为位置参数 `issue-{issue_number}` 而非 `--issue {issue_number}`

### 可衡量目标

- 所有 Agent Runner 配置项（`config.toml`、`AgentRunnerWorktreeSettings`、`WorktreeConfig`）中的三个 worktree 命令全部更新
- `scripts/git_worktree.sh` 不再被任何代码引用
- 现有功能不受影响：create、reuse、path 三种场景行为一致

---

## 2. Requirement Shape

| 维度 | 内容 |
|------|------|
| **Actor** | Agent Runner 内部逻辑（`run_agent_once.py` 通过配置命令创建/复用/定位 worktree） |
| **Trigger** | `run_agent_once.py` 在 claim issue 后调用 `create_command`、`reuse_command`、`path_command` |
| **Expected behavior** | 三种场景行为与变更前一致：创建隔离 worktree、检测已存在 worktree、获取 worktree 路径 |
| **Explicit scope boundary** | 只修改 worktree 配置命令；不修改 worktree 创建逻辑、不修改 Agent Runner 业务流程、不修改测试 |

---

## 3. Repository Context

### 3.1 当前相关模块

| 文件 | 角色 |
|------|------|
| `config.toml` | Agent Runner `[agent_runner.worktree]` 配置段 |
| `src/backend/core/shared/models/agent_runner.py` | `WorktreeConfig` frozen dataclass，定义命令默认值 |
| `src/backend/infrastructure/config/settings.py` | `AgentRunnerWorktreeSettings` Pydantic 模型 |
| `docs/guides/agent-runner.md` | 使用指南，包含配置示例 |
| `scripts/git_worktree.sh` | 被替代的旧脚本 |

### 3.2 现有值 vs 改动后值

| 配置项 | 当前值 | 改动后值 |
|--------|--------|----------|
| `create_command` | `just worktree --issue {issue_number} enter_shell=false` | `just worktree issue-{issue_number} enter_shell=false` |
| `reuse_command` | `just worktree --issue {issue_number} --existing-branch enter_shell=false` | `bash -c 'test -d "$(dirname "$(git rev-parse --show-toplevel)")/issue-{issue_number}"'` |
| `path_command` | `bash scripts/git_worktree.sh --print-path --issue {issue_number} --existing-branch` | `bash -c 'echo "$(dirname "$(git rev-parse --show-toplevel)")/issue-{issue_number}"'` |

### 3.3 架构约束

无架构约束变化。所有改动位于配置层（三层：config.toml / settings.py / models.py），不涉及依赖方向变更。

---

## 4. Implementation Guide

### 4.1 改动项

四份文件中的 worktree 命令默认值同步更新：

1. `config.toml` — 运行时配置
2. `src/backend/core/shared/models/agent_runner.py` — 领域模型的 frozen dataclass 默认值
3. `src/backend/infrastructure/config/settings.py` — Pydantic `AgentRunnerWorktreeSettings` 默认值
4. `docs/guides/agent-runner.md` — 文档示例

### 4.2 核心思路

将外部 shell 脚本和 `just` 长选项的抽象层去除，直接以内联 bash 命令表达 worktree 所在的约定路径：

```
$(dirname $(git rev-parse --show-toplevel))/issue-{issue_number}
```

这个路径等价于在 Git 仓库同级目录下以 `issue-{issue_number}` 命名的目录，是 `git worktree` 的典型位置。

### 4.3 设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| `reuse_command` 使用 `test -d` 而非调用 `git worktree list` | `test -d` 检测目录存在即可 | 更轻量、无需解析 `git worktree list` 输出；目录存在即表示 worktree 存在 |
| `create_command` 改为位置参数 | `issue-{issue_number}` | 与 `just worktree` 更新的参数风格一致 |
| 统一 `dirname` 公式 | `$(dirname "$(git rev-parse --show-toplevel)")/issue-{issue_number}` | 与 `git worktree add` 的默认行为对齐；不增加额外的配置项 |

---

## 5. Definition Of Done

- [x] `config.toml` 中 `[agent_runner.worktree]` 三个命令已更新
- [x] `src/backend/core/shared/models/agent_runner.py` 中 `WorktreeConfig` 默认值已更新
- [x] `src/backend/infrastructure/config/settings.py` 中 `AgentRunnerWorktreeSettings` 默认值已更新
- [x] `docs/guides/agent-runner.md` 中的配置示例已同步更新
- [x] `just test` 全部通过

---

## 6. Acceptance Checklist

### Behavior Acceptance

- [x] `create_command` 执行后能创建新的 worktree 目录
- [x] `reuse_command` 在目标目录存在时返回 0，不存在时返回非 0
- [x] `path_command` 输出正确的 worktree 绝对路径

### Configuration Acceptance

- [x] `config.toml` 覆盖生效（使用自定义值替换默认命令）
- [x] 使用默认值时，pydantic-settings 加载值与 `WorktreeConfig` dataclass 默认值一致

### Documentation Acceptance

- [x] `docs/guides/agent-runner.md` 配置示例已同步
- [x] 文档中不再引用 `scripts/git_worktree.sh`

### Validation Acceptance

- [x] `just lint` 通过
- [x] `just test` 通过

---

## 7. Risks And Follow-Ups

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| `git rev-parse --show-toplevel` 在非 Git 目录中失败 | `reuse_command` / `path_command` 出错 | 此类调用发生在已 clone 的仓库内，风险极低；若失败，bash 返回非 0，引导用户检查工作目录 |
| `dirname` 公式与 `just worktree` 实际路径不一致 | worktree 路径错位 | 两个公式使用相同的路径约定；`just worktree` 的 add 命令使用 `../../issue-{issue_number}` |
| `scripts/git_worktree.sh` 未被删除 | 残留死脚本造成混淆 | 确认无引用后删除该文件 |
