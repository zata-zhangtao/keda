# iAR registry list / takeover daemon spawn cwd 修复

## 1. Introduction & Goals

### Problem Statement

在 `iar registry start` / `iar takeover` 拉起托管 daemon 后，以及日常观察 `iar registry list` 时，出现两个相互关联的 bug：

1. **`iar registry list` 显示过期 running 状态**：`_run_registry_list_command` 在构建“running”视图时，没有过滤 `processes.json` 中的 `exited` / `stopped` / `killed` 记录。只要某个 repo_id 历史上启动过 daemon，即使进程早已退出，`registry list` 仍会把这些记录显示成 `running (process-id)`，误导用户认为 daemon 仍在跑。

2. **`iar takeover` / `iar registry start` 启动的 daemon 找不到 registry 配置**：`_run_registry_start_command`、`_restart_daemons` 以及 `takeover` 的 `_start_daemons_for_repo` 都把 `spawn_cwd` 设为**目标仓库本身的路径**（如 `/Users/zata/.iar/repos/zata-zhangtao/keda`）。daemon 子进程启动后按 cwd 向上查找 `config.toml`，找到的是 clone 下来的那份 `config.toml`，而不是用户当前用来注册仓库的那份 `config.toml`。结果子进程报 `Repository 'zata-zhangtao-keda' not found in config.` 并立即退出，但 `registry list` 又把它显示成 running，形成“假 running”。

### Proposed Solution Summary

1. **修复 `iar registry list` 状态过滤**：在 `src/backend/api/cli_registry.py:_run_registry_list_command` 遍历 `supervisor.list_processes()` 时，跳过 `record.status != "running"` 的记录，只有真正存活的进程才显示为 `running`。

2. **统一 daemon 子进程的 spawn cwd**：把 `_run_registry_start_command`、`_restart_daemons` 和 `cli_takeover.py:_start_daemons_for_repo` 中的 `spawn_cwd` 统一改为 `resolve_config_toml_path().parent`，即当前生效的 `config.toml` 所在目录。这样 daemon 子进程加载的 registry 与父 CLI 刚刚写入/读取的 registry 完全一致。

3. **提取 `_start_daemons_for_repo` 为模块级函数**：把原本内嵌在 `_run_takeover_command` 里的闭包提升为 `cli_takeover.py` 的模块级函数 `_start_daemons_for_repo(repo_id, _repo_path)`，提高可测试性，并保持 `execute_takeover` 的回调签名不变。

4. **测试覆盖**：新增 `tests/test_cli_registry.py` 覆盖 registry list 状态过滤、registry start 的 spawn_cwd、takeover daemon 启动的 spawn_cwd；更新 `tests/test_agent_runner_cli.py` 中 `test_main_registry_start_single_repo` 与 `test_main_registry_reinit_start_daemons_*` 以断言 config 目录而非仓库路径。

**Why this is the smallest viable change**：问题根源只在两个 CLI 入口对 `supervisor.list_processes()` 和 `start_runner_process(..., spawn_cwd=...)` 的使用方式。`PidfileProcessSupervisor` 本身已经正确维护 `status` 字段并能探活；`start_runner_process` 本身已经支持任意 `spawn_cwd`。不需要新增抽象、不需要改 supervisor、不需要改配置 schema。

### Measurable Objectives

- **OBJ-1**：`iar registry stop --repo-id zata-zhangtao-keda` 后，即使 `~/.iar/processes.json` 中仍保留该 repo 的 `exited` 记录，`iar registry list` 中 `zata-zhangtao-keda` 的 daemon / review-daemon 列也显示 `stopped`，不再出现 `running (xxx)`。
- **OBJ-2**：`iar takeover zata-zhangtao/freshai` 完成后，被启动的 daemon 子进程不会报 `Repository 'zata-zhangtao-freshai' not found in config.`，而是能正常进入轮询循环。
- **OBJ-3**：`iar registry start --repo-id zata-zhangtao-keda` 时，`start_runner_process` 收到的 `spawn_cwd` 等于当前生效 `config.toml` 的目录，而非 `/Users/zata/.iar/repos/zata-zhangtao/keda`。
- **OBJ-4**：`iar registry reinit --repo-id X --start-daemons` 启动 daemon 时，同样使用 config 目录作为 `spawn_cwd`。
- **OBJ-5**：`just test local` 与 `just lint` 全部通过。

