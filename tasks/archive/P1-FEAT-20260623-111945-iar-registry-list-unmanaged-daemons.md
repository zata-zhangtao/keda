# iAR registry list 检测未托管 daemon

## 1. Introduction & Goals

当前 `iar registry list` 只读取 `~/.iar/processes.json` 中由 `iar registry start` / console / `iar takeover` 登记的**托管进程**。开发者直接执行 `iar daemon` 或 `iar review-daemon` 启动的前台/后台进程不会被列出，导致 `registry list` 呈现的 running/stopped 状态与实际系统进程不一致。这不仅误导用户，还可能造成同一仓库被两个 daemon 同时 claim Issues 的竞争问题。

### Proposed Solution Summary

扩展 `IRunnerProcessSupervisor` 端口与 `PidfileProcessSupervisor` 实现，新增**系统进程扫描**能力：在 `iar registry list` 执行时，除读取 `processes.json` 外，还通过 `psutil` 扫描当前用户可见的 `iar daemon` / `iar review-daemon` 进程，将不在托管登记表中的存活实例识别为 `running (unmanaged)`。未托管进程**仅用于观测**，不改变 `registry stop` 的行为（stop 继续只操作 `processes.json` 中的托管记录）。输出层面在现有 `daemon` / `review-daemon` 列用附加标签区分 managed / unmanaged，不新增列，保持表格紧凑。

### Measurable Objectives

- **OBJ-1**：当系统中有手动 `iar daemon --repo-id keda-main` 进程在跑且 `processes.json` 无对应托管记录时，`iar registry list` 中 `keda-main` 的 `daemon` 列显示 `running (unmanaged)`。
- **OBJ-2**：`iar registry stop --repo-id keda-main` 只停止 `processes.json` 中的托管进程，不会因为 list 中显示 unmanaged 而尝试停止它。
- **OBJ-3**：`iar registry list` 对同仓库同时存在托管 running 和未托管 running 的情况，优先显示托管状态，避免重复计数。
- **OBJ-4**：未托管进程识别同时覆盖 `daemon` 与 `review-daemon` 两种 kind。
- **OBJ-5**：文档 `docs/guides/agent-runner.md` 明确说明 managed / unmanaged daemon 的区别与混用风险。

### Realistic Validation

除单元测试和集成测试外，本 PRD 要求通过**真实项目入口点**验证关键行为，确保真实使用路径生效，而非仅在隔离 fixture 中通过。

- [x] **未托管 daemon 检测真实验证**：先手动 `iar daemon --repo-id keda-main` 启动前台进程，再执行 `iar registry list`，验证对应仓库 `daemon` 列显示 `running (unmanaged)`。
- [x] **托管与未托管共存真实验证**：在已有手动 daemon 运行的前提下执行 `iar registry start --repo-id keda-main`，验证 `registry list` 仍显示 managed running，不出现双行或重复 unmanaged 标记。
- [x] **stop 不影响未托管进程真实验证**：执行 `iar registry stop --repo-id keda-main` 后，手动启动的 daemon 进程仍然存活，且 `registry list` 继续显示 `running (unmanaged)`。

**为什么单元测试不够**：未托管进程检测依赖真实 OS 进程扫描、命令行解析与 pid 存活判断；mock 无法验证 `psutil` 在真实系统进程表上的行为，也无法证明 `registry stop` 不会误杀未托管进程。

### Delivery Dependencies

- Group: iar-cli-process-management
- Depends on groups:
  - none
- Depends on tasks/issues:
  - none（相关 PRD `P1-FEAT-20260623-012835-iar-registry-start-stop-daemon` 与 `P1-BUG-20260623-105846-iar-registry-list-and-takeover-daemon-cwd-fix` 已归档完成，本 PRD 在此基础上扩展）
- Gate type: none
- Notes: 本变更独立交付，不阻塞也不依赖其他 pending PRD。

## 2. Requirement Shape

- **actor**：使用 `iar registry list` 观察 daemon 状态的开发者 / 运维人员。
- **trigger**：执行 `iar registry list`；系统中有手动启动的 `iar daemon` 或 `iar review-daemon` 进程。
- **expected behavior**：
  - `registry list` 表格继续列出 `config.toml` 中所有已注册仓库。
  - 对每个仓库的 `daemon` / `review-daemon` 列，综合 `processes.json` 中的托管记录与系统进程扫描结果给出最终状态。
  - 托管 running 显示 `running (<process_id>)`；未托管 running 显示 `running (unmanaged)`；未运行显示 `stopped`。
  - 当托管与未托管同时存在时，显示托管状态（managed 优先）。
  - `registry stop` 的过滤逻辑保持不变，只处理 `processes.json` 中的 running 记录。
