# 新增 `iar registry start` / `iar registry stop` 命令

## 1. Introduction & Goals

### Problem Statement

目前 `iar` 只有两种方式能把 daemon / review-daemon 以"托管进程"形态登记到 `~/.iar/processes.json`（从而被 `iar registry list` 显示为 running）：

1. `iar takeover`：从 GitHub clone 到 `~/.iar/repos/<owner>/<repo>`，再自动起 daemon
2. `iar registry reinit --repo-id X --start-daemons`：reinit 会强制重置 `.iar.toml`

对于像 `keda-main` 这种**已经在本地开发目录 init 好**、且**只想让 `registry list` 显示 running** 的仓库，用户只有两个选择：

- 每次 reinit（会丢失 `.iar.toml` 里手改的字段）
- 手动用 tmux / nohup 起 daemon（`registry list` 永远显示 stopped）

这明显缺一个轻量命令：`iar registry start --repo-id keda-main`，直接走现有的 `start_runner_process` + `PidfileProcessSupervisor.spawn()`，不重置 `.iar.toml`，并把进程登记进 `processes.json`。

### Proposed Solution Summary

1. **新增 `iar registry start`**：
   - `iar registry start --repo-id <repo_id>`：启动该仓的 daemon + review-daemon
   - `iar registry start --all`：启动所有 enabled 注册仓的 daemon + review-daemon
   - 复用 `backend.core.use_cases.console_processes.start_runner_process` 启动进程
   - 复用 `backend.engines.agent_runner.factory.create_process_supervisor` 写 `~/.iar/processes.json`
   - 复用 `~/.iar/config.toml` 中 `[agent_runner.console].runner_command` 决定启动前缀（如 `uv run iar` / `iar`）
   - `spawn_cwd` 设为注册仓根目录，保证 cwd 推断 PRD 的 fallback 路径也能命中单仓
2. **新增 `iar registry stop`**：
   - `iar registry stop --repo-id <repo_id>`：停止该仓的 daemon + review-daemon
   - `iar registry stop --all`：停止所有 running 的 daemon + review-daemon
   - 复用 `backend.core.use_cases.console_processes.stop_runner_process`
   - 未匹配 running 进程时退出码 0 并提示（幂等）
3. **可选开关**：`--no-review-daemon` 只起 / 只停 daemon，但**默认两边都起**（和 `takeover`、`reinit --start-daemons` 行为一致）。
4. **CLI 入口**：同步更新 `cli_parser.py`（argparse legacy 入口）与 `cli_typer.py`（Typer 入口）。
5. **文档**：更新 `docs/guides/agent-runner.md` 的 registry 小节，说明 `registry start/stop` 的使用场景与 launchd / systemd 开机自启的关系。

**Why this is the best fit**：所有核心能力已经存在——`start_runner_process` / `stop_runner_process` / `PidfileProcessSupervisor` 都是为了托管进程设计的；`reinit --start-daemons` 和 `takeover` 已经在调用它们。新增命令只是把"启动托管 daemon"封装成独立 CLI 入口，**零新增 supervisor 抽象、零 schema 变化、零 `.iar.toml` 变化**。

### Measurable Objectives

- **OBJ-1**：`iar registry start --repo-id keda-main` 退出码 0，退出后立即 `ps -ef | grep "iar daemon"` 能看到新进程，`iar registry list` 中 `keda-main` 的 daemon 与 review-daemon 显示 running + 进程 ID。
- **OBJ-2**：再次运行 `iar registry start --repo-id keda-main` 因"已存在 running 进程"退出码非零，stderr 提示已存在的进程 ID。
- **OBJ-3**：`iar registry start --all` 启动当前所有 enabled 注册仓的 daemon + review-daemon（除非已 running），并打印每个仓的成功/失败/跳过摘要。
- **OBJ-4**：`iar registry stop --repo-id keda-main` 退出码 0，停止 daemon + review-daemon，`iar registry list` 恢复 stopped，残留进程消失。
- **OBJ-5**：`iar registry stop --all` 停止所有 running 的 daemon + review-daemon，并对每个进程打印结果；重复 stop 退出码 0（幂等）。
- **OBJ-6**：对 disabled 仓调用 `start` 报错退出；对不存在的 repo_id 报错退出；对未 init 的注册仓路径报错退出（与现有 `resolve_repository_targets_with_diagnostics` 行为对齐）。
- **OBJ-7**：`docs/guides/agent-runner.md` registry 小节新增 `start` / `stop` 使用说明，并说明与 `reinit --start-daemons` / `takeover` 的对比。

### Realistic Validation

除单元测试外，本 PRD 要求通过**真实项目入口点**验证关键行为，确保托管进程在真实 OS 环境里被 spawn / 登记 / 停止。