### Realistic Validation

本修复涉及 CLI 行为与真实子进程启动路径，需要在真实项目入口点验证。

- [x] **Registry list 过滤真实验证**：先 `iar registry start --repo-id keda-main`，再 `iar registry stop --repo-id keda-main`，确认 `iar registry list` 两列都显示 `stopped`，没有残留 `running (xxx)`（覆盖 OBJ-1）。
- [x] **Takeover daemon 启动真实验证**：在一个新的 GitHub 仓库上执行 `iar takeover owner/repo`，确认 daemon 日志不再出现 `Repository '...' not found in config.`，且 `iar registry list` 显示 running 时进程确实存活（覆盖 OBJ-2）。
- [x] **Registry start spawn cwd 真实验证**：在 `/Users/zata/code/keda` 执行 `iar registry start --repo-id keda-main`，检查 `~/.iar/processes.json` 中该 daemon 的 `command` 对应的子进程 cwd 为 `/Users/zata/code/keda`（即 config 目录），而非 repo 路径本身（覆盖 OBJ-3）。
- [x] **Reinit --start-daemons spawn cwd 真实验证**：执行 `iar registry reinit --repo-id keda-main --start-daemons`，确认启动的 daemon 子进程 cwd 同样落在 config 目录（覆盖 OBJ-4）。

**为什么单元测试不够**：状态过滤行为虽然可用 mock 覆盖，但 `registry list` 真正暴露问题的场景是 supervisor 已经刷新记录为 `exited` 后列表仍显示 `running`；spawn cwd 错误只有在真实子进程启动、按 cwd 解析 `config.toml` 时才会暴露。mock 会绕过 `iar daemon` 的真实配置加载路径。

### Delivery Dependencies

- Group: none
- Depends on groups:
  - none
- Depends on tasks/issues:
  - none
- Gate type: none
- Notes: 本修复是对已归档 PRD `P1-FEAT-20260623-012835-iar-registry-start-stop-daemon` 与 `P1-FEAT-20260623-002646-iar-daemon-cwd-infer-single-repo` 的修正，不阻塞其它 pending PRD。

## 2. Requirement Shape

- **actor**：使用 `iar registry start` / `iar registry stop` / `iar registry list` / `iar takeover` 管理托管 daemon 的开发者。
- **trigger**：
  - 执行 `iar registry list` 查看 daemon 状态。
  - 执行 `iar registry start --repo-id X` / `--all` 启动 daemon。
  - 执行 `iar registry reinit --repo-id X --start-daemons` 重新初始化并启动 daemon。
  - 执行 `iar takeover owner/repo` 接管仓库并自动启动 daemon。
- **expected behavior**：
  - `registry list` 只把 `status == "running"` 的进程显示为 `running`；`exited` / `stopped` / `killed` 显示为 `stopped`。
  - `registry start`、`reinit --start-daemons`、`takeover` 启动 daemon 时，`spawn_cwd` 使用当前生效 `config.toml` 所在目录，使子进程能解析到与父 CLI 一致的 registry。
  - 现有 `--no-review-daemon`、逐仓错误汇总、双开检测等行为保持不变。
- **scope boundary**：只修改 `src/backend/api/cli_registry.py` 与 `src/backend/api/cli_takeover.py` 中上述两个使用点；不改 `PidfileProcessSupervisor`、不改配置 schema、不改 daemon 命令行参数。

## 3. Repository Context And Architecture Fit

### Current Relevant Modules

- `src/backend/api/cli_registry.py`：
  - `_run_registry_list_command`：构造 running 视图的地方，需要过滤 status。
  - `_run_registry_start_command`：把 `spawn_cwd` 从 `repo_path` 改为 config 目录。
  - `_restart_daemons`：`reinit --start-daemons` 的 daemon 重启入口，同样需要改 spawn_cwd。
- `src/backend/api/cli_takeover.py`：
  - `_start_daemons_for_repo`：takeover 的 daemon 启动回调，需要改 spawn_cwd 并提升为模块级函数。
