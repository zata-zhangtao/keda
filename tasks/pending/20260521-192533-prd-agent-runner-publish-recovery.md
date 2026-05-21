# PRD: Agent Runner Publish Failure Recovery & Resume

## 1. Introduction & Goals

`iar run-once` 当前把 Agent 执行、runner 提交、Git push、PR 创建和 label 收尾串在同一次流程里。实际故障中，Agent 已经完成代码修改、runner 已经生成本地 commit、验证也通过，但最后 `git push` 因配置 remote 不存在而失败，Issue 被标记为 `agent/failed`。

同类发布阶段失败还包括网络中断、GitHub CLI 认证过期、GitHub API 临时错误、rate limit、`git push` 连接失败、`gh pr create` 超时或 PR 查询失败。这些失败的共同点是：代码成果已经在本地 commit 中，恢复应重试发布收尾，而不是重跑 Agent。

这类失败不应该重新启动 Agent。正确恢复方式是复用已有 worktree 和本地 commit，幂等完成发布收尾：push 分支、创建或复用 draft PR、把 Issue 从 `agent/failed` 切到 `agent/review`。

本 PRD 的目标：

- 在 `run-once` 领取 Issue 前发现明显的发布配置错误，避免 Agent 做完后才失败。
- 为“已有本地 commit，但发布阶段失败”的任务提供显式恢复命令。
- 恢复命令必须幂等、安全，不重复创建 PR，不误推 base branch，不处理未提交脏变更。

## 2. Requirement Shape

- **Actor**：本地操作者或自动化 runner 运维者。
- **Trigger**：
  - `iar run-once` 即将领取 ready Issue。
  - `iar run-once` 在 `publish_changes` 阶段失败。
  - `git push`、PR 查询、PR 创建、Issue label 更新或 Issue comment 创建因为网络/API/认证类错误失败。
  - 用户显式执行 `iar recover-publish --issue <number>`。
- **Expected Behavior**：
  - `run-once` 非 dry-run 时先做 publish preflight，配置 remote 不存在时直接失败，不领取 Issue。
  - publish 阶段失败时，Issue comment 明确说明本地 commit 已存在但发布收尾失败，并给出恢复命令；错误摘要保留失败命令、exit code、stdout/stderr 或异常文本。
  - `recover-publish` 只恢复发布收尾，不启动 Agent，不运行 recovery prompt，不要求新 commit。
  - `recover-publish` 成功后，远程分支存在，draft PR 存在或被复用，Issue label 进入 review 状态。
- **Scope Boundary**：
  - 不处理 Agent 代码修复、测试失败、无 commit、未提交变更等执行阶段问题；这些属于 surgical failure recovery。
  - 不自动选择非配置 remote；配置 remote 不存在必须失败并提示用户修配置。
  - 不引入数据库、状态文件或后台队列；恢复基于 Git worktree、GitHub API 和现有配置。

## 3. Repository Context And Architecture Fit

### Existing Path

| 路径 | 当前职责 | 与本 PRD 的关系 |
|---|---|---|
| `src/backend/api/cli.py` | `iar` CLI 参数解析与 use case 调用 | 新增 `recover-publish` 子命令 |
| `src/backend/core/use_cases/run_agent_once.py` | Issue 领取、Agent 执行、验证、提交代理、发布 PR | 复用 `get_current_branch`, `get_head_sha`, `has_changes`, `publish_changes` 的部分逻辑；补强 publish failure comment |
| `src/backend/core/shared/interfaces/agent_runner.py` | GitHub 与进程端口 | 需要扩展 PR 查询能力，避免 core 直接硬编码 `gh pr list` |
| `src/backend/core/shared/models/agent_runner.py` | core 层配置与值对象 | 新增 PR summary / publish recovery request-result 模型 |
| `src/backend/infrastructure/github_client.py` | GitHub CLI 适配器 | 实现按 head branch 查询 open PR |
| `tests/test_run_agent.py` | runner 编排行为测试 | 覆盖 preflight 与 publish failure comment |
| `tests/test_agent_runner_cli.py` | CLI parser 测试 | 覆盖 `recover-publish` 参数 |

### Architecture Constraints

- CLI 层只解析参数和装配依赖，不写恢复业务规则。
- 发布恢复编排属于 core use case，建议新增 `src/backend/core/use_cases/recover_publish.py`。
- core 层不得直接导入 `infrastructure`，PR 查询必须通过 `IGitHubClient`。
- Git 命令仍通过 `IProcessRunner` 执行，保持测试可替换。
- `run-once` 与 `recover-publish` 应复用相同的 remote 校验与 PR 创建逻辑，避免并行实现。

### Reuse Candidates