- [x] **单仓 start/stop 真实验证**：`iar registry start --repo-id keda-main` 后立即 `ps` 与 `iar registry list` 双重确认；再 `iar registry stop --repo-id keda-main` 后确认进程消失（覆盖 OBJ-1/2/4）。
- [x] **--all 批量 start/stop 真实验证**：`iar registry start --all` 后列出所有 enabled 仓的 daemon/review-daemon running；`stop --all` 后全部 stopped（覆盖 OBJ-3/5）。
- [x] **双开防护真实验证**：连续两次 `start --repo-id keda-main` 验证第二次非零退出并给出已在运行的进程 ID（覆盖 OBJ-2）。
- [x] **异常路径真实验证**：`iar registry start --repo-id nonexistent` 与 `start --repo-id transmaster`（disabled）验证退出码非零且信息可操作（覆盖 OBJ-6）。

**为什么单元测试不够**：`start_runner_process` 最终要 spawn 真实 OS 子进程并写入 `~/.iar/processes.json`；mock 无法验证 PID 存活、log 目录创建、supervisor 读取 processes.json 后 `registry list` 正确显示 running。双开检测依赖 supervisor 对真实 PID 的探活，mock 会绕过这个核心路径。

### Delivery Dependencies

- Group: none
- Depends on groups:
  - none
- Depends on tasks/issues:
  - none
- Gate type: none
- Notes: 本任务与 `P1-FEAT-20260623-002646-iar-daemon-cwd-infer-single-repo`（cwd 推断 PRD）正交，可独立实现/发布；落地顺序任意。建议本 PRD 落地后，文档里把"手动 daemon"指引改为推荐 `iar registry start --repo-id X`。

## 2. Requirement Shape

- **actor**：在本地已经 init 过某个 / 多个仓库、希望像 `systemctl` 一样用 `iar registry` 管理 daemon 生命周期的开发者。
- **trigger**：执行 `iar registry start --repo-id X`、`iar registry start --all`、`iar registry stop --repo-id X`、`iar registry stop --all`。
- **expected behavior**：
  - `start` 默认起 daemon + review-daemon；`--no-review-daemon` 可只起 daemon
  - `stop` 默认停 daemon + review-daemon；`--no-review-daemon` 可只停 daemon
  - `--repo-id` 与 `--all` 互斥
  - 单仓 `start` 时若任一 kind 已 running → 整体失败并打印已存在进程信息；另一条 kind 不应被起（原子语义，避免半起状态）
  - 单仓 `stop` 时若任一 kind 未 running → 跳过并提示，不视为失败
  - `--all start` 时逐仓执行；已 running 的仓跳过并提示；遇到 disabled / 未 init / 不存在仓记录错误但不阻断其他仓（失败列表最后汇总）
  - `--all stop` 时遍历 `processes.json` 中所有 daemon / review-daemon 记录并尝试停止；未 running 的跳过
- **scope boundary**：只新增 `iar registry start` / `stop` 两个子命令；不修改 `takeover`、`reinit`、`remove` 的现有行为；不修改 `PidfileProcessSupervisor` 的内部状态机；不修改 `.iar.toml` / config.toml schema；不修改 cwd 推断逻辑。

## 3. Repository Context And Architecture Fit

### Current Relevant Modules

- `src/backend/api/cli_parser.py:317-391`：registry 子命令 argparse 注册点（scan / sync / reinit / remove / list），需新增 start / stop parser。
- `src/backend/api/cli_typer.py`：Typer 入口，registry 子命令目前似乎走 legacy argparse（见 `cli.py:482-541`），需确认是否需要同步 typer 命令；若 registry 没有 typer 命令则只需改 argparse 路径。
- `src/backend/api/cli.py:482-541`：registry 子命令 dispatch 分支，需新增 `registry start` / `registry stop` 的调用。
- `src/backend/api/cli_registry.py`：registry 子命令实现所在地，当前有 `_run_registry_reinit_command`、`_run_registry_remove_command`、`_run_registry_list_command`。新增 `_run_registry_start_command` / `_run_registry_stop_command`。
- `src/backend/core/use_cases/console_processes.py:106-169`：现有 `start_runner_process` / `stop_runner_process` API，直接复用。
- `src/backend/engines/agent_runner/factory.py:268+`：现有 `create_process_supervisor`、`load_fresh_agent_runner_settings`、`resolve_repository_targets_with_diagnostics`，直接复用。
- `src/backend/infrastructure/console/process_supervisor.py`：`PidfileProcessSupervisor` 实现，本次只读不改。
- `src/backend/core/shared/interfaces/runner_console.py`：`RunnerProcessKind` 枚举（DAEMON / REVIEW_DAEMON），复用。
- `docs/guides/agent-runner.md`：registry 管理小节，需新增 start/stop 文档。
- `tests/test_agent_runner_cli.py`：registry parser 测试，需新增 start/stop 解析测试。

### Existing Architecture Pattern To Follow

- 四层依赖方向：CLI 层 `api/cli_registry.py` 可调用 `core.use_cases.console_processes` 与 `engines.agent_runner.factory`；不跨层 import infrastructure。
- `console_processes.py` 的 `start_runner_process` 已经封装了"校验 enabled context + 防双开 + 拼 argv + supervisor.spawn"完整流程，新命令不要绕过它。
- `stop_runner_process` 已经封装了"按 process_id 停止 + 刷新 registry"，新命令直接复用。
- 与 `reinit --start-daemons` 的 `_restart_daemons`（`cli_registry.py:202+`）行为对齐：先停旧再起新；`start` 为了安全也应在发现同 kind 已 running 时报错而非直接重启。