- `src/backend/engines/agent_runner/factory.py`：新增被调用的 `resolve_config_toml_path()`（已有函数）。
- `src/backend/infrastructure/config/settings.py`：`resolve_config_toml_path()` 的实现位置，按 `IAR_CONFIG` → cwd 向上查找 → `~/.iar/config.toml` → 源码根的顺序解析。
- `src/backend/infrastructure/console/process_supervisor.py`：`PidfileProcessSupervisor.list_processes()` 已返回带 `status` 字段的记录，无需修改。
- `tests/test_agent_runner_cli.py`：已有 `test_main_registry_start_single_repo` 与 `test_main_registry_reinit_start_daemons_*` 需要更新断言。
- `tests/test_cli_registry.py`：新增测试文件。

### Existing Architecture Pattern To Follow

- 四层依赖方向不变：`api/cli_registry.py` 调用 `engines.agent_runner.factory.resolve_config_toml_path()` 是允许的（api → engines）。
- `spawn_cwd` 的语义在 `console_processes.start_runner_process` 的 docstring 中已明确为“keda 项目根 / config 目录，保证子进程读到正确配置”；本次修复是让调用方真正遵守该语义。
- 测试风格与现有 `tests/test_agent_runner_cli.py` 一致：mock supervisor / settings / `start_runner_process`，断言调用参数。

### Ownership And Dependency Boundaries

- **修改代码**：`src/backend/api/cli_registry.py`、`src/backend/api/cli_takeover.py`。
- **复用代码**：`resolve_config_toml_path()`、`PidfileProcessSupervisor`、`start_runner_process`。
- **不修改**：supervisor 实现、settings schema、daemon 核心循环。

### Constraints From Runtime, Docs, Tests, Or Workflows

- 单文件 1000 行警告：`cli_registry.py` 当前长度可控；提取闭包到模块级不会显著增加文件长度。
- `just test` / `just lint` 必须通过。
- 文件 I/O 保持显式 `encoding="utf-8"`（本修复未新增文件 I/O）。

### Matching Or Related PRDs

- **`tasks/archive/P1-FEAT-20260623-012835-iar-registry-start-stop-daemon.md`**：定义了 `iar registry start/stop` 命令，其中曾建议 `spawn_cwd = repo_path`。本修复纠正该决策，改为 config 目录。
- **`tasks/archive/P1-FEAT-20260623-002646-iar-daemon-cwd-infer-single-repo.md`**：处理了前台 `iar daemon` 的 cwd 推断。本修复处理的是托管 daemon 子进程的 spawn cwd，与其正交但目标一致：让 daemon 能加载正确配置。
- **`tasks/pending/`**：无直接依赖或冲突的 pending PRD。
- **关系判定**：本任务是独立 bug 修复，不阻塞其它 pending PRD。

## 4. Recommendation

### Recommended Approach

1. 在 `src/backend/api/cli_registry.py:_run_registry_list_command` 中增加状态过滤：
   ```python
   for record in supervisor.list_processes():
       if record.status != "running":
           continue
       ...
   ```
2. 在 `src/backend/api/cli_registry.py` 中：
   - 导入 `resolve_config_toml_path`（替代或补充 `resolve_console_spawn_cwd`）。
   - `_run_registry_start_command` 在循环外计算 `spawn_cwd = resolve_config_toml_path().parent`，并传给 `start_runner_process`。
   - `_restart_daemons` 同样改为 `spawn_cwd = resolve_config_toml_path().parent`。
3. 在 `src/backend/api/cli_takeover.py` 中：
   - 导入 `resolve_config_toml_path`。
   - 将 `_start_daemons_for_repo` 提升为模块级函数，使用 `spawn_cwd = resolve_config_toml_path().parent`。
4. 更新相关测试，断言 `spawn_cwd == config_path.parent` 且 `!= repo_path`。

### Why This Is The Best Fit For The Current Architecture

- 复用已有 `resolve_config_toml_path()`，不新增配置解析逻辑。
- 保持 `PidfileProcessSupervisor` 和 `start_runner_process` 不变，风险最小。
- 修正后的 `spawn_cwd` 语义与 `console_processes.py` docstring 一致。
- registry list 过滤只加一行 `continue`，不引入新抽象。