- **explicit scope boundary**：
  - 只增加"观测"能力，不增加对未托管进程的生命周期管理。
  - 不修改 `iar daemon` / `iar review-daemon` 命令本身，不要求它们自我登记。
  - 仅识别命令行中可解析出 `--repo-id` 或可匹配到 registry 路径的进程；无法识别的进程不显示。

## 3. Repository Context And Architecture Fit

### Current Relevant Modules

- `src/backend/core/shared/interfaces/runner_console.py`
  - 定义 `IRunnerProcessSupervisor`、`RunnerProcessRecord`、`RunnerProcessKind`。
  - 当前注释声明"不接管用户手工启动的 CLI 进程"，需要更新以反映新的观测能力。
- `src/backend/infrastructure/console/process_supervisor.py`
  - `PidfileProcessSupervisor` 实现，管理 `~/.iar/processes.json` 与 OS 子进程 spawn。
  - 新增未托管进程扫描逻辑的天然位置（基础设施层负责与 OS 进程表交互）。
- `src/backend/engines/agent_runner/factory.py`
  - `create_process_supervisor()` 组装 `PidfileProcessSupervisor` 实例。
  - 可能需要把 `config.toml` 路径 / registry 路径传入 supervisor，以便扫描时匹配 repo。
- `src/backend/api/cli_registry.py`
  - `_run_registry_list_command` 消费 supervisor 数据并渲染 Rich Table。
  - 需要合并托管记录与未托管扫描结果。
- `tests/test_cli_registry.py`
  - 现有 registry list 单元测试；需要补充 unmanaged 状态渲染用例。
- `docs/guides/agent-runner.md`
  - registry 生命周期管理章节需要补充 unmanaged 状态说明。

### Existing Architecture Pattern

- 四层依赖方向：`api/` → `core/` / `engines/`；`engines/` → `core/` / `infrastructure/`；`infrastructure/` 只依赖外部包。
- 进程监管使用端口 `IRunnerProcessSupervisor`，实现放在 `infrastructure/console/process_supervisor.py`。
- `processes.json` 是托管进程登记表，与系统进程表解耦。

### Ownership And Dependency Boundaries

- 系统进程扫描属于基础设施能力，必须放在 `infrastructure/` 层，禁止在 `core/` 或 `api/` 中直接调用 `psutil`。
- `core/shared/interfaces/runner_console.py` 只定义端口契约，不感知实现细节。
- `api/cli_registry.py` 通过 `create_process_supervisor()` 获取 supervisor，只调用端口方法。

### Constraints

- `psutil` 当前不在 `pyproject.toml` 直接依赖中，但实际环境已安装（可能是间接依赖）。为保证可重复性，需要加入 `pyproject.toml` 的依赖列表。
- 必须兼容 macOS 与 Linux；`psutil` 提供跨平台进程枚举，优于直接调用 `ps`。
- 扫描应限制为当前用户拥有的进程，避免读取其他用户进程带来的权限噪音。
- 未托管进程没有 `process_id` 和 `log_path`，需要构造合成标识（建议用 `pid` 作为 display id）。

### Matching Or Related PRDs

- **已归档**：`tasks/archive/P1-FEAT-20260623-012835-iar-registry-start-stop-daemon.md`
  - 引入 `iar registry start/stop` 与 `processes.json` 托管模型。
  - 其风险章节 R-1/R-2 已明确指出手动 `iar daemon` 与托管 daemon 混用会导致状态不一致；本 PRD 正是对那一风险的缓解。
- **已归档**：`tasks/archive/P1-BUG-20260623-105846-iar-registry-list-and-takeover-daemon-cwd-fix.md`
  - 修复 `registry list` 对已退出记录显示 `running` 的 bug，并统一 spawn cwd。
  - 本 PRD 在其过滤后的真实 running 记录基础上叠加未托管扫描。
- **pending 中无直接重叠**：
  - `P2-FEAT-20260623-110000-iar-daemon-default-current-repo-only.md` 只涉及 daemon 默认目标解析，不冲突。
  - 其他 pending PRD 与进程监管无关。

## 4. Recommendation

### Recommended Approach

**最小改动：扩展 supervisor 端口与实现，list 命令做结果合并。**