### Ownership And Dependency Boundaries

- **新增代码**：`src/backend/api/cli_registry.py` 两个 `_run_registry_*_command` 函数 + parser 注册；`src/backend/api/cli.py` 两个 dispatch 分支；`src/backend/api/cli_parser.py` parser 定义；测试 + 文档。
- **复用代码**：`console_processes.py`、`factory.py`、pydantic settings。
- **不修改**：`PidfileProcessSupervisor` 内部实现；`AgentRunnerSettings` schema；`.iar.toml` schema；`takeover` / `reinit` / `remove` 逻辑。

### Constraints From Runtime, Docs, Tests, Or Workflows

- **单文件 1000 行警告**：`cli.py` 已接近上限，新增 dispatch 分支要非常紧凑（建议每个分支 ≤10 行）；`cli_registry.py` 当前长度可控。
- **init gate**：`start_runner_process` 内部会校验 `repo_id` 在 enabled contexts 中；disabled 仓会在 `_resolve_enabled_context` 处抛出 `ConsoleProcessError`，CLI 层捕获打印即可。
- **路径解析**：`start_runner_process` 的 `spawn_cwd` 必须是真实仓根；从 registry entry 的 `path` 字段得到后传给 `resolve_repository_targets_with_diagnostics`，其返回值里每个 context 已带 `repo_path`。
- **日志目录**：`create_process_supervisor()` 会基于 `settings.console.process_registry_path` 与 `settings.console.log_dir` 初始化；如果 log 目录不存在，需要确认 `PidfileProcessSupervisor` 是否会自动创建（若不会，start command 需显式 `mkdir -p`）。
- **CLAUDE.md 文档同步要求**：行为变更必须同步 `docs/guides/agent-runner.md`。

### Matching Or Related PRDs

- **依赖/关联**：`tasks/pending/P1-FEAT-20260623-002646-iar-daemon-cwd-infer-single-repo`（cwd 推断 PRD）。两个 PRD 正交；本 PRD 落地后，用户可以在任意目录跑 `iar registry start --repo-id keda-main`，spawn 的 daemon 子进程 cwd 落在仓根，同时 argv 带 `--repo-id keda-main`，双重保险。
- **`tasks/pending/` 其他 PRD**：无直接关联。
- **`tasks/archive/`**：`P1-FEAT-20260617-103000-iar-cli-oneclick-install` 涉及 `iar takeover` 流程，可作为参考（其 daemon 启动已走 `start_runner_process`），但不构成依赖。
- **关系判定**：本任务独立，不复制、不阻塞其他 pending PRD。

## 4. Recommendation

### Recommended Approach

1. **新增 argparse parser**（`cli_parser.py` 在 `registry_subparsers` 后追加）：
   - `registry start`：`--repo-id` / `--all`（互斥）/ `--no-review-daemon`（可选）
   - `registry stop`：`--repo-id` / `--all`（互斥）/ `--no-review-daemon`（可选）
2. **新增 dispatch 分支**（`cli.py` 在 `registry list` 分支前或后）：
   ```python
   if parsed.command == "registry start":
       return _run_registry_start_command(parsed, process_runner)
   if parsed.command == "registry stop":
       return _run_registry_stop_command(parsed, process_runner)
   ```
3. **实现 `_run_registry_start_command`**（`cli_registry.py`）：
   - 校验 `--repo-id` 与 `--all` 互斥
   - 加载 `settings = load_fresh_agent_runner_settings()`
   - 解析 `contexts, _ = resolve_repository_targets_with_diagnostics(settings)`
   - 创建 `supervisor = create_process_supervisor()`
   - 取 `runner_command = settings.console.runner_command`（默认 `["iar"]` 或 `["uv", "run", "iar"]`）
   - 确定要处理的 repo_id 列表：
     - `--repo-id`：单元素列表
     - `--all`：`settings.repositories` 中所有 `enabled=True` 的 repo_id（注意：这里不像 cwd 推断 PRD 那样要求 `.iar.toml`，registry entry 即可）
   - 对每仓按 `DAEMON` → `REVIEW_DAEMON` 顺序尝试 spawn：
     - 任一 kind 失败 → 该仓整体失败，不继续 spawn 同仓另一个 kind（避免半起）；打印错误
     - 都成功 → 打印两个进程 ID
   - `--all` 模式汇总：打印成功数 / 失败数 / 跳过数；只要有一仓失败退出码 1，全部成功退出码 0
   - 创建 log 目录（如 supervisor 不自动创建）：`Path(settings.console.log_dir).expanduser().mkdir(parents=True, exist_ok=True)`（通过 supervisor 初始化或显式保证）
