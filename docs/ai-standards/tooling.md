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
| `just run` | 运行主应用 |
| `just test` | 运行本地测试 |
| `uv run mkdocs build` | 验证文档站点 |
| `just docs-serve` | 本地预览文档 |

## PRD Workflow Hooks

本仓库通过 `pre-commit` 维护 PRD 交付状态：

- `tasks/pending/` 下的 PRD 可以保留未完成验收项
- `tasks/` 根目录下的 active PRD 必须完成 `Acceptance Checklist`
- 新增、复制或重命名进入 `tasks/archive/` 的 PRD 也必须完成验收清单
- 已存在的历史 archive PRD 不会因为普通修改被重新套用新规则
- PRD 文件名支持旧格式 `*-prd-*.md` 和优先级格式 `P0/P1/P2/P3-<TYPE>-YYYYMMDD-HHMMSS-<slug>.md`
- 验收清单标题支持英文 `Acceptance Checklist`、中文 `验收清单` 和双语标题

这条规则的目标是让“归档”代表交付完成，同时避免历史归档文档被新标准批量翻旧账。

### Pre-commit Hook 与 Runner Gate 的职责边界

PRD 交付状态由三层机制共同维护，彼此不互相替代：

1. **Prompt 引导**：Agent Runner 的 `build_prompt()` 明确要求 Agent 在完成任务前更新 PRD 的 `Acceptance Checklist` 并归档 PRD。这是行为引导，依赖 Agent 自觉执行。
2. **Runner PRD Delivery Gate**：在 `publish_changes()` 之前，runner 对 PRD-backed Issue 做机器级校验。若 pending PRD 的 checklist 已全部完成，runner 自动执行 `git mv` 将其归档；若仍有未完成项，则阻断发布并进入 recovery loop。这是成功路径的交付门禁。
3. **Pre-commit Hook**：`hooks/check_prd_acceptance_checklist.py` 在本地提交时检查 root-level active PRD 和**新进入** `tasks/archive/` 的 PRD。hook 没有 Issue 上下文，因此不扫描 `tasks/pending/` 中尚未执行的 backlog PRD，避免误伤。

**关键区别**：
- Runner gate 只在 Issue 执行成功路径时检查**该 Issue 对应的 canonical PRD**，并可在 checklist 完成时自动移动文件。
- Pre-commit hook 是通用本地提交防线，检查所有 staged 的 active/archive PRD，但不处理 pending PRD。
- `archive_tasks.py` 的 root-level active PRD 自动归档职责保持不变，不扩大到 `tasks/pending/`。

### Codex macOS 通知

macOS 用户可以把 Codex CLI 的 `notify` 事件转发到系统快捷指令：

```bash
just codex-notify install codex通知
just codex-notify test codex通知
```

该能力会把顶层 `notify` 写入 `~/.codex/config.toml`，并在 Codex 完成 `agent-turn-complete` 事件时调用 macOS 快捷指令，快捷指令输入中包含当前任务仓库名和当前 Git 分支。详细步骤见 `docs/guides/codex-notifications.md`。

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
- `.last_tested_commit`：绑定当前分支、HEAD 和 test 有效 tree；提交前由 `check-test-flag` 校验。

`just test` 在运行测试前会先执行 `SKIP=check-test-flag just lint --full`。因此测试成功后会同时刷新 test 标记和 full lint 标记，避免刚跑完 `just test` 后再次执行完整 full lint。

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

本仓库通过 `pre-commit` 维护 PRD 交付状态：

- `tasks/pending/` 下的 PRD 可以保留未完成验收项
- `tasks/` 根目录下的 active PRD 必须完成 `Acceptance Checklist`
- 新增、复制或重命名进入 `tasks/archive/` 的 PRD 也必须完成验收清单
- 已存在的历史 archive PRD 不会因为普通修改被重新套用新规则
- PRD 文件名支持旧格式 `*-prd-*.md` 和优先级格式 `P0/P1/P2/P3-<TYPE>-YYYYMMDD-HHMMSS-<slug>.md`
- 验收清单标题支持英文 `Acceptance Checklist`、中文 `验收清单` 和双语标题

这条规则的目标是让"归档"代表交付完成，同时避免历史归档文档被新标准批量翻旧账。

**PRD 归档动作**：实现完成后，将对应 PRD 从 `tasks/pending/` 移动到 `tasks/archive/`，并确保验收清单已全部完成。

## Codex macOS 通知

macOS 用户可以把 Codex CLI 的 `notify` 事件转发到系统快捷指令：

```bash
just codex-notify install codex通知
just codex-notify test codex通知
```

该能力会把顶层 `notify` 写入 `~/.codex/config.toml`，并在 Codex 完成 `agent-turn-complete` 事件时调用 macOS 快捷指令，快捷指令输入中包含当前任务仓库名和当前 Git 分支。详细步骤见 `docs/guides/codex-notifications.md`。