1. 在 `IRunnerProcessSupervisor` 新增 `list_unmanaged_processes(registry_entries: list[RegistryRepositoryEntry]) -> list[RunnerProcessRecord]` 抽象方法。
2. 在 `PidfileProcessSupervisor` 实现该方法：
   - 使用 `psutil.process_iter(["pid", "name", "cmdline", "username", "cwd"])` 遍历当前用户进程。
   - 过滤命令行以 `iar` 开头且包含 `daemon` / `review-daemon` 子命令的进程。
   - 解析 `--repo-id`；若无，则尝试用 `cwd` 匹配 `registry_entries` 中的 `path`。
   - 排除已存在于 `processes.json` 中的 pid（即托管进程）。
   - 返回合成 `RunnerProcessRecord`：
     - `process_id = f"unmanaged-{pid}"`
     - `status = "running"`
     - `kind = RunnerProcessKind.DAEMON / REVIEW_DAEMON`
     - `log_path = ""`
     - `command = tuple(cmdline)`
     - `started_at = process.create_time()` 的 ISO 格式（不可用则留空）
3. 在 `create_process_supervisor()` 中确保 supervisor 能获取 registry 条目（通过设置或构造函数参数）。
4. 在 `_run_registry_list_command` 中：
   - 先获取托管记录并按 `(repo_id, kind)` 去重。
   - 再调用 `supervisor.list_unmanaged_processes(registry_entries)`。
   - 对每个 registry 仓库、每个 kind，合并规则：
     - 若存在托管 running 记录，显示 managed 状态。
     - 否则若存在未托管 running 记录，显示 `running (unmanaged)`。
     - 否则显示 `stopped`。
5. 更新 `_format_process_status` 以支持 unmanaged 标记。
6. 更新 `docs/guides/agent-runner.md` 与 `pyproject.toml`。

### Why This Is The Best Fit

- 不破坏现有托管模型：`processes.json` 继续是 stop / log / restart 的唯一依据。
- 改动集中在基础设施层与 CLI 渲染层，core 层只增加一个端口方法，符合依赖方向。
- 使用 `psutil` 是跨平台扫描系统进程的最小依赖，避免 shell out 到 `ps` 的平台差异。
- 对 `registry stop` 零影响，避免误杀用户手动启动的进程。

### Alternatives Considered

- **Alternative A：让 `iar daemon` 自我登记到 `processes.json`**
  - 需要 CLI 入口直接依赖 supervisor / infrastructure，破坏 `api/` 不直接依赖 `infrastructure/` 的分层约束。
  - 手动进程可能由 cron / launchd / systemd 启动，生命周期与 `iar` CLI 解耦，自我登记会在异常退出时留下脏记录。
  -  rejected：违反架构约束且引入状态同步风险。

- **Alternative B：在 `api/cli_registry.py` 直接调用 `psutil`**
  - 更短代码路径，但把系统进程扫描放在 API 层，破坏基础设施层边界。
  - 后续若 console API 也需要未托管检测，会重复实现。
  - rejected：违反分层原则。

- **Alternative C：新增独立扫描模块，不扩展 supervisor 端口**
  - 需要 CLI 同时依赖 supervisor 和新模块，增加概念数量。
  - 进程扫描与 supervisor 天然相关（都是 OS 进程观察），分开反而造成冗余。
  - rejected：不如扩展现有端口内聚。

## 5. Implementation Guide

> This section is a living implementation guide based on current repository analysis. If implementation discovers additional affected files, hidden dependencies, edge cases, or a better path, update this PRD before proceeding.

### Core Logic

1. `iar registry list` 调用 `_run_registry_list_command(process_runner)`。
2. `_run_registry_list_command` 构造 `create_registry_editor()` 与 `create_process_supervisor()`。
3. `supervisor.list_processes()` 返回托管记录（已刷新 running/exited）。
4. 新增 `supervisor.list_unmanaged_processes(registry_entries)` 返回未托管记录。
5. CLI 按 `(repo_id, kind)` 聚合两种记录，managed 优先，渲染表格。

### Change Impact Tree