4. **实现 `_run_registry_stop_command`**（`cli_registry.py`）：
   - 创建 `supervisor = create_process_supervisor()`
   - 列出所有 processes
   - `--repo-id`：过滤 `record.repo_id == repo_id` 且 `record.kind in {daemon, review_daemon}`（受 `--no-review-daemon` 控制）的记录；逐条 `stop_runner_process`
   - `--all`：同上，不过滤 repo_id
   - 对每条记录先 `_refresh_record`（调用 supervisor 内部方法探活），若 `status != "running"` 则跳过
   - 记录不存在 / 未 running → 打印跳过信息，不视为失败
   - stop 失败（如 SIGTERM 超时）→ 打印警告，退出码 1
5. **文档**：在 `docs/guides/agent-runner.md` 的"Registry 生命周期管理"小节新增 `iar registry start` / `stop` 示例。
6. **测试**：
   - parser 测试：验证 `--repo-id` / `--all` / `--no-review-daemon` 解析正确、互斥报错
   - 单测（mock supervisor）：验证 `_run_registry_start_command` 对单仓调用两次 `start_runner_process`，对 `--all` 调用多次；验证双开时报错；验证 disabled 仓报错
   - 真实验证：见 Realistic Validation Plan

### Why This Is The Best Fit For The Current Architecture

- 复用 `start_runner_process` / `stop_runner_process`：它们就是为此设计的 API，避免重复实现 spawn / pidfile / log 逻辑。
- 复用 `create_process_supervisor`：与 `takeover` / `reinit --start-daemons` 共用同一个 supervisor，保证 `processes.json` 格式一致。
- 不修改 supervisor：保持 infrastructure 层稳定，降低回归风险。
- 命令语义与 `systemctl start/stop` 对齐：开发者容易理解。

### Rationale For Rejecting Redundant Abstractions

- 不新建 `ProcessLifecycleManager`：现有 `console_processes.py` 已经封装。
- 不新建 `DaemonManager` use case：单仓 start/stop 只是 CLI 层到现有 core use case 的薄封装。
- 不新建 schema：复用 registry 的 `enabled` 字段与 `processes.json`。

### Alternatives Considered

- **Alternative A：`iar daemon --register` 选项**：让前台 `iar daemon` 自己把自己登记到 processes.json。
  - 拒绝理由：破坏进程责任边界——前台 daemon 进程不应该知道自己被 supervisor 管理；`takeover` / `reinit` 已经证明由外部命令 spawn 并登记是更清晰的设计。
- **Alternative B：把 start/stop 合并到 `iar registry reinit --start-daemons`**：不改代码，只让用户继续用 reinit。
  - 拒绝理由：reinit 会强制重置 `.iar.toml`（`force=True`），用户痛点就是不想 reinit；新命令必须不碰 `.iar.toml`。
- **Alternative C：每个 kind 单独命令**：`iar registry start-daemon --repo-id X`、`iar registry start-review-daemon --repo-id X`。
  - 拒绝理由：命令数量翻倍，且与 `takeover` 同时起两个 daemon 的惯例不一致；用 `--no-review-daemon` 一个 flag 足够表达细粒度需求。

## 5. Implementation Guide

> This section is a living implementation guide based on current repository analysis. If implementation discovers additional affected files, hidden dependencies, edge cases, or a better path, update this PRD before proceeding.

### Core Logic

**Start flow**

```text
user runs "iar registry start --repo-id keda-main"
  ↓
cli_parser.py 解析 -> Namespace(command="registry start", repo_id="keda-main", all=False, no_review_daemon=False)
  ↓
cli.py _run_parsed_command -> "registry start" 分支 -> _run_registry_start_command(parsed, process_runner)
  ↓
load_fresh_agent_runner_settings()
resolve_repository_targets_with_diagnostics(settings) -> contexts
  ↓
create_process_supervisor()
  ↓
for repo_id in selected_ids:
  for kind in (DAEMON, REVIEW_DAEMON) unless no_review_daemon:
    start_runner_process(repo_id, kind, contexts, supervisor, runner_command, spawn_cwd=repo_path)
    print "Started daemon for keda-main (process xxx)"
  ↓
return 0 if all succeeded else 1
```

**Stop flow**

```text
user runs "iar registry stop --repo-id keda-main"
  ↓
_run_registry_stop_command
  ↓
create_process_supervisor()
supervisor.list_processes()
  ↓
for record in matched_records:
  if record.status == "running":
    stop_runner_process(record.process_id, supervisor, stop_timeout_seconds=30)
    print "Stopped keda-main daemon (process xxx)"
  else:
    print "Skipped keda-main daemon (not running)"
  ↓
return 0 if all stop succeeded else 1
```

### Change Impact Tree