- `format_command(config.worktree.path_command, issue_number=...)`：只解析预期 worktree 路径，不创建新 worktree。
- `get_current_branch`, `get_head_sha`, `has_changes`, `validate_publish_remote`：作为恢复前安全检查。
- `github_client.create_draft_pr(...)`：PR 不存在时继续使用现有 draft PR 创建入口。
- `github_client.edit_issue_labels(...)` 与 `comment_issue(...)`：完成 label 收尾和结果记录。

### Potential Redundancy Risks

- 不应新增第二套 GitHub CLI wrapper；只扩展现有 `GitHubCliClient`。
- 不应新增“恢复状态文件”；状态可以从 Git 分支、PR 列表和 Issue labels 推导。
- 不应让 `recover-publish` 调用 `create_or_reuse_worktree`，因为该函数可能创建 worktree；恢复命令必须只处理已经存在的工作成果。

## 4. Recommendation

### Recommended Approach：新增 `recover-publish` 用例 + 发布前 preflight + 幂等 PR 复用

1. 在 core 层新增 `recover_publish_issue(...)`：
   - 根据 `config.worktree.path_command` 解析 issue worktree。
   - 校验 worktree 存在且是 Git worktree。
   - 校验 worktree clean，拒绝未提交变更。
   - 校验当前 branch 安全：不能为空、不能等于 base branch、默认必须匹配 issue number；不匹配时要求显式 `--branch`。
   - 校验 `[agent_runner.git].remote` 存在。
   - push 当前 branch 到配置 remote。
   - 查询 head branch 是否已有 open PR；有则复用，没有则创建 draft PR。
   - 将 Issue label 从 `agent/failed` / `agent/running` / `agent/ready` 切到 `agent/review`。
   - 写 Issue comment，记录 branch、HEAD、PR URL、是否复用已有 PR。

2. 在 CLI 层新增：

   ```bash
   uv run iar recover-publish --issue 5
   uv run iar recover-publish --issue 5 --branch issue-5
   ```

3. `run-once` publish 阶段失败时：
   - 不再只输出原始异常。
   - comment 中加入“本地 commit 已存在，发布失败”的诊断、失败命令摘要和 `iar recover-publish --issue <number>` 命令。
   - 保持 Issue 为 `agent/failed`，由恢复命令成功后切到 review。

### Why This Fits

- 只新增一个明确 use case，不改变 Agent retry loop。
- 用现有 worktree、GitHub client、process runner 端口完成恢复。
- 幂等性来自 GitHub PR 查询和 label set 操作，不需要外部状态。
- 发布恢复与代码修复分离，避免 push 失败时错误地重启 Agent。

### Alternatives Considered

| 方案 | 说明 | 拒绝原因 |
|---|---|---|
| 把发布失败纳入 Agent recovery loop | push 失败后重新启动 Agent，让它继续处理 | Agent 被明确禁止 push/建 PR；且代码已完成，重启 Agent 可能产生无关改动 |
| `run-once` 自动在失败后立即重试 push 多次 | 对所有 publish failure 做内置重试 | remote 配置错误、权限错误不是短暂问题；自动重试会浪费时间且仍无恢复入口 |
| 只要求用户手动执行 git push / gh pr create | 文档化人工恢复步骤 | 可行但不可重复、容易漏 label/comment 收尾，且不适合 daemon 场景 |
| 新增状态文件记录 publish checkpoint | 在 `.agent-runner/` 写 publish 状态 | 额外状态会过期；当前状态可从 Git 和 GitHub 查询得到 |

## 5. Implementation Guide

### Core Logic

```text
recover_publish_issue(request, config, github_client, process_runner):
  worktree_path = resolve_existing_issue_worktree(repo_path, issue_number, config)
  ensure worktree_path exists
  ensure has_changes(worktree_path) == False

  branch = get_current_branch(worktree_path)
  ensure branch != config.git.base_branch
  ensure branch matches issue number, unless request.expected_branch is provided
  if expected_branch provided, ensure branch == expected_branch

  head_sha = get_head_sha(worktree_path)
  validate_publish_remote(worktree_path, config)
  git push -u <configured_remote> <branch>

  existing_pr = github_client.find_open_pr_by_head(branch, cwd=worktree_path)
  if existing_pr:
      pr_url = existing_pr.url
      reused_pr = True
  else:
      pr_url = github_client.create_draft_pr(...)
      reused_pr = False

  github_client.edit_issue_labels(
      issue_number,
      add=[config.labels.review],
      remove=[config.labels.failed, config.labels.running, config.labels.ready],
  )
  github_client.comment_issue(issue_number, publish recovery summary)
  return PublishRecoveryResult(...)
```

### Change Impact Tree