### Rationale For Rejecting Redundant Abstractions

- 不新增 `ProcessStatusFilter` 类：一行 `if record.status != "running": continue` 已足够。
- 不新增 `DaemonSpawnContext` 对象：`resolve_config_toml_path().parent` 直接表达意图，无需包装。
- 不改 supervisor 接口：supervisor 已经返回 `status`，问题在消费方。

### Alternatives Considered

- **Alternative A：在 `PidfileProcessSupervisor.list_processes()` 中过滤掉非 running 记录后再返回**。
  - 拒绝理由：supervisor 作为底层端口，保留完整历史记录更有用（日志、审计、失败排查）；过滤是 CLI 展示层的职责。
- **Alternative B：通过 `IAR_CONFIG` 环境变量传给 daemon 子进程**。
  - 拒绝理由：需要修改 `PidfileProcessSupervisor.spawn()` 接口；当前修改 spawn_cwd 已能达到同样效果，且侵入更小。
- **Alternative C：把 registry 条目同步写入 clone 下来的仓库 config.toml**。
  - 拒绝理由：会造成多份 config 不一致，且 takeover 只是临时副本；registry 的真实来源应是操作员当前使用的 config。

## 5. Implementation Guide

> This section is a living implementation guide based on current repository analysis. If implementation discovers additional affected files, hidden dependencies, edge cases, or a better path, update this PRD before proceeding.

### Core Logic

**Registry list 过滤流程**

```text
user runs "iar registry list"
  ↓
cli_registry.py:_run_registry_list_command
  ↓
supervisor.list_processes() 返回全部记录（含 exited/stopped/running）
  ↓
for record in records:
  if record.status != "running":
    continue          # 新增过滤
  running[record.repo_id][record.kind].append(record.process_id)
  ↓
_format_process_status(...)
  ↓
registry list table 只显示真正 running 的进程
```

**Daemon spawn cwd 修正流程**

```text
user runs "iar registry start --repo-id keda-main"
  ↓
_run_registry_start_command
  ↓
settings = load_fresh_agent_runner_settings()
contexts, _ = resolve_repository_targets_with_diagnostics(settings)
supervisor = create_process_supervisor()
runner_command = settings.console.runner_command
spawn_cwd = resolve_config_toml_path().parent   # 修正点
  ↓
for repo_id in selected_ids:
  for kind in (DAEMON, REVIEW_DAEMON):
    start_runner_process(..., spawn_cwd=spawn_cwd)
```

Takeover 与 reinit 路径同理，只是入口不同。

### Change Impact Tree

```text
.
├── src/backend/api/
│   ├── cli_registry.py
│   │   [修改]
│   │   【registry list 过滤非 running 记录；registry start / reinit daemon 使用 config 目录作为 spawn_cwd】
│   │
│   │   ├── _run_registry_list_command: 增加 if record.status != "running": continue
│   │   ├── _run_registry_start_command: spawn_cwd 改为 resolve_config_toml_path().parent
│   │   └── _restart_daemons: spawn_cwd 改为 resolve_config_toml_path().parent
│   │
│   └── cli_takeover.py
│       [修改]
│       【takeover 的 daemon 启动使用 config 目录作为 spawn_cwd；回调提取为模块级函数】
│
│       ├── 新增模块级 _start_daemons_for_repo(repo_id, _repo_path)
│       └── _run_takeover_command 中移除内嵌闭包，直接引用模块级函数
│
├── src/backend/engines/agent_runner/
│   └── factory.py
│       [无需修改]
│       【复用已有 resolve_config_toml_path()】
│
├── tests/
│   ├── test_cli_registry.py
│   │   [新增]
│   │   【覆盖 registry list 过滤、registry start spawn_cwd、takeover spawn_cwd】
│   │
│   │   ├── test_registry_list_skips_non_running_records
│   │   ├── test_registry_list_includes_running_records
│   │   ├── test_registry_start_uses_config_directory_as_spawn_cwd
│   │   ├── test_registry_start_rejects_missing_repo_path
│   │   └── test_takeover_start_daemons_uses_config_directory_as_spawn_cwd
│   │
│   └── test_agent_runner_cli.py
│       [修改]
│       【更新现有测试以断言 config 目录 spawn_cwd】
│
│       ├── test_main_registry_start_single_repo: spawn_cwd == config_path.parent
│       └── test_main_registry_reinit_start_daemons_uses_config_directory_cwd: 重命名并更新断言
│
└── docs/
    └── guides/agent-runner.md
        [可选后续]
        【若文档中仍描述 spawn cwd 为 repo 路径，需同步修正】
```