```text
.
├── src/backend/api/
│   ├── cli_parser.py
│   │   [修改]
│   │   【新增 registry start / stop 两个 subparser；支持 --repo-id / --all / --no-review-daemon】
│   │
│   │   └── 新增 "start" subparser（约 25-35 行）
│   │   └── 新增 "stop" subparser（约 20-30 行）
│   │
│   ├── cli.py
│   │   [修改]
│   │   【新增 registry start / stop dispatch 分支】
│   │
│   │   ├── import _run_registry_start_command / _run_registry_stop_command
│   │   └── 新增 if parsed.command == "registry start" / "registry stop" 分支（每个约 2-3 行）
│   │
│   └── cli_registry.py
│       [修改]
│       【实现 _run_registry_start_command / _run_registry_stop_command】
│
│       ├── 新增 helper _repo_ids_to_start(settings, parsed)
│       ├── 新增 _run_registry_start_command（约 60-80 行）
│       └── 新增 _run_registry_stop_command（约 40-60 行）
│
├── src/backend/core/use_cases/
│   └── console_processes.py
│       [无需修改]
│       【复用 start_runner_process / stop_runner_process】
│
├── src/backend/engines/agent_runner/
│   └── factory.py
│       [无需修改]
│       【复用 load_fresh_agent_runner_settings / create_process_supervisor / resolve_repository_targets_with_diagnostics】
│
├── src/backend/infrastructure/console/
│   └── process_supervisor.py
│       [无需修改]
│       【只读；PidfileProcessSupervisor 行为不变】
│
├── tests/
│   └── test_agent_runner_cli.py
│       [修改]
│       【新增 parser 测试与 _run_registry_start/stop 的 mock 测试】
│
│       ├── test_cli_parser_registry_start
│       ├── test_cli_parser_registry_start_all
│       ├── test_cli_parser_registry_start_no_review_daemon
│       ├── test_cli_parser_registry_stop
│       ├── test_main_registry_start_calls_start_runner_process
│       ├── test_main_registry_start_all_iterates_enabled_repos
│       ├── test_main_registry_start_skips_review_daemon
│       └── test_main_registry_stop_calls_stop_runner_process
│
└── docs/
    └── guides/agent-runner.md
        [修改]
        【在 Registry 生命周期管理小节新增 start/stop 用法示例】

        ├── 新增 "启动托管 daemon" 小节
        └── 新增 "停止托管 daemon" 小节
```

### Executor Drift Guard

执行前用以下 `rg` 命令复查边界：

```bash
# 确认 registry 子命令的 parser/dispatch 位置
rg -n "registry_parser|registry_command|registry start|registry stop" src/backend/api/

# 确认 start_runner_process / stop_runner_process 调用点（复用者）
rg -n "start_runner_process|stop_runner_process" src/backend/

# 确认 RunnerProcessKind 定义
rg -n "class RunnerProcessKind|DAEMON|REVIEW_DAEMON" src/backend/core/shared/interfaces/runner_console.py

# 确认 processes.json 路径配置
rg -n "process_registry_path|log_dir|runner_command" src/backend/infrastructure/config/settings.py

# 确认文档位置
rg -n "Registry 生命周期管理|iar registry" docs/guides/agent-runner.md
```

**潜在隐藏引用**：
- `cli_typer.py` 是否也注册了 `registry` 子命令。当前代码里 `registry` 似乎只有 legacy argparse 入口（`cli_parser.py` + `cli.py`），但执行前应 `rg -n "registry" src/backend/api/cli_typer.py` 确认。
- `console_processes.py` 的 `_resolve_enabled_context` 要求 `repo_id` 在 `contexts` 中；若仓 `enabled=false` 则 context 不会被解析出来，`start_runner_process` 会抛出 `ConsoleProcessError`，CLI 层要 catch。
- `stop_runner_process` 接受 `process_id` 而不是 `repo_id`；stop 命令需要先 `list_processes()` 按 repo_id + kind 过滤。
- `processes.json` 中 kind 存的字符串是 `RunnerProcessKind.DAEMON.value`（即 `"daemon"`）与 `RunnerProcessKind.REVIEW_DAEMON.value`（即 `"review_daemon"`）。stop 命令过滤时要和 `record.kind` 字符串比较，或直接用 `RunnerProcessKind` 枚举比较（推荐枚举）。

**验证失败排错要点**：
- `start` 报 "is not an enabled registry target"：检查 `~/.iar/config.toml` 中 `repo_id` 是否存在且 `enabled=true`。
- `start` 报 "A running ... process already exists"：已有同 kind 进程；先 `stop` 再 `start`。
- `start` 后 `ps` 能看到但 `registry list` 显示 stopped：可能是 supervisor 与 registry list 用了不同的 processes.json 路径；检查 `settings.console.process_registry_path`。
- `stop` 报 process not found：进程可能已退出但 processes.json 残留；supervisor 的 `stop` 会刷新记录；如果还是报，可手动 `pkill -f "iar daemon --repo-id X"`。

### Flow Diagram