```text
.
├── src/backend/core/shared/interfaces/runner_console.py
│   [修改]
│   【更新端口契约：IRunnerProcessSupervisor 新增 list_unmanaged_processes 方法，并修正“不接管手工进程”的过时注释】
│   ├── 新增 abstractmethod list_unmanaged_processes
│   └── 更新 RunnerProcessRecord / IRunnerProcessSupervisor 注释
│
├── src/backend/infrastructure/console/process_supervisor.py
│   [修改]
│   【PidfileProcessSupervisor 实现系统进程扫描，识别不在 processes.json 中的 iar daemon / review-daemon 实例】
│   ├── 新增 _parse_repo_id_from_argv / _resolve_repo_id_from_cwd 等辅助函数
│   ├── 实现 list_unmanaged_processes 方法
│   └── 依赖 psutil 进行跨平台进程枚举
│
├── src/backend/engines/agent_runner/factory.py
│   [修改]
│   【create_process_supervisor 传入 registry 条目或配置，使 supervisor 扫描时能按 cwd 匹配 repo_id】
│   └── 调整 create_process_supervisor 签名/实现
│
├── src/backend/api/cli_registry.py
│   [修改]
│   【_run_registry_list_command 合并托管与未托管记录，_format_process_status 显示 unmanaged 标记】
│   ├── list 命令调用 supervisor.list_unmanaged_processes
│   ├── 合并 running 视图时 managed 优先
│   └── _format_process_status 支持 unmanaged 状态
│
├── tests/test_cli_registry.py
│   [修改]
│   【补充 registry list 对 running (unmanaged) 状态的单元测试】
│   ├── 测试仅未托管 running 时显示 running (unmanaged)
│   ├── 测试托管与未托管共存时显示 managed
│   └── 测试 stop 命令不会因 unmanaged 记录而匹配到进程
│
├── tests/test_console_processes.py 或新增 tests/test_process_supervisor.py
│   [新增/修改]
│   【覆盖未托管进程扫描的辅助逻辑，如命令行解析与 repo_id 匹配】
│
├── docs/guides/agent-runner.md
│   [修改]
│   【在 registry list / start / stop 章节补充 managed / unmanaged 状态说明与混用风险】
│
└── pyproject.toml
    [修改]
    【将 psutil 加入项目直接依赖，确保新环境可安装】
```

### Executor Drift Guard

- 搜索所有 `IRunnerProcessSupervisor` 的实现与 mock：
  ```bash
  rg "IRunnerProcessSupervisor|list_processes\(\)|def spawn\(" src/backend tests
  ```
  任何实现该接口的 fake / stub（尤其在测试中）都必须补全 `list_unmanaged_processes`，否则抽象方法会导致实例化失败。
- 搜索 `processes.json` 相关注释或文档，确认 unmanaged 概念没有与其他地方冲突：
  ```bash
  rg "unmanaged|manual.*daemon|手工.*daemon|前台.*daemon" docs src/backend tests
  ```
- 如果 `create_process_supervisor` 的签名改变，检查所有调用点：
  ```bash
  rg "create_process_supervisor\(" src/backend tests
  ```

### Flow Or Architecture Diagram

```mermaid
flowchart TD
    A[iar registry list] --> B[_run_registry_list_command]
    B --> C[create_registry_editor]
    B --> D[create_process_supervisor]
    C --> E[registry_entries<br/>from config.toml]
    D --> F[supervisor.list_processes<br/>from processes.json]
    D --> G[supervisor.list_unmanaged_processes<br/>scan OS processes via psutil]
    F --> H[Merge running records<br/>managed优先]
    G --> H
    H --> I[Rich Table Output]
    I --> J[daemon: running (p123) 或 running (unmanaged) 或 stopped]
```

### Realistic Validation Plan

| Behavior | Real Entry Point | Test Layer | Mock Boundary | Data/Env Needed | Command Or Procedure | Required For Acceptance |
|---|---|---|---|---|---|---|
| 未托管 daemon 被 list 检测 | `iar registry list` | integration (real CLI) | 不 mock OS 进程扫描 | `<repo_id>` enabled 且已 init；手动 daemon 在跑 | `iar daemon --repo-id keda-main &`；`iar registry list`；验证 `daemon` 列显示 `running (unmanaged)` | Yes |
| stop 不误杀未托管 daemon | `iar registry stop --repo-id keda-main` | integration (real CLI) | 不 mock | 同上 | `iar registry stop --repo-id keda-main`；`ps` 确认手动 daemon 仍存活；`registry list` 仍显示 `running (unmanaged)` | Yes |
| 托管与未托管共存时 managed 优先 | `iar registry start --repo-id keda-main` | integration (real CLI) | 不 mock | 手动 daemon 已在跑 | 先手动起 daemon，再 `iar registry start --repo-id keda-main`；`registry list` 显示 `running (<process_id>)` 而非 `running (unmanaged)` | Yes |
| 跨平台进程扫描正确性 | `pytest tests/test_process_supervisor.py` | unit | mock psutil Process | 无需真实 daemon | 运行新增单元测试 | Yes |
| 文档同步 | 阅读 `docs/guides/agent-runner.md` | manual | 不适用 | 不需要 | 检查新增 managed/unmanaged 说明存在且示例可执行 | Yes |

