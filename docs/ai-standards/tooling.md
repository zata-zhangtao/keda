# Tooling Standards

## Preferred Tools

### Python

- 包管理：`uv`
- 任务入口：`just`
- 文档：`mkdocs`

优先顺序：

- 用 `uv` 代替 `pip` / `conda`
- 用 `just` 代替手工记忆零散命令

## Common Commands

| Command | Purpose |
|---|---|
| `just sync` | 同步开发依赖 |
| `just run` | 运行主应用（后端 + 前端）；若前端 `node_modules` 缺失会自动运行 `npm install` |
| `just run backend_port=8010 frontend_port=5178` | 使用指定端口运行主应用，并保存为当前 Git worktree 的默认端口 |
| `just run frontend` | 只启动前端（Vite，默认端口 5173） |
| `just frontend dev` | 在 `frontend/` 运行 `npm run dev` |
| `just down` | 按当前 Git worktree 保存的端口停止本地开发服务 |
| `just copy <new-dir>` | 派生新项目；随机分配两个互不重叠的端口（后端 8000-8999、前端 5180-5999）避免多副本端口冲突 |
| `just test` | 运行本地测试 |
| `uv run mkdocs build` | 验证文档站点 |
| `just docs-serve` | 本地预览文档 |

## Justfile Layering

仓库根目录下的 `just` 入口被拆分为两层，分别由模板上游和派生项目自己拥有：

- `justfile.shared`：模板上游维护的共享 recipe 集合，由 `just sync-template` 同步。所有项目无关的脚手架命令（`sync`、`lint`、`test`、`docs-serve`、`clean`、`release`、`check`、`codex-notify`、`staged_changes`、`worktree`、`implement`、`sync-template`、`e2e`、`e2e-install`、`export-env-encrypted` 以及内部 `_check-completion`）都在这里。**不要手改这个文件**——改了下次 `just sync-template` 会提示覆盖。
- `justfile`：项目私有入口，第一行启用 `set allow-duplicate-recipes` 后通过 `import 'justfile.shared'` 引入共享层，之后只保留与项目结构耦合、仅模板维护者使用，或会触达本机 AI 工具目录的 recipe：`default`、`run`、`down`、`frontend`、`ops`、`sync-local-skills`、`copy`。派生项目可以自由增删此文件中的 recipe，`just sync-template` 默认会跳过它。

行为约定：

- `import` 是 just 1.19+ 原生命名空间合并；私有 `justfile` 使用 `set allow-duplicate-recipes` 允许本地同名 recipe 覆盖共享版本。需要本地化某条共享命令时，直接在 `justfile` 中重写同名 recipe 即可，不要去改 `justfile.shared`。
- 裸 `just` 默认运行 `default` recipe；根 `justfile` 需要保留本地 `default`，并委托执行 `just --list`。
- `just sync-template` 的默认跳过名单（`scripts/shared/template/sync_template.sh` 的 `_is_skipped_by_default`）已包含 `justfile`；`justfile.shared` 仍按常规规则进入 NEW/CHANGED 候选清单。
- `just copy <dir>` 通过定位 `justfile` 中的 `# Copy template to a new directory` 段落标记，把 `copy` recipe 及其之后的内容从 destination 的 `justfile` 中 trim 掉；destination 仍保留 `set allow-duplicate-recipes`、`import 'justfile.shared'` 与 `default`/`run`/`down`/`frontend` 起始版本。该 marker 是 contract，调整 `copy` 上方注释时需保持 marker 文本不变。
- 从单文件旧版升级时，建议手工把本地 `justfile` 重写为最小私有版（`import 'justfile.shared'` + 项目特有 recipe），避免与 import 进来的共享 recipe 重复定义。

## Run Port State

`just run` 会通过 `git rev-parse --git-path vanta-run.env` 定位 Git 本地状态文件。该文件不进入版本控制；在主 worktree 中位于 `.git/vanta-run.env`，在 linked worktree 中位于对应 `.git/worktrees/<name>/vanta-run.env`。

- 未传端口且状态文件不存在时，默认使用后端 `8000`、前端 `5173`。
- 传入 `backend_port`、`frontend_port` 时，会保存本次端口配置。
- 后续 `just run` 和 `just down` 会复用保存的端口。
- 前端 Vite 使用 `strictPort`，端口被占用时直接失败，避免自动漂移后 `just down` 停错端口。
- `just copy <name>` 派生新项目时，会从两个互不重叠的区间随机分配端口（后端 `8000-8999`、前端 `5180-5999`），并写入 destination justfile 的 `run`/`down` 默认值以及 `.git/vanta-run.env`，让首次 `just run` 就走随机端口；所选端口会打印到 stdout。`just sync-template` 不会覆盖已随机化的 justfile，因此派生项目可以长期保留自己的端口。