```mermaid
flowchart TD
    A[iar registry start] --> B{--repo-id or --all?}
    B -- --repo-id --> C[resolve single repo_id]
    B -- --all --> D[iterate all enabled repo_ids]
    C --> E{no_review_daemon?}
    D --> E
    E -- no --> F[kinds = DAEMON, REVIEW_DAEMON]
    E -- yes --> G[kinds = DAEMON]
    F --> H[for each repo_id × kind:
start_runner_process]
    G --> H
    H --> I{all succeeded?}
    I -- yes --> J[exit 0]
    I -- no --> K[exit 1]

    L[iar registry stop] --> M{--repo-id or --all?}
    M -- --repo-id --> N[filter processes by repo_id]
    M -- --all --> O[all daemon/review-daemon processes]
    N --> P{no_review_daemon?}
    O --> P
    P -- no --> Q[kinds = DAEMON, REVIEW_DAEMON]
    P -- yes --> R[kinds = DAEMON]
    Q --> S[for each matched running process:
stop_runner_process]
    R --> S
    S --> T{all succeeded?}
    T -- yes --> U[exit 0]
    T -- no --> V[exit 1]
```

### Realistic Validation Plan

| Behavior | Real Entry Point | Test Layer | Mock Boundary | Data/Env Needed | Command Or Procedure | Required For Acceptance |
|---|---|---|---|---|---|---|
| 单仓 start 成功 | `iar registry start --repo-id keda-main` | integration (real CLI) | 不 mock OS 子进程；真实 spawn | `keda-main` enabled 且已 init | `iar registry start --repo-id keda-main`；然后 `ps -ef | grep "iar daemon"` 与 `iar registry list` 都确认 running | Yes |
| 双开防护 | 同上第二次执行 | integration (real CLI) | 不 mock supervisor | 上一条已 running | 再次执行 `iar registry start --repo-id keda-main`；验证 exit code 非零，stderr 含 "already exists" 与原进程 ID | Yes |
| 单仓 stop 成功 | `iar registry stop --repo-id keda-main` | integration (real CLI) | 不 mock | daemon / review-daemon 已 running | `iar registry stop --repo-id keda-main`；`ps` 无残留；`iar registry list` 显示 stopped | Yes |
| --all start | `iar registry start --all` | integration (real CLI) | 不 mock | 至少两个 enabled 仓（keda-main + fsense） | 执行后每个 enabled 仓的 daemon + review-daemon 都 running；`registry list` 对应行列显示 running | Yes |
| --all stop | `iar registry stop --all` | integration (real CLI) | 不 mock | 上一条后所有 daemon running | 执行后所有 daemon / review-daemon stopped；`ps` 无残留 | Yes |
| disabled 仓 start 拒绝 | `iar registry start --repo-id transmaster` | integration (real CLI) | 不 mock | `transmaster` registry entry 存在但 `enabled=false` | 验证 exit code 非零，stderr 含 "not an enabled registry target" | Yes |
| 不存在 repo_id 拒绝 | `iar registry start --repo-id nonexistent` | integration (real CLI) | 不 mock | 不需要 | 验证 exit code 非零，stderr 提示 repo_id 不存在 | Yes |
| parser 解析正确 | `iar registry start --help` / `iar registry stop --help` | unit/parser | mock argparse | 不需要 | 跑 pytest 对应测试 | Yes |
| 文档同步 | 阅读 `docs/guides/agent-runner.md` | manual | 不适用 | 不需要 | 检查新增 "启动托管 daemon" / "停止托管 daemon" 小节存在且示例可执行 | Yes |

**Failure triage**：
- start 后 daemon 进程存在但 `registry list` 显示 stopped：先检查 `~/.iar/processes.json` 是否写入；再检查 `registry list` 读取的 `process_registry_path` 配置与 start 命令用的是同一个；再检查是否有权限问题。
- start 成功但 review-daemon 没起：检查 `--no-review-daemon` 是否误传；检查 `start_runner_process` 对 REVIEW_DAEMON 调用是否抛出异常。
- `--all` 部分仓失败：每个失败仓会独立打印错误；汇总退出码 1；修复失败原因后重跑即可（幂等跳过已 running）。

### Low-Fidelity Prototype

不需要。CLI 行为，无 UI 组件。

### ER Diagram

无数据模型变化；不修改 pydantic schema、不新增表/实体。

### Interactive Prototype Change Log

无交互式原型文件变化。

### External Validation

| Topic | Source | Checked On | Relevant Finding | Impact On Recommendation |
|---|---|---|---|---|
| — | — | — | 无外部事实需要查询；所有依赖 API 与配置 schema 已内置于仓库代码 | — |

## 6. Definition Of Done

- `iar registry start --repo-id X` / `--all` 实现并跑通真实 OS spawn
- `iar registry stop --repo-id X` / `--all` 实现并跑通真实 OS stop
- `iar registry list` 能反映 start/stop 后的 running/stopped 状态
- 双开检测生效；disabled / 不存在 repo_id 报错
- `just test` 全量通过
- `docs/guides/agent-runner.md` registry 小节已更新
- PRD 归档前 acceptance checklist 全部勾选

## 7. Acceptance Checklist

### Architecture Acceptance