```text
.
├── src/backend/api/
│   └── cli.py
│       [修改] 新增 recover-publish 子命令与参数解析
├── src/backend/core/shared/
│   ├── interfaces/agent_runner.py
│   │   [修改] IGitHubClient 新增 find_open_pr_by_head(...)
│   └── models/agent_runner.py
│       [修改] 新增 PullRequestSummary, PublishRecoveryRequest, PublishRecoveryResult
├── src/backend/core/use_cases/
│   ├── run_agent_once.py
│   │   [修改] publish failure comment 增加恢复命令与本地 commit 说明
│   └── recover_publish.py
│       [新增] 发布恢复编排、worktree 解析、安全校验、幂等 PR 复用
├── src/backend/infrastructure/
│   └── github_client.py
│       [修改] 通过 gh pr list 实现 find_open_pr_by_head(...)
├── tests/
│   ├── test_agent_runner_cli.py
│   │   [修改] 覆盖 recover-publish CLI parser
│   ├── test_recover_publish.py
│   │   [新增] 覆盖恢复成功、复用 PR、安全拒绝、label 收尾
│   └── conftest.py
│       [修改] FakeGitHubClient 支持 PR 查询
└── docs/
    └── guides/agent-runner.md
        [修改] 增加发布失败恢复说明
```

### Flow Or Architecture Diagram

```mermaid
flowchart TD
    A["iar recover-publish --issue N"] --> B["CLI parses request"]
    B --> C["core: resolve existing worktree"]
    C --> D{"worktree exists and clean?"}
    D -->|No| E["fail with actionable message"]
    D -->|Yes| F{"branch safe for issue?"}
    F -->|No| E
    F -->|Yes| G{"configured remote exists?"}
    G -->|No| E
    G -->|Yes| H["git push -u remote branch"]
    H --> I{"open PR for head branch exists?"}
    I -->|Yes| J["reuse PR"]
    I -->|No| K["create draft PR"]
    J --> L["label: failed/running/ready -> review"]
    K --> L
    L --> M["comment recovery summary"]
```

### Low-Fidelity Prototype

No UI changes.

### ER Diagram

No persistent data model changes.

### Interactive Prototype Change Log

No prototype files changed.

### External Validation

No external web validation required; repository code paths and GitHub CLI usage are already present in the project.

## 6. Definition Of Done

- `recover-publish` can finish a task where Agent already produced a local commit but publish failed.
- `run-once` fails early for missing configured remote before claiming an Issue.
- Publish failure comments distinguish execution failure from publish failure.
- PR creation is idempotent and does not duplicate an existing open PR for the same head branch.
- Safety checks reject dirty worktrees, base branch publishing, missing remotes, and suspicious branches.
- Documentation and tests are updated.
- `just test` passes.

## 7. Acceptance Checklist

### Architecture Acceptance

- [ ] `src/backend/core/use_cases/recover_publish.py` exists and contains the publish recovery orchestration.
- [ ] `src/backend/api/cli.py` exposes `recover-publish --issue <number>` and optional `--branch <branch>`.
- [ ] `src/backend/core/shared/interfaces/agent_runner.py` extends `IGitHubClient` with PR lookup capability.
- [ ] `src/backend/infrastructure/github_client.py` implements PR lookup without leaking infrastructure imports into core.
- [ ] `recover_publish.py` does not call Agent CLI builders or recovery prompt logic.

### Behavior Acceptance

- [ ] `iar run-once` checks configured remote before claiming ready Issues when not in dry-run mode.
- [ ] Missing configured remote produces an error that includes the configured remote and available remotes.
- [ ] `iar recover-publish --issue 5` resolves an existing issue worktree without creating a new worktree.
- [ ] Recovery refuses to continue when the worktree has uncommitted changes.
- [ ] Recovery refuses to publish when current branch equals `[agent_runner.git].base_branch`.
- [ ] Recovery refuses suspicious branch names that do not reference the issue number unless `--branch` is supplied.
- [ ] Recovery pushes to `[agent_runner.git].remote` only; it never auto-selects another remote.
- [ ] If an open PR already exists for the head branch, recovery reuses it and does not create a duplicate PR.
- [ ] If no open PR exists, recovery creates one draft PR with body containing `Closes #<issue_number>`.
- [ ] Successful recovery removes `agent/failed`, `agent/running`, and `agent/ready`, then adds `agent/review`.
- [ ] Successful recovery comments the Issue with branch, HEAD SHA, PR URL, and whether the PR was reused.
- [ ] Publish failure in `run-once` comments the Issue with `iar recover-publish --issue <number>`.
- [ ] Publish failure comments include the failed publish operation category, such as push, PR lookup, PR create, label update, or comment update when available.

### Safety Acceptance