## Automatic Frontend Dependency Install

`just run`（以及 `just run frontend`）在启动前端服务前会检查 `frontend/` 目录是否存在 `node_modules/`。若缺失且系统已安装 `npm`，则自动运行 `npm install` 安装依赖；若 `npm` 未安装，会给出明确提示并退出。这样新克隆仓库或清理依赖后首次运行无需手动执行安装步骤。

## Docker Local Run

`just run docker` **强制要求当前目录存在 `.env.local`**（缺失时直接报错退出），并按 `settings.py` 的方式分层加载环境：**先 `.env`、后 `.env.local` 覆盖**。

- 实际命令是 `docker compose --env-file .env --env-file .env.local up --build`（`.env` 不存在时自动省略）。多个 `--env-file` 后者优先，所以 `.env.local` 覆盖 `.env` 中的同名键，并让 compose 的 `${VAR}` 替换读到最终值。
- `docker-compose.yml` 中各服务的 `env_file` 同样按 `[.env, .env.local]` 顺序列出（均 `required: false`），把两份文件合并注入容器，`.env.local` 优先。这与 `settings.py` 的 `env_file=(.env, .env.local)` 完全一致。
- 不做任何地址改写——你连哪个数据库 / 存储，由你在 `.env.local` 中自行决定。
- `docker-compose.dokploy.yml` 不使用 `env_file`，部署环境变量仍由 Dokploy 平台注入。

## PRD Workflow Hooks

本仓库通过 `pre-commit` 调用项目本地 `hooks/shared/check_prd_acceptance_checklist.py` 维护 PRD 交付状态；PRD skill 另带 `scripts/check_prd_acceptance_checklist.py`，供 agent 按 skill 相对路径运行，或由其他仓库自行接入：

- `tasks/pending/` 下的 PRD 可以保留未完成验收项
- `tasks/` 根目录下的旧 active PRD 必须完成 `Acceptance Checklist`
- 新增、复制或重命名进入 `tasks/archive/` 的 PRD 也必须完成验收清单
- 已存在的历史 archive PRD 不会因为普通修改被重新套用新规则
- 验收清单标题支持英文 `Acceptance Checklist`、中文 `验收清单` 和双语标题

这条规则的目标是让“归档”代表交付完成，同时避免历史归档文档被新标准批量翻旧账。

当 agent 已加载 PRD skill 时，也可以从 skill 目录运行：

```bash
python scripts/check_prd_acceptance_checklist.py --repo-root "$PWD" --all
```

## Platform Notes

- Windows 下优先使用 PowerShell 语法
- 文本文件读写显式指定 UTF-8

## Playwright Exception

`tests/playwright-e2e/` 是 Node/TypeScript 包：

- 使用 `npm`
- 不使用 `uv`
- 运行方式遵循该目录自己的 `README.md`

## Lint Modes

`just lint` 是默认开发反馈命令，等价于：

```bash
uv run pre-commit run --show-diff-on-failure
```

它使用同一份 `.pre-commit-config.yaml`，并按 pre-commit 默认 staged-files 语义运行；不会默认传入 `--all-files`，不会运行 manual 重复检测 hooks，也不会写入 `.last_linted_commit`。

完整模式：

- `just lint --full`：运行 `uv run pre-commit run --all-files --show-diff-on-failure`，通过后写入 `.last_linted_commit`；manual 重复检测 hooks 不属于该模式。
- `just lint --reuse`：显式运行 manual 重复检测 hooks：`jscpd`、`pylint-duplicate-code`，并补跑 `check-architecture`、`check-guidelines-consistency`、`check-max-file-lines`。
- `just lint --repo`：本地交付前总入口，串行执行 `just lint --full`、`just lint --reuse`、`just test`、`uv run mkdocs build --strict`，并在最后重新确认 full lint/test 标记。

推荐顺序：

1. 本地迭代和提交前频繁运行 `just lint`。
2. 修改复用边界、架构规则、AI 规范入口或疑似重复逻辑时运行 `just lint --reuse`。
3. 交付、PRD 归档或合并前运行 `just lint --repo`；如果时间受限，至少运行 `just lint --full` 和相关测试。

## Quality Check Flags

`just lint --full` 和 `just test` 会把通过状态写入 Git 目录下的本地标记文件：

- `.last_linted_commit`：绑定当前分支、HEAD 和 lint 有效 tree；未变更时，后续 `just lint --full` 走快速路径。
- `.last_tested_commit`：绑定当前分支、HEAD 和 test 有效 tree；提交前由 `check-test-flag` 校验，并在 `just test` 入口用于判断是否需要重新跑测试。
- 对于刚 `git init`、尚无首个 commit 的仓库，flag 会绑定当前分支、`no-commit` 和对应有效 tree，因此模板仓库复制后可在首次提交前正常运行 `just test` / `just lint --full` / `git commit` 流程。

