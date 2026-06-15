# PRD: 本地 IAR 仓库扫描与 Registry 批量同步

## 1. Introduction & Goals

### Problem Statement

Agent Runner 的仓库 registry（`config.toml` 的 `[agent_runner.repositories.*]`）是 runner 识别目标仓库的唯一事实来源。当前存在两个明显痛点：

1. **前端「项目接入」页面只能手动添加仓库**：用户本地已经运行过 `iar init` 的多个仓库，必须一条条输入 `repo_id` 和路径才能注册到 registry。
2. **`/roadmap` 页面仓库选择是手动输入框**：默认硬编码 `keda-main`，如果 registry 里有其他仓库，用户无法方便地切换。

这导致本地有多个 IAR 仓库时，注册成本高、容易遗漏，且 roadmap 页面无法感知这些仓库。

### Proposed Solution Summary

- **Registry 扫描能力**：后端新增本地目录扫描器，自动发现带 `.iar.toml` 的 git 仓库，读取其中的 `repository.id` 与 `display_name`。
- **前端批量同步 UI**：在「项目接入」页面新增「扫描本地 IAR 仓库」区域，用户输入目录后列出候选仓库，勾选未注册仓库一键批量写入 `config.toml`。
- **CLI 同步命令**：新增 `iar registry scan` 与 `iar registry sync`，方便命令行用户快速注册本地仓库。
- **Roadmap 仓库下拉框**：将 roadmap 页面的仓库输入框改为下拉选择器，自动从 registry 加载已启用仓库。

### Measurable Objectives

- 用户可在「项目接入」页面扫描 `/Users/zata/code` 等目录，一次性勾选并同步多个 IAR 仓库到 registry。
- 同步后 `/roadmap` 页面仓库下拉框自动显示所有已注册仓库，默认优先 `keda-main` 或第一个已启用仓库。
- 用户可通过 `iar registry scan <dir>` 预览候选仓库，通过 `iar registry sync <dir>` 自动注册所有未注册仓库。
- 已有 registry 的仓库不会被重复注册；扫描深度限制在 4 层，避免误入深层依赖目录。

### Realistic Validation

- [x] **UT-1 API 发现与批量添加**
  - 入口：`GET /api/v1/agent-runner/repositories/discover` 与 `POST /api/v1/agent-runner/repositories/batch`
  - 步骤：创建包含 `.iar.toml` 与 `.git` 的临时目录结构，调用 discover 与 batch 端点。
  - 验证：discover 返回候选列表（含 `already_registered` 标记），batch 正确添加新仓库并跳过已注册仓库。
  - 证据：`tests/test_agent_runner_console_api.py::test_discover_iar_repositories_finds_local_repos`、`test_batch_add_repositories_skips_existing`。

- [x] **UT-2 CLI 解析与执行**
  - 入口：`iar registry scan/sync`
  - 步骤：构造临时 IAR 仓库与临时 `config.toml`，调用 `main(["registry", "scan", ...])` 与 `main(["registry", "sync", ...])`。
  - 验证：scan 打印候选，sync 写入 registry，dry-run 不写入，已注册仓库被跳过。
  - 证据：`tests/test_agent_runner_cli.py` 中新增的 6 个 registry 相关测试。

- [x] **UT-3 前端类型检查**
  - 入口：`frontend/src/pages/repositories-page.tsx`、`frontend/src/pages/roadmap-page.tsx`
  - 步骤：`npm run typecheck`
  - 验证：无 TypeScript 类型错误。

- [x] **RV-1 全量回归**
  - 步骤：`just test`
  - 验证：lint 与全部 886 个 pytest 用例通过。

## 2. Requirement Shape

- **Actor**：在本机使用 iar 管理多个仓库的 operator。
- **Trigger**：
  - operator 打开「项目接入」页面，想要批量导入本地 IAR 仓库。
  - operator 在 `/roadmap` 页面需要切换仓库。
  - operator 在终端想快速把一批本地仓库注册到 registry。
- **Expected Behavior**：
  - 扫描器只识别同时包含 `.git` 与 `.iar.toml` 的目录。
  - 候选仓库的 `repo_id` 优先从 `.iar.toml` 的 `[agent_runner.repository].id` 读取，缺失时按目录名规范化生成。
  - 已注册仓库在候选列表中标记为「已注册」且不可勾选/自动跳过。
  - 批量同步调用现有 registry editor 逐条写入 `config.toml`，保留文件注释与格式。
  - CLI `sync` 默认直接写入，支持 `--dry-run` 预览。
- **Explicit Scope Boundary**：
  - 只扫描并注册仓库，不修改 `.iar.toml` 内容。
  - 不自动启用/停用仓库，只添加新条目（默认 enabled）。
  - 扫描深度限制为 4 层，不递归整个文件系统。

## 3. Repository Context And Architecture Fit

### Current Relevant Modules And Files