- [ ] `recover-publish` does not run `git add`, `git commit`, `git merge`, or branch deletion commands.
- [ ] `recover-publish` does not run any Agent command.
- [ ] `recover-publish` leaves labels unchanged when push or PR creation fails.
- [ ] `recover-publish` leaves labels unchanged when network/API/auth failures interrupt push, PR lookup, or PR creation.
- [ ] Re-running `recover-publish` after success exits successfully and reuses the existing PR.

### Documentation Acceptance

- [ ] `docs/guides/agent-runner.md` documents when to use `recover-publish`.
- [ ] The docs clarify that `labels sync` is not a publish environment validator.
- [ ] The docs include the manual recovery fallback commands for cases where GitHub CLI is unavailable.

### Validation Acceptance

- [ ] `uv run pytest tests/test_recover_publish.py -v` passes.
- [ ] `uv run pytest tests/test_run_agent.py tests/test_agent_runner_cli.py -v` passes.
- [ ] `just test` passes.

## 8. Functional Requirements

**FR-1**: `run_once` must perform publish preflight before calling `github_client.list_ready_issues` when `dry_run == False`.

**FR-2**: Publish preflight must fail if `[agent_runner.git].remote` is not present in `git remote` output, and the error must include all available remotes.

**FR-3**: `recover-publish` must resolve the issue worktree using `config.worktree.path_command` only; it must not call the worktree create command.

**FR-4**: `recover-publish` must fail if the resolved path does not exist or is not a Git worktree.

**FR-5**: `recover-publish` must fail if `git status --porcelain` is non-empty.

**FR-6**: `recover-publish` must fail if current branch is empty, equals `config.git.base_branch`, or does not reference the issue number and no explicit `--branch` was supplied.

**FR-7**: If `--branch` is supplied, `recover-publish` must fail unless the current branch exactly equals that value.

**FR-8**: `recover-publish` must push with `git push -u <config.git.remote> <current_branch>`.

**FR-9**: `IGitHubClient.find_open_pr_by_head` must return an open PR summary for the current branch when one exists.

**FR-10**: `recover-publish` must call `create_draft_pr` only when `find_open_pr_by_head` returns no open PR.

**FR-11**: `recover-publish` must update Issue labels only after push and PR lookup/create succeed.

**FR-12**: `recover-publish` must write an Issue comment after success with enough information for a human reviewer to inspect the recovered publication.

**FR-13**: `run-once` publish failure comments must include the worktree path when available and the exact recovery command.

**FR-14**: `run-once` publish failure comments must preserve enough error context for network/API/auth failures, including failed operation name and captured stdout/stderr when available.

**FR-15**: `recover-publish` must be safe to rerun after transient network/API/auth failures; it must derive current state from Git and GitHub instead of assuming the previous failed operation completed nothing.

## 9. Non-Goals

- Do not retry or repair Agent-generated code.
- Do not recover failed verification.
- Do not migrate or delete worktrees.
- Do not auto-detect and use a different remote when config is wrong.
- Do not merge PRs or enable auto-merge.
- Do not support closed PR reopening in the first implementation; closed PRs are ignored and a new draft PR may be created.

## 10. Risks And Follow-Ups

| 风险 | 缓解措施 |
|---|---|
| Branch safety matching is too strict for unusual branch names | Support explicit `--branch` override that must exactly match current branch |
| Transient network or GitHub API failures leave publish half-complete | Make recovery idempotent by checking remote branch state and existing PR before creating or updating anything |
| Existing PR lookup misses fork-qualified head branches | Keep lookup in `GitHubCliClient` so implementation can adapt the `gh pr list` flags without touching core |
| Label update succeeds but comment fails | Treat comment failure as command failure in tests unless existing GitHub client behavior already makes partial comment failures unavoidable |

## 11. Decision Log

| ID | Decision | Chosen | Rejected | Rationale |
|---|---|---|---|---|
| D-01 | Recovery mechanism | Add explicit `iar recover-publish` command | Re-run Agent recovery loop | Publish failure happens after code is complete, so rerunning Agent risks unrelated code churn |
| D-02 | State source | Derive state from worktree, Git remotes, PR list, and Issue labels | Add `.agent-runner` publish checkpoint file | Existing external state is authoritative and avoids stale local recovery metadata |
| D-03 | Remote behavior | Require configured remote to exist | Auto-use the only available remote | Wrong remote selection can publish sensitive or unintended branches |
| D-04 | PR idempotency | Query open PR by head branch before creating | Always call `gh pr create` and catch failure | Explicit lookup is testable and avoids relying on CLI error text |
| D-05 | Worktree behavior | Resolve existing worktree only | Create missing worktree during recovery | Publish recovery must operate on the completed local commit, not start a new execution environment |
