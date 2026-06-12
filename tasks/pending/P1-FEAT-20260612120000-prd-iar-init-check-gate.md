# PRD: iar 命令仓库初始化门禁

## 1. 背景与目标

当前除 `iar init` 外的所有 `iar` 命令在目标仓库缺少 `.iar.toml` 时都会静默降级运行：使用 `config.toml` 全局默认值、用目录名作为 `repo_id`、使用 `origin` 作为默认 remote 等。这会导致标签同步到错误仓库、Issue 创建到错误项目、runner 推送到错误 remote 等难以排查的问题。

本需求目标是为所有 `iar` 命令增加一道初始化门禁：在命令真正执行业务逻辑前，强制确认目标仓库已经执行过 `iar init`（即存在有效的 `.iar.toml`）。未初始化时直接失败，并明确告诉用户先运行 `iar init`。

### 真实测试 Checklist

本 PRD 的实现不能只依赖 mock 层单元测试，必须至少覆盖以下真实入口或真实运行边界：

- [ ] 在已 `iar init` 的仓库中运行 `uv run iar labels sync --dry-run` 成功。
- [ ] 在未 `iar init` 的临时 Git 仓库中运行 `uv run iar labels sync --dry-run` 失败并提示 `iar init`。
- [ ] 在未 `iar init` 的临时 Git 仓库中运行 `uv run iar run --dry-run` 失败并提示 `iar init`。
- [ ] 在未 `iar init` 的临时 Git 仓库中运行 `uv run iar issue create tasks/pending/test.md --dry-run` 失败并提示 `iar init`。
- [ ] 在未 `iar init` 的临时 Git 仓库中运行 `uv run iar worktree create --branch x --base-branch main` 失败并提示 `iar init`。
- [ ] `iar init` 自身在任何 Git 仓库中都能正常运行，不被门禁拦截。
- [ ] 实现完成后必须运行 `just test`，作为本 PRD 完成前的最终回归门禁。

## 2. 需求形态

- **Actor**: 在目标仓库或 keda 仓库中运行 `iar` CLI 的开发者或 operator。
- **Trigger**: 执行任何 `iar` 子命令，除了 `iar init`。
- **Expected behavior**:
  - 命令参数解析完成后、业务逻辑执行前，检查目标仓库是否存在有效的 `.iar.toml`。
  - 若目标仓库未初始化，立即以非零退出码失败，并打印清晰错误：仓库路径、缺失的配置文件名、建议执行的 `iar init` 命令。
  - 若目标仓库已初始化，命令继续按现有逻辑执行，行为不变。
- **Explicit scope boundary**:
  - 本 PRD 只增加初始化状态检查与错误提示，不改变 `.iar.toml` 格式、配置合并逻辑、use case 行为或 GitHub 交互逻辑。
  - 本 PRD 不提供 `--skip-init-check` 等绕过开关；仓库必须先初始化才能使用 iar。
  - `iar init` 完全豁免，包括 `--dry-run` 和 `--force` 形式。

## 3. 仓库上下文与架构适配

当前相关模块和文件：

- `src/backend/api/cli.py` 负责解析 CLI 命令并分发 use case，是增加统一门禁的最合适位置。
- `src/backend/api/cli_parser.py` 与 `src/backend/api/cli_typer.py` 负责 CLI 参数定义，需要同步理解哪些命令需要检查（除 `init` 外全部）。
- `src/backend/engines/agent_runner/repository_local.py` 提供 `detect_git_repository_root` 与 `.iar.toml` 文件写入/渲染逻辑。
- `src/backend/infrastructure/config/settings.py` 定义 `IAR_REPOSITORY_CONFIG_FILENAME` 与 `load_agent_runner_local_settings`，可复用本地配置加载逻辑来判断初始化状态。
- `src/backend/engines/agent_runner/factory.py` 提供 `resolve_repository_targets` 与 `resolve_issue_from_prd_target`，负责把 CLI selector 解析为 `RepositoryRunContext`。
- `tests/test_agent_runner_cli.py`、`tests/test_worktree_cli.py`、`tests/test_agent_runner_config.py` 覆盖 CLI dispatch 与仓库解析，需要新增/更新用例。
- `docs/guides/agent-runner.md` 与 `README.md` 需要更新，说明 `iar init` 是前置步骤。

需要遵循的既有架构模式：

- CLI 参数解析与命令分发留在 `src/backend/api/cli.py`。
- 物理文件读取与 TOML 解析不进入 `core/`，复用 `infrastructure/config/settings.py` 的现有加载函数。
- Git 仓库根目录探测复用 `engines/agent_runner/repository_local.py` 的 `detect_git_repository_root`。
- 错误信息通过 `error_console` 输出，CLI 返回非零退出码。

运行时、文档、测试和工作流约束：