### Executor Drift Guard

执行或 review 前用以下命令复查边界：

```bash
# 确认 registry list 中 status 过滤位置
rg -n "record.status" src/backend/api/cli_registry.py

# 确认所有 daemon spawn 入口是否都改为 resolve_config_toml_path().parent
rg -n "spawn_cwd\s*=" src/backend/api/cli_registry.py src/backend/api/cli_takeover.py

# 确认 resolve_config_toml_path 引用点
rg -n "resolve_config_toml_path" src/backend/api/cli_registry.py src/backend/api/cli_takeover.py

# 确认测试覆盖
rg -n "spawn_cwd|record.status" tests/test_cli_registry.py tests/test_agent_runner_cli.py
```

**潜在隐藏引用**：
- `agent_runner_console.py` 等 API 路由也调用 `start_runner_process(..., spawn_cwd=resolve_console_spawn_cwd())`，但那是 FastAPI 托管进程入口，与 CLI registry/takeover 是不同场景，本次未改动。
- 如果未来其它地方也以“仓库路径”作为 `spawn_cwd` 调用 `start_runner_process`，需要同样审查。

**验证失败排错要点**：
- `registry list` 仍显示已退出进程为 running：检查 `_run_registry_list_command` 是否确实加了 `if record.status != "running": continue`。
- takeover 后 daemon 仍报 `Repository not found in config`：检查 `start_runner_process` 的 `spawn_cwd` 是否为 config 目录，以及该目录下的 `config.toml` 是否包含对应 `repo_id`。
- 测试 `test_takeover_start_daemons_uses_config_directory_as_spawn_cwd` 失败：检查 `cli_takeover.py` 的 `_start_daemons_for_repo` 是否已被提取为模块级函数且 patch 路径正确。

### Flow Diagram

```mermaid
flowchart TD
    A[iar registry list] --> B[supervisor.list_processes]
    B --> C{record.status == "running"?}
    C -- yes --> D[add to running view]
    C -- no --> E[skip]
    D --> F[render table]
    E --> F

    G[iar registry start / reinit --start-daemons / takeover] --> H[resolve_config_toml_path]
    H --> I[spawn_cwd = config.toml parent directory]
    I --> J[start_runner_process]
    J --> K[iar daemon --repo-id X]
    K --> L[子进程从 config 目录加载 registry]
```

### Realistic Validation Plan

| Behavior | Real Entry Point | Test Layer | Mock Boundary | Data/Env Needed | Command Or Procedure | Required For Acceptance |
|---|---|---|---|---|---|---|
| Registry list 不显示已退出进程 | `iar registry list` | integration (real CLI) | 不 mock supervisor；真实 stop 后观察 | `keda-main` daemon 已 start 再 stop | `iar registry start --repo-id keda-main && iar registry stop --repo-id keda-main && iar registry list`；验证无 `running (xxx)` | Yes |
| Takeover daemon 能找到配置 | `iar takeover owner/repo` | integration (real CLI) | 真实 gh clone / init | 选择一个未接管的 GitHub 仓库 | `iar takeover owner/repo`；检查 daemon 日志无 `not found in config`；`ps` 与 `registry list` 一致 | Yes |
| Registry start spawn cwd 正确 | `iar registry start --repo-id keda-main` | integration (real CLI) | 不 mock | `keda-main` enabled 且已 init | 启动后检查 `~/.iar/processes.json` 中 daemon 子进程 cwd 为 config 目录 | Yes |
| Reinit --start-daemons spawn cwd 正确 | `iar registry reinit --repo-id keda-main --start-daemons` | integration (real CLI) | 不 mock | `keda-main` 已 init | 启动后检查 daemon 子进程 cwd 为 config 目录 | Yes |
| 状态过滤单测 | `pytest tests/test_cli_registry.py -v` | unit | mock supervisor | 不需要 | `uv run pytest tests/test_cli_registry.py -v` | Yes |
| spawn cwd 单测 | `pytest tests/test_cli_registry.py tests/test_agent_runner_cli.py -v` | unit | mock start_runner_process | 不需要 | `uv run pytest tests/test_cli_registry.py tests/test_agent_runner_cli.py -v` | Yes |