| 路径 | 当前职责 | 与本 PRD 的关系 |
|---|---|---|
| `frontend/src/pages/repositories-page.tsx` | 仓库 registry 管理页面 | 新增扫描与批量同步 UI |
| `frontend/src/pages/roadmap-page.tsx` | 路线图页面 | 仓库选择改为下拉框 |
| `frontend/shared/api/console.ts` | console API wrapper | 新增 `discoverRepositories`、`batchAddRepositories` |
| `frontend/shared/api/types.ts` | 共享 DTO | 新增 `DiscoveredRepositoryEntry`、`BatchAddRepositoriesResult` |
| `src/backend/api/routes/agent_runner_console.py` | console 写 API | 新增 `GET /repositories/discover`、`POST /repositories/batch` |
| `src/backend/core/shared/interfaces/runner_console.py` | console 端口与模型 | 新增 `DiscoveredRepositoryEntry` |
| `src/backend/engines/agent_runner/repository_local.py` | 仓库本地配置 helper | 新增 `discover_iar_repositories` |
| `src/backend/engines/agent_runner/factory.py` | 对象装配 | 提供 `create_registry_editor` |
| `src/backend/infrastructure/config/registry_editor.py` | registry 写回实现 | 被复用 |
| `src/backend/api/cli_parser.py` | CLI 参数解析 | 新增 `registry scan/sync` 子命令 |
| `src/backend/api/cli_typer.py` | Typer 命令树 | 新增 `registry_app` 与命令 |
| `src/backend/api/cli.py` | CLI 执行后端 | 新增 registry 命令执行逻辑 |
| `tests/test_agent_runner_console_api.py` | console API 测试 | 新增发现与批量添加测试 |
| `tests/test_agent_runner_cli.py` | CLI 测试 | 新增 registry 命令测试 |

### Existing Architecture Pattern To Follow

```text
src/backend/api/ -> src/backend/core/ -> src/backend/engines/ -> src/backend/infrastructure/
```

- 扫描逻辑涉及读取 `.iar.toml`（infrastructure 配置），因此放在 `engines/agent_runner/repository_local.py`，避免 core 层反向依赖 infrastructure。
- API 路由只做 DTO 转换，具体扫描调用 `engines` 层函数。
- 前端只通过 `/api/v1/agent-runner/*` HTTP API 交互。

## 4. Recommendation

### Recommended Approach

1. **后端发现函数**：在 `engines/agent_runner/repository_local.py` 实现 `discover_iar_repositories(scan_root, editor)`，BFS 遍历 4 层目录，对同时含 `.git` 与 `.iar.toml` 的目录读取本地配置，返回候选列表并标记是否已注册。
2. **后端 API**：在 `routes/agent_runner_console.py` 新增：
   - `GET /repositories/discover?scan_root=...`
   - `POST /repositories/batch`（批量添加，已存在自动跳过）
3. **前端 UI**：在 `repositories-page.tsx` 新增扫描输入框、候选列表表格、复选框、全选与同步按钮。
4. **roadmap 下拉框**：在 `roadmap-page.tsx` 用 `fetchRegistryRepositories` 加载仓库列表，改为 `<select>`。
5. **CLI**：新增 `iar registry scan/sync` 两个子命令，复用同一发现函数。

### Alternatives Considered

| 方案 | 拒绝原因 |
|---|---|
| 自动扫描整个用户 home 目录 | 扫描范围不可控，可能误入深层目录；由用户指定根目录更明确 |
| 修改 `iar init` 自动注册 | 会改变全局配置，且多仓库场景下用户可能不想全部注册 |
| 在 core 层直接读取 `.iar.toml` | 违反架构方向，core 不能导入 infrastructure |
| 前端直接扫描本地文件系统 | 浏览器无此权限，必须通过后端 API |

## 5. Implementation Guide

### Core Logic

#### 发现函数 `discover_iar_repositories`

- 输入：`scan_root`（Path）、`editor`（`IRepositoryRegistryEditor`）
- 输出：`list[DiscoveredRepositoryEntry]`
- 行为：
  1. 解析并校验 `scan_root` 为绝对路径目录。
  2. BFS 遍历最多 4 层子目录。
  3. 对每个目录，若存在 `.git` 且 `.iar.toml` 是文件，则读取本地配置。
  4. `repo_id` 优先用 `.iar.toml` 中的 `repository.id`，否则用规范化目录名。
  5. `display_name` 优先用 `.iar.toml` 中的 `repository.display_name`，否则用目录名。
  6. 对比 registry 中已有路径，设置 `already_registered`。

#### 批量添加 API `POST /repositories/batch`

- 请求体：`{ repositories: [{ repo_id, path, display_name? }] }`
- 行为：逐条调用 `add_registry_repository`，已存在则加入 `skipped`，其他错误加入 `errors`。
- 返回：`{ added, skipped, errors }`。

#### CLI 命令

- `iar registry scan [scan_root]`：打印候选仓库，`already_registered` 标记为 `registered`，否则 `new`。
- `iar registry sync [scan_root] [--dry-run]`：自动注册所有未注册仓库；dry-run 只打印不写入。