- [x] 新增命令实现位于 `src/backend/api/cli_registry.py`，未在 cli.py 或 parser 中重复实现 spawn/stop 逻辑
- [x] `start_runner_process` / `stop_runner_process` 被直接复用，未绕过或复制到 api 层
- [x] 依赖方向正确：`cli_registry.py` import 来源仅限 `backend.api.cli_console` / `backend.core.use_cases.console_processes` / `backend.engines.agent_runner.factory` 等（不跨层 import infrastructure）
- [x] 未修改 `src/backend/infrastructure/console/process_supervisor.py` 的 supervisor 内部实现
- [x] 未修改 pydantic settings 模型、`.iar.toml` schema、config.toml 字段

### Behavior Acceptance

- [x] `iar registry start --repo-id keda-main` 成功启动 daemon + review-daemon（OBJ-1）
- [x] 再次 `start --repo-id keda-main` 因已 running 报错退出（OBJ-2）
- [x] `iar registry start --all` 启动所有 enabled 注册仓的两个 daemon（OBJ-3）
- [x] `iar registry start --repo-id X --no-review-daemon` 仅启动 daemon
- [x] `iar registry stop --repo-id keda-main` 停止两个 daemon（OBJ-4）
- [x] `iar registry stop --repo-id X --no-review-daemon` 仅停止 daemon
- [x] `iar registry stop --all` 停止所有 running daemon + review-daemon（OBJ-5）
- [x] 重复 `stop --repo-id X` 退出码 0 并提示未 running（幂等）
- [x] `start --repo-id transmaster`（disabled）报错退出（OBJ-6）
- [x] `start --repo-id nonexistent` 报错退出（OBJ-6）
- [x] `--repo-id` 与 `--all` 同时传时 argparse 报错互斥

### Parser / CLI Entry Acceptance

- [x] `cli_parser.py` 新增 `registry start` subparser，支持 `--repo-id`、`--all`、`--no-review-daemon`
- [x] `cli_parser.py` 新增 `registry stop` subparser，支持 `--repo-id`、`--all`、`--no-review-daemon`
- [x] `cli.py` 新增 `registry start` 与 `registry stop` dispatch 分支
- [x] `cli_typer.py` 如有 registry 入口则同步；如无则确认 argparse 路径仍生效
- [x] 测试覆盖 parser 对 `--repo-id` / `--all` / `--no-review-daemon` 的解析与互斥

### Documentation Acceptance

- [x] `docs/guides/agent-runner.md` 新增 "启动托管 daemon" 小节，给出 `iar registry start --repo-id X` 与 `start --all` 示例
- [x] `docs/guides/agent-runner.md` 新增 "停止托管 daemon" 小节，给出 `iar registry stop --repo-id X` 与 `stop --all` 示例
- [x] 文档说明 `registry start` 与 `reinit --start-daemons` / `takeover` 的区别（不重置 `.iar.toml`）
- [x] 文档说明与 launchd / systemd 开机自启的集成关系（start 命令适合放进 launchd plist 的 `ProgramArguments`）

### Validation Acceptance

- [x] `just test` 在仓库根目录执行后全部通过
- [x] Realistic Validation Plan 第 1 行（单仓 start）手动验证通过
- [x] Realistic Validation Plan 第 2 行（双开防护）手动验证通过
- [x] Realistic Validation Plan 第 3 行（单仓 stop）手动验证通过
- [x] Realistic Validation Plan 第 4 行（--all start）手动验证通过
- [x] Realistic Validation Plan 第 5 行（--all stop）手动验证通过
- [x] Realistic Validation Plan 第 6 行（disabled 仓拒绝）手动验证通过
- [x] Realistic Validation Plan 第 7 行（不存在 repo_id 拒绝）手动验证通过
- [x] 新增单元测试 `test_main_registry_start_*` / `test_main_registry_stop_*` 通过

## 8. Functional Requirements

- **FR-1**：`iar registry start --repo-id <repo_id>` 必须启动该仓的 daemon + review-daemon（除非 `--no-review-daemon` 指定），并把两个进程登记到 `~/.iar/processes.json`。
- **FR-2**：`iar registry start --all` 必须遍历 `~/.iar/config.toml` 中所有 `enabled=true` 的注册仓，为每个仓执行 FR-1；已 running 的进程跳过并提示；任一仓失败时整体退出码 1，但不阻止其他仓尝试。
- **FR-3**：`iar registry start` 必须拒绝 disabled 仓、不存在 repo_id、以及同 kind 已有 running 进程的情况，并以非零退出码退出。
- **FR-4**：`iar registry stop --repo-id <repo_id>` 必须停止该仓的 daemon + review-daemon（除非 `--no-review-daemon` 指定），从 `processes.json` 刷新状态，并释放 OS 进程。
- **FR-5**：`iar registry stop --all` 必须停止所有 `processes.json` 中状态为 running 的 daemon + review-daemon；未 running 的记录跳过并提示；任一 stop 失败时整体退出码 1。
- **FR-6**：`--repo-id` 与 `--all` 必须互斥；同时指定时 argparse 报错。
- **FR-7**：start 命令的 `spawn_cwd` 必须是 registry entry 的 `path` 字段对应的绝对路径，以保证子进程 cwd 落在仓根。
- **FR-8**：stop 命令必须先调用 supervisor 的 refresh 机制（或等价逻辑）确认进程仍活着；对已经 exited 的记录不调用 `stop_runner_process` 或捕获其幂等结果，避免误报错。
- **FR-9**：start / stop 命令本身不修改 `.iar.toml` 内容，不重新初始化仓库配置。
- **FR-10**：新增命令的 parser 与 dispatch 必须同时覆盖 argparse 与 Typer（如果 Typer 已暴露 registry 入口）两个入口。