**Failure Triage Notes**：
- 若 `iar registry list` 始终不显示 `running (unmanaged)`：检查 `psutil` 是否可导入；检查扫描是否过滤了当前用户；检查命令行解析是否把 `review-daemon` 错当成 `review`。
- 若显示 `running (unmanaged)` 但实际是托管进程：检查 pid 去重逻辑是否把 `processes.json` 中的 pid 排除。
- 若 `registry stop` 报错 KeyError `unmanaged-xxx`：检查 stop 命令的过滤条件是否错误包含了未托管记录的 process_id。

### Low-Fidelity Prototype

不需要交互原型；输出仅为 CLI 表格状态变化。

### ER Diagram

No data model changes in this PRD.

### Interactive Prototype Change Log

No interactive prototype file changes in this PRD.

### External Validation

No external validation required; repository evidence was sufficient.

## 6. Definition Of Done

- [x] `IRunnerProcessSupervisor` 端口新增 `list_unmanaged_processes` 方法，所有实现与测试 stub 已同步。
- [x] `PidfileProcessSupervisor` 使用 `psutil` 扫描并返回未托管 `RunnerProcessRecord` 列表。
- [x] `_run_registry_list_command` 正确合并托管与未托管记录，managed 优先，未托管显示 `running (unmanaged)`。
- [x] `registry stop` 逻辑未改变，仍只处理托管记录。
- [x] 新增/更新单元测试覆盖合并逻辑与扫描辅助函数。
- [x] `just test` 全量通过，无回归。
- [x] `docs/guides/agent-runner.md` 已补充 managed/unmanaged 说明。
- [x] `pyproject.toml` 已加入 `psutil` 依赖。

## 7. Acceptance Checklist

### Architecture Acceptance

- [x] `IRunnerProcessSupervisor.list_unmanaged_processes` 定义在 `src/backend/core/shared/interfaces/runner_console.py`，实现仅在 `src/backend/infrastructure/console/process_supervisor.py`。
- [x] `api/cli_registry.py` 不直接导入 `psutil` 或进行 OS 进程枚举。
- [x] `registry stop` 的过滤条件仍基于 `processes.json` 中的托管记录；未托管 `process_id` 不被 stop 匹配。

### Dependency Acceptance

- [x] `pyproject.toml` 中已声明 `psutil` 为直接依赖。
- [x] `uv.lock` 或依赖锁定文件已更新（如项目使用 lockfile）。

### Behavior Acceptance

- [x] `iar registry list` 对仅有未托管 running 的仓库显示 `running (unmanaged)`。
- [x] `iar registry list` 对同时存在托管与未托管 running 的仓库显示托管状态（`running (<process_id>)`）。
- [x] `iar registry list` 对无 running 进程的仓库显示 `stopped`。
- [x] 未托管进程识别同时覆盖 `iar daemon` 与 `iar review-daemon`。
- [x] 无法解析 repo_id 的陌生 `iar daemon` 进程不被显示（避免误报）。

### Documentation Acceptance

- [x] `docs/guides/agent-runner.md` 的 `iar registry list` 章节说明输出中的 `running (unmanaged)` 含义。
- [x] 文档说明混用 `iar daemon` 与 `iar registry start` 的风险。

### Validation Acceptance

- [x] 通过真实 CLI 验证：手动 `iar daemon --repo-id keda-main` 后 `iar registry list` 显示 `running (unmanaged)`。
- [x] 通过真实 CLI 验证：`iar registry stop --repo-id keda-main` 后手动 daemon 仍存活且 list 仍显示 unmanaged。
- [x] 运行 `just test` 全量通过。

## 8. Functional Requirements