- Python 项目优先使用 `uv` 和 `just`。
- 实现完成后必须运行 `just test`。
- 公共 Python API 需要 Google Style docstrings。
- CLI 行为变化必须同步更新文档。

## 4. 推荐方案

### Recommended Approach

在 `src/backend/api/cli.py` 的命令主入口增加统一的仓库初始化检查。具体实现：

1. **初始化判断函数**

   在 `src/backend/engines/agent_runner/repository_local.py` 中新增：

   ```python
   class IARRepositoryNotInitializedError(Exception):
       """Raised when a target repository has not run `iar init`."""

   def require_iar_repository_initialized(
       repo_root_path: Path,
       process_runner: SubprocessRunner | None = None,
   ) -> None:
       """Raise if the repository lacks a valid .iar.toml.

       A valid local config means:
       - `.iar.toml` exists as a regular file.
       - It is parseable TOML.
       - It contains an `[agent_runner]` section.
       - `repository.id` is non-empty.
       """
   ```

   该函数复用 `load_agent_runner_local_settings` 的加载逻辑；加载失败或 `repository.id` 为空时抛出 `IARRepositoryNotInitializedError`，包含仓库根路径与 `.iar.toml` 路径。

2. **CLI 入口统一检查**

   在 `src/backend/api/cli.py` 的 `_run_parsed_command` 中：

   - `if parsed.command == "init"` 分支保持原有逻辑，不触发检查。
   - 其余所有分支（`labels`、`issue create`、`run`、`daemon`、`review`、`review-daemon`、`recover`、`blocked-continue`、`ask`、`deliberate`、`worktree` 等）在执行具体业务前，先解析出目标仓库根路径并调用 `require_iar_repository_initialized`。

   目标仓库根路径的获取策略：

   - `worktree` 命令：通过 `detect_git_repository_root(Path.cwd(), process_runner)` 取得当前 Git 仓库根。
   - 其他命令：复用 `resolve_repository_targets` 的 selector 解析能力，取得返回的每个 `RepositoryRunContext.repo_path`，对每个目标仓库执行检查。任一仓库未初始化即失败。
   - 为 `--repo-id` 或 `--all` 等多仓库场景，检查应覆盖所有最终选中的仓库，而不是仅检查当前工作目录。

3. **错误输出与退出码**

   检查失败时，CLI 打印类似：

   ```
   [red]Repository is not initialized for iar.[/]
   Expected local config: /path/to/repo/.iar.toml
   Run the following command from the repository root:
     iar init
   ```

   然后返回 `1`。

4. **测试策略**

   - 单元测试：在 `tests/test_agent_runner_cli.py` 中新增 mock 测试，验证非 `init` 命令在缺少 `.iar.toml` 时返回 `1` 且错误信息包含 `iar init`。
   - 单元测试：验证 `require_iar_repository_initialized` 对存在/缺失/无效 `.iar.toml` 的处理。
   - 真实入口测试：在临时 Git 仓库中分别运行 `uv run iar init` 和若干非 init 命令，验证门禁生效。

为什么该方案最适合当前架构：

- 统一在 CLI 入口检查，避免在每个 use case 或 factory 函数中重复加锁。
- 复用已有的本地配置加载与 Git 根目录探测能力，改动面最小。
- 检查逻辑与配置解析逻辑解耦：即使未来 `.iar.toml` 格式扩展，也只需修改一处判断标准。

## 5. 方案对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| **CLI 入口统一检查（推荐）** | 职责清晰；不侵入 use case 和 factory；便于测试和后续调整 | 需要在 `cli.py` 中为每个命令明确解析目标仓库路径 |
| 在 `resolve_repository_targets` 内部拦截 | 所有仓库目标解析自然经过此处 | 会改变现有返回 `None` 降级行为，可能影响 console/monitor 等只读场景；错误信息不够直观 |
| 在每个 use case 开头检查 | 最小化改动入口 | 大量重复代码，遗漏风险高 |

## 6. 验收标准

- [ ] 除 `iar init` 外，任何 `iar` 命令在未 `iar init` 的目标仓库中均返回非零退出码，并明确提示运行 `iar init`。
- [ ] `iar init` 自身不触发初始化检查，且行为与当前一致。
- [ ] 已 `iar init` 仓库中所有命令行为与当前一致，无回归。
- [ ] `--repo-id` 与 `--all` 等多仓库 selector 覆盖的所有目标仓库均接受检查，任一未初始化即失败。
- [ ] `iar worktree create/path/remove/cleanup` 同样接受初始化检查。
- [ ] 新增/更新的单元测试覆盖存在、缺失、无效 `.iar.toml` 三种情况。
- [ ] 实现完成后 `just test` 通过。
- [ ] `docs/guides/agent-runner.md` 与 `README.md` 已更新，明确 `iar init` 是前置步骤。