### Change Impact Tree

```text
.
├── Frontend
│   ├── frontend/src/pages/repositories-page.tsx [修改]
│   ├── frontend/src/pages/roadmap-page.tsx [修改]
│   ├── frontend/shared/api/console.ts [修改]
│   └── frontend/shared/api/types.ts [修改]
├── API
│   ├── src/backend/api/routes/agent_runner_console.py [修改]
│   └── src/backend/api/app.py [未修改，已有注册]
├── Engines
│   ├── src/backend/engines/agent_runner/repository_local.py [修改]
│   └── src/backend/engines/agent_runner/factory.py [未修改，已有 create_registry_editor]
├── Core
│   └── src/backend/core/shared/interfaces/runner_console.py [修改]
├── CLI
│   ├── src/backend/api/cli_parser.py [修改]
│   ├── src/backend/api/cli_typer.py [修改]
│   └── src/backend/api/cli.py [修改]
└── Tests
    ├── tests/test_agent_runner_console_api.py [修改]
    └── tests/test_agent_runner_cli.py [修改]
```

## 6. Definition Of Done

- [x] 「项目接入」页面可扫描本地目录并批量同步 IAR 仓库到 registry。
- [x] `/roadmap` 页面仓库选择为下拉框，自动加载 registry 中已启用仓库。
- [x] CLI 支持 `iar registry scan` 与 `iar registry sync`。
- [x] 已注册仓库不会被重复添加；扫描深度限制 4 层。
- [x] `just test` 与 `npm run typecheck` 通过。
- [x] 四层架构依赖方向无违例。

## 7. Acceptance Checklist

### Architecture Acceptance

- [x] `discover_iar_repositories` 位于 `engines/` 层，未在 `core/` 直接导入 `infrastructure.config.settings`。
- [x] API 路由不直接操作文件系统或 config.toml，只通过 use case / engines 函数调用。
- [x] 前端只通过 `frontend/shared/api/*` HTTP wrapper 与后端交互。

### Behavior Acceptance

- [x] 扫描只识别同时含 `.git` 与 `.iar.toml` 的目录。
- [x] 候选列表正确标记 `already_registered`。
- [x] 批量同步跳过已注册仓库，新增仓库写入 `config.toml`。
- [x] CLI `sync --dry-run` 不修改 `config.toml`。
- [x] roadmap 页面默认选中 `keda-main`（若存在）或第一个已启用仓库。

### Validation Acceptance

- [x] `just test` 全量通过（886 tests）。
- [x] `npm run typecheck` 通过。
- [x] 新增 API 与 CLI 测试覆盖关键路径。

## 8. Functional Requirements

- **FR-1**：系统必须提供本地目录扫描能力，发现已初始化 IAR 的 git 仓库。
- **FR-2**：系统必须区分候选仓库是否已注册到 registry。
- **FR-3**：系统必须支持批量将候选仓库写入 `config.toml`，并跳过已注册项。
- **FR-4**：前端必须在「项目接入」页面提供扫描输入、候选列表、复选框与同步按钮。
- **FR-5**：前端必须在 `/roadmap` 页面提供 registry 仓库下拉选择器。
- **FR-6**：CLI 必须提供 `registry scan` 与 `registry sync` 命令。
- **FR-7**：扫描深度必须限制为 4 层，避免无差别递归。

## 9. Non-Goals

- 不修改 `.iar.toml` 内容。
- 不自动启用/停用已有 registry 条目。
- 不扫描非 git 目录或没有 `.iar.toml` 的仓库。
- 不做跨网络/远程仓库发现。

## 10. Risks And Follow-Ups

- **同名 repo_id 冲突**：不同目录可能生成相同 repo_id；当前实现保留第一个，后续可考虑提示用户手动处理。
- **扫描深度限制**：4 层可能漏掉某些组织较深的仓库；用户可指定更具体的子目录作为 `scan_root`。
- **已注册但路径不同**：当前按路径匹配判断 `already_registered`；同名不同路径的仓库会显示为新仓库，写入时会因 ID 冲突被跳过。

## 11. Decision Log

| ID | 决策问题 | Chosen | Rejected | Rationale |
|---|---|---|---|---|
| D-01 | 扫描实现位置 | `engines/agent_runner/repository_local.py` | `core/use_cases/` | 读取 `.iar.toml` 依赖 infrastructure 配置，放 engines 层符合架构方向 |
| D-02 | 扫描算法 | BFS，最大深度 4 | 无限制递归 | 避免扫入 `node_modules` 等深层目录 |
| D-03 | repo_id 来源 | `.iar.toml` 中的 `repository.id` 优先，否则目录名 | 总是目录名 | 尊重用户已有配置 |
| D-04 | 批量同步冲突处理 | 跳过已存在 | 覆盖或报错 | 安全优先，避免误改现有配置 |
| D-05 | CLI 默认行为 | `sync` 直接写入，`--dry-run` 预览 | 默认 dry-run | 命令行用户通常期望直接执行 |