**Failure triage**：
- `registry list` 过滤未生效：检查 `supervisor.list_processes()` 返回的记录是否包含 `status` 字段；若 supervisor 返回字符串而非 dataclass，需确认比较方式。
- takeover 后 daemon 仍找不到 config：确认 `resolve_config_toml_path()` 在父 CLI 运行时返回的是你期望的那份 config；确认 `spawn_cwd` 传给 `start_runner_process` 的是 `.parent`。

### Low-Fidelity Prototype

不需要。本修复为 CLI 行为与后台子进程启动路径，无 UI 组件。

### ER Diagram

无数据模型变化；不修改 pydantic schema、不新增表/实体。

### Interactive Prototype Change Log

无交互式原型文件变化。

### External Validation

| Topic | Source | Checked On | Relevant Finding | Impact On Recommendation |
|---|---|---|---|---|
| — | — | — | 无外部事实需要查询；所有依赖 API 与配置解析逻辑已内置于仓库代码 | — |

## 6. Definition Of Done

- `iar registry list` 对已退出/已停止进程显示 `stopped`，对 running 进程显示 `running (process-id)`。
- `iar registry start`、`iar registry reinit --start-daemons`、`iar takeover` 启动的 daemon 子进程 cwd 为当前生效 `config.toml` 目录。
- 新增 `tests/test_cli_registry.py` 并通过。
- 更新 `tests/test_agent_runner_cli.py` 相关测试并通过。
- `just test local` 全量通过。
- `just lint` 通过。
- PRD acceptance checklist 全部勾选后归档。

## 7. Acceptance Checklist

### Architecture Acceptance

- [x] 修改仅位于 `src/backend/api/cli_registry.py` 与 `src/backend/api/cli_takeover.py`，未跨层 import infrastructure。
- [x] 复用已有 `resolve_config_toml_path()`，未新增配置解析逻辑。
- [x] 未修改 `PidfileProcessSupervisor` 内部实现。
- [x] 未修改 pydantic settings 模型、`.iar.toml` schema、config.toml 字段。

### Behavior Acceptance

- [x] `iar registry list` 对 `exited` / `stopped` / `killed` 记录显示 `stopped`（OBJ-1）。
- [x] `iar registry list` 对 `running` 记录显示 `running (process-id)`。
- [x] `iar takeover owner/repo` 启动的 daemon 不报 `Repository not found in config.`（OBJ-2）。
- [x] `iar registry start --repo-id X` 使用 config 目录作为 `spawn_cwd`（OBJ-3）。
- [x] `iar registry reinit --repo-id X --start-daemons` 使用 config 目录作为 `spawn_cwd`（OBJ-4）。
- [x] 现有双开检测、逐仓错误汇总、`--no-review-daemon` 行为保持不变。

### Parser / CLI Entry Acceptance

- [x] `cli_parser.py` 与 `cli_typer.py` 无需修改（registry / takeover parser 已存在）。
- [x] `cli.py` 无需新增 dispatch 分支（registry / takeover 分支已存在）。

### Documentation Acceptance

- [x] 若 `docs/guides/agent-runner.md` 中描述了 spawn cwd 为 repo 路径，已同步修正为 config 目录。

### Validation Acceptance

- [x] `just test local` 在仓库根目录执行后全部通过。
- [x] `just lint` 在仓库根目录执行后全部通过。
- [x] Realistic Validation Plan 第 1 行（registry list 过滤）手动验证通过。
- [x] Realistic Validation Plan 第 2 行（takeover daemon 配置加载）手动验证通过。
- [x] Realistic Validation Plan 第 3 行（registry start spawn cwd）手动验证通过。
- [x] Realistic Validation Plan 第 4 行（reinit spawn cwd）手动验证通过。
- [x] 新增单元测试 `tests/test_cli_registry.py` 全部通过。