- **FR-1**：`IRunnerProcessSupervisor` 必须提供 `list_unmanaged_processes(registry_entries)` 方法，返回当前系统中属于该 registry 的未托管 running 进程记录。
- **FR-2**：`PidfileProcessSupervisor.list_unmanaged_processes` 必须使用 `psutil` 枚举当前用户拥有的进程，仅考虑命令行可识别为 `iar daemon` 或 `iar review-daemon` 的进程。
- **FR-3**：未托管进程识别必须先从命令行解析 `--repo-id`；若命令行未指定，再尝试用进程 cwd 匹配 `registry_entries` 中的 `path`。
- **FR-4**：已登记在 `processes.json` 中的 pid 必须从未托管扫描结果中排除，避免同一进程被重复报告。
- **FR-5**：`_run_registry_list_command` 合并托管与未托管记录时，对每个 `(repo_id, kind)` 组合必须优先使用托管 running 记录；仅当无托管 running 时才使用未托管记录。
- **FR-6**：未托管进程在表格中必须显示为 `running (unmanaged)`；托管进程继续显示为 `running (<process_id>)`。
- **FR-7**：`registry stop` 必须仅操作 `processes.json` 中的托管 running 记录；未托管记录不参与 stop 匹配。
- **FR-8**：`pyproject.toml` 必须将 `psutil` 声明为直接依赖。
- **FR-9**：`docs/guides/agent-runner.md` 必须更新以解释 managed / unmanaged daemon 的区别与推荐用法。

## 9. Non-Goals

- 不修改 `iar daemon` / `iar review-daemon` 命令本身，不要求它们自我登记到 `processes.json`。
- 不通过 `iar registry stop` 停止未托管进程；用户仍需手动 kill 手动启动的 daemon。
- 不实现未托管进程的日志续读（未托管进程没有专用 log 文件）。
- 不支持扫描其他用户拥有的进程（避免权限与隐私问题）。
- 不处理命令行无法识别 cwd 也无法匹配 registry 的 orphan 进程（避免误报）。

## 10. Risks And Follow-Ups

- **R-1（psutil 版本差异）**：`psutil` 在不同平台/版本上的字段名可能略有差异（如 `cmdline()` 返回 `None`）。实现中需做防御式处理。
  - 缓解：所有 `psutil` 访问都包裹 try/except；`cmdline` 为空或无法读取时跳过该进程。
- **R-2（性能影响）**：每次 `registry list` 都遍历全量进程可能变慢（尤其进程数很多的机器）。
  - 缓解：使用 `psutil.process_iter` 的缓存参数或一次性抓取；registry 条目数量通常很小，扫描开销可控。
- **R-3（重复 daemon 竞争仍存在）**：本 PRD 只解决"看不见"的问题，不阻止用户同时手动和托管启动两个 daemon。实际双开竞争仍需用户避免。
  - 缓解：文档明确说明风险；未来可考虑在 `registry start` 双开检测中也扫描系统进程。
- **R-4（cwd 匹配歧义）**：手动 daemon 的 cwd 可能不是 registry 记录路径（例如从 worktree 或子目录启动），导致无法识别。
  - 缓解：优先依赖 `--repo-id`；文档推荐手动启动时总是带 `--repo-id`。

## 11. Decision Log

| ID | Decision | Chosen | Rejected | Rationale |
|---|---|---|---|---|
| D-01 | 扫描能力放在哪一层 | 扩展 `IRunnerProcessSupervisor` 端口，由 `PidfileProcessSupervisor` 实现 | 在 `api/cli_registry.py` 直接调用 `psutil` | 系统进程扫描是基础设施能力，放在 `infrastructure/` 层符合四层依赖方向，且便于 console API 复用。 |
| D-02 | 是否让 `iar daemon` 自我登记 | 不自我登记，仅通过扫描观测 | `iar daemon --register` 选项 | 自我登记会让 CLI 入口直接依赖 infrastructure 层，破坏分层；且手动进程生命周期与 CLI 解耦，易产生脏记录。 |
| D-03 | 未托管进程显示格式 | 在现有列显示 `running (unmanaged)` | 新增独立列（如 `managed`） | 不新增列可保持表格紧凑，且与现有 `running (<process_id>)` 格式一致，用户容易理解。 |
| D-04 | 是否停止未托管进程 | `registry stop` 只处理托管记录 | stop 也 kill 未托管进程 | 未托管进程可能由用户主动前台运行或由外部调度器管理，擅自停止会造成意外中断。 |
| D-05 | 依赖选择 | 直接依赖 `psutil` | 调用系统 `ps` 命令 | `psutil` 跨平台且返回结构化数据，避免 macOS/Linux 上 `ps` 参数差异和 shell 注入风险。 |
| D-06 | 托管与未托管共存时的优先级 | 优先显示托管状态 | 同时显示两者或显示未托管 | 托管状态是用户可操作的来源，优先显示避免用户误以为 daemon 处于失控状态。 |