`just test` 在入口处会先检查 `.last_tested_commit`：若分支、HEAD 和 test 有效 tree 均未变化，则直接打印提示并退出，避免重复 lint 与 pytest。只有在 flag 无效或不存在时，才会执行 `SKIP=check-test-flag just lint --full` 并运行测试。测试成功后会同时刷新 test 标记和 full lint 标记，避免刚跑完 `just test` 后再次执行完整 full lint。

`just lint --full` 的快速路径仍会执行轻量的 `check-test-flag`，除非调用方显式设置 `SKIP=check-test-flag`。如果 `SKIP` 跳过了除 `check-test-flag` 以外的 hook，本次 full lint 不会写入 `.last_linted_commit`。

当 Git index 中存在新增、复制或重命名进入 `tasks/archive/` 的 PRD 时，`just lint --full` 不使用快速路径，而是强制运行完整 `pre-commit`。这是因为 archive PRD 验收检查依赖 staged 状态，需要和提交阶段保持一致。

## Duplicate Detection Hooks

重复检测 hooks 被设置为 `manual` stage，不会在默认 `git commit`、`just lint` 或 `just lint --full` 中运行；需要通过 `just lint --reuse`、`just lint --repo` 或 CI/CD 的显式 reuse diagnostics 步骤运行：

- `jscpd`：跨 Python / TypeScript / JavaScript 的复制粘贴检测，版本由 `.pre-commit-config.yaml` 的 `additional_dependencies` 固定
- `pylint duplicate-code`：Python 结构级重复检测，版本由 `pyproject.toml` 与 `uv.lock` 固定

重复检测区分"候选文件"和"比较语料"：候选文件来自当前变更；`jscpd` 比较 `src/backend/` 与 `frontend/`，`pylint duplicate-code` 比较 `src/backend/`。这样可以阻断新增重复，同时避免历史重复让干净工作区的 `just lint` 永久失败。

常用调试命令：

| Command | Purpose |
|---|---|
| `just lint --reuse` | 运行所有复用、架构和规范一致性诊断 |
| `uv run pre-commit run jscpd --hook-stage manual --all-files` | 验证 jscpd hook 可执行 |
| `uv run pre-commit run pylint-duplicate-code --hook-stage manual --all-files` | 验证 pylint duplicate-code hook 可执行 |

若需要对干净工作区中的指定历史文件强制运行重复扫描，可临时设置 `DUPLICATION_CHECK_FORCE=1`。

误报处理优先级：

1. 复用已有函数或提取公共规则
2. 如果是合法相似 DTO / schema，缩小候选变更或在代码审查中说明
3. 不为绕过检测而复制逻辑或降低全局阈值

## PRD Workflow Hooks

本仓库通过 `pre-commit` 调用项目本地 `hooks/shared/check_prd_acceptance_checklist.py` 维护 PRD 交付状态；PRD skill 另带 `scripts/check_prd_acceptance_checklist.py`，供 agent 按 skill 相对路径运行，或由其他仓库自行接入：

- `tasks/pending/` 下的 PRD 可以保留未完成验收项
- `tasks/` 根目录下的旧 active PRD 必须完成 `Acceptance Checklist`
- 新增、复制或重命名进入 `tasks/archive/` 的 PRD 也必须完成验收清单
- 已存在的历史 archive PRD 不会因为普通修改被重新套用新规则
- 验收清单标题支持英文 `Acceptance Checklist`、中文 `验收清单` 和双语标题

这条规则的目标是让"归档"代表交付完成，同时避免历史归档文档被新标准批量翻旧账。

当 agent 已加载 PRD skill 时，也可以从 skill 目录运行：

```bash
python scripts/check_prd_acceptance_checklist.py --repo-root "$PWD" --all
```

**PRD 归档动作**：实现完成后，将对应 PRD 从 `tasks/pending/` 移动到 `tasks/archive/`，并确保验收清单已全部完成。

## Codex macOS 通知

macOS 用户可以把 Codex CLI 的 `notify` 事件转发到系统快捷指令：

```bash
just codex-notify install codex通知
just codex-notify test codex通知
```

该能力会把顶层 `notify` 写入 `~/.codex/config.toml`，并在 Codex 完成 `agent-turn-complete` 事件时调用 macOS 快捷指令，快捷指令输入中包含当前任务仓库名和当前 Git 分支。详细步骤见 `docs/guides/codex-notifications.md`。