## 8. Functional Requirements

- **FR-1**：`iar registry list` 必须只把 `status == "running"` 的进程记录显示为 `running`；非 running 记录必须显示为 `stopped`。
- **FR-2**：`iar registry start --repo-id <repo_id>` 调用 `start_runner_process` 时，`spawn_cwd` 必须等于当前生效 `config.toml` 的父目录。
- **FR-3**：`iar registry start --all` 必须为每个目标仓库使用相同的 config 目录 `spawn_cwd`。
- **FR-4**：`iar registry reinit --repo-id <repo_id> --start-daemons` 启动 daemon 时，`spawn_cwd` 必须等于当前生效 `config.toml` 的父目录。
- **FR-5**：`iar takeover owner/repo` 在启动被接管仓库的 daemon 时，`spawn_cwd` 必须等于当前生效 `config.toml` 的父目录。
- **FR-6**：`_start_daemons_for_repo` 必须保持 `execute_takeover` 所需的回调签名 `(repo_id: str, _repo_path: Path) -> None`。
- **FR-7**：所有修改不得破坏现有 `--no-review-daemon`、双开检测、disabled 仓拒绝、不存在 repo_id 拒绝等行为。

## 9. Non-Goals

- 不修改 `iar daemon` / `iar review-daemon` 前台命令的 cwd 推断逻辑（已由另一 PRD 处理）。
- 不修改 `PidfileProcessSupervisor` 的状态机或探活逻辑。
- 不新增 config.toml / `.iar.toml` 字段。
- 不改变 `takeover` 的 clone / init / register 流程。
- 不提供自动清理 `processes.json` 中 exited 记录的功能。

## 10. Risks And Follow-Ups

- **R-1（config 目录推断歧义）**：如果用户通过 `IAR_CONFIG` 显式指定了一份远离当前工作目录的 config，`resolve_config_toml_path().parent` 会变成那份 config 的目录，daemon 子进程可能不在用户预期的 cwd。缓解：这是预期行为——子进程本就应该加载 `IAR_CONFIG` 指向的那份 config。
- **R-2（多 config 场景）**：如果用户在 keda 源码目录运行 takeover，但期望 daemon 使用全局 `~/.iar/config.toml`，当前实现会优先使用 cwd 找到的 keda `config.toml`。缓解：这是 registry 编辑的默认行为（`create_registry_editor` 使用 `resolve_config_toml_path()`），与父 CLI 保持一致。
- **Follow-up F-1**：未来可考虑在 `PidfileProcessSupervisor` 中定期清理长期 exited 记录，减少 `processes.json` 膨胀，但这不是本修复的必要部分。

## 11. Decision Log

| ID | Decision | Chosen | Rejected | Rationale |
|---|---|---|---|---|
| D-01 | Registry list 过滤位置 | CLI 层 `_run_registry_list_command` 中过滤 | 在 `PidfileProcessSupervisor.list_processes()` 中过滤 | Supervisor 保留完整历史记录更有用；展示过滤是 CLI 职责 |
| D-02 | Daemon spawn cwd | `resolve_config_toml_path().parent` | 继续用 `repo_path`；用 `IAR_CONFIG` 环境变量 | `repo_path` 导致子进程加载不到 registry；`IAR_CONFIG` 需要改 supervisor 接口，当前方案侵入更小 |
| D-03 | `_start_daemons_for_repo` 组织方式 | 提升为 `cli_takeover.py` 模块级函数 | 保持为 `_run_takeover_command` 内嵌闭包 | 模块级函数可独立测试，保持回调签名不变 |
| D-04 | 是否同步修改 API 路由的 spawn cwd | 否 | 是 | API 路由使用 `resolve_console_spawn_cwd()` 返回 keda 源码根，与 CLI 场景不同；本次只修 CLI bug |
| D-05 | 是否引入新抽象封装 spawn_cwd 计算 | 否 | 新增 helper / dataclass | `resolve_config_toml_path().parent` 已足够表达意图，避免过度设计 |