## 9. Non-Goals

- 不改写 `iar daemon` / `iar review-daemon` 的默认行为
- 不修改 cwd 推断 PRD 的语义
- 不修改 `PidfileProcessSupervisor` 内部实现（不新增自动恢复、心跳、过期清理等）
- 不修改 `takeover`、`reinit --start-daemons`、`remove` 的现有行为
- 不新增新的 config.toml / `.iar.toml` 字段
- 不为非 daemon 的 `run_once` / `blocked_continue` 进程新增 start/stop 管理（仅 daemon / review-daemon）
- 不提供"开机自启 plist 模板自动生成"（文档中给出示例即可，不引入新命令）

## 10. Risks And Follow-Ups

- **R-1（双开竞争）**：如果用户同时手动前台 `iar daemon --repo-id X` 和 `iar registry start --repo-id X`，supervisor 的 PID 探活可能无法识别前台进程（因为它不在 processes.json 里），从而 spawn 出第二个 daemon，导致两个进程同时 claim 同一仓的 Issues。缓解：`start` 双开检测基于 processes.json，无法阻止前台手动启动；文档中明确建议"要么用 registry start 管理，要么手动前台运行，不要混用"。
- **R-2（stop 后前台进程残留）**：如果用户之前用前台 `iar daemon` 起的进程未停止，registry stop 会只清理 processes.json 里的记录，前台进程继续跑。缓解：同 R-1，文档说明混用风险。
- **R-3（--all 遇到未 init 的注册仓）**：registry entry 中 `path` 存在但仓内没有 `.iar.toml`（被删除或移动）。`resolve_repository_targets_with_diagnostics` 会产出 failure entry；`start_runner_process` 在 `_resolve_enabled_context` 阶段会拒绝；`--all` 会打印失败但继续处理其他仓。这不是回归，是配置问题。
- **R-4（日志目录不存在）**：如果 `settings.console.log_dir` 目录不存在，`supervisor.spawn` 可能失败。实现时要在 supervisor 初始化后或 start 命令开头 `mkdir -p`。
- **Follow-up F-1**：后续可新增 `iar registry restart --repo-id X`（等价于 stop + start），作为 start/stop 的便捷组合。
- **Follow-up F-2**：后续可新增 `iar registry status --repo-id X`（只显示单个仓的进程状态），避免总看 list 表。

## 11. Decision Log

| ID | Decision | Chosen | Rejected | Rationale |
|---|---|---|---|---|
| D-01 | 命令形态 | `iar registry start/stop --repo-id X` / `--all` | 作为 `iar daemon --register` 选项（Alt A）；拆成 `start-daemon` / `start-review-daemon`（Alt C） | 与现有 `takeover` / `reinit --start-daemons` 的托管进程语义一致；单组 start/stop 命令最符合 systemctl 心智；避免 daemon 进程自管理 |
| D-02 | 默认启动哪些 daemon | 同时启动 daemon + review-daemon | 默认只起 daemon | 与 takeover 行为一致；review-daemon 是 PR 创建后自动维护的标准配套 |
| D-03 | 是否支持 `--no-review-daemon` | 支持 | 不支持 | 给用户细粒度控制，避免某些场景下不需要 review-daemon 时浪费资源 |
| D-04 | `--all` 遇到部分失败 | 继续处理其他仓，退出码 1 | 立即停止 | daemon 各仓之间无依赖，部分失败不应阻断其他仓的正常运行 |
| D-05 | 单仓 start 部分 kind 失败 | 整体失败，不继续 spawn 同仓另一个 kind | 继续另一个 kind | 避免半起状态；用户看到的是"keda-main 启动失败"，而不是"daemon 起了但 review-daemon 没起"的迷惑状态 |
| D-06 | stop 的幂等语义 | 未 running 记录跳过，退出码 0 | 未 running 记录报错 | stop 的语义是"确保停止"；已经停止视为目标达成 |
| D-07 | 是否修改 supervisor 实现 | 不修改，只复用 API | 在 supervisor 里加 `restart_all_on_boot` | 本任务 scope 是新增 CLI 入口；自动恢复是另一个独立问题，可用 launchd 方案先解决 |
| D-08 | 是否同步更新 docs | 是 | 否 | CLAUDE.md 要求；同时 `registry start` 是用户高频入口，文档缺失会导致继续用 tmux / nohup 旧方式 |
| D-09 | 与 cwd 推断 PRD 的关系 | 正交独立 | 合并到 cwd 推断 PRD | 两者解决的问题不同，合并会膨胀 scope 并延缓交付；落地顺序任意 |
