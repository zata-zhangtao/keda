# PRD: Worktree 统一集中到 `<repo>-worktrees` 并自动归类 issue 分支

## 1. Introduction & Goals

当前 `scripts/worktree/create.sh` 的默认行为是把任何 worktree 直接放在仓库父目录下：

```
repo_parent_path/branch_name
# 例：~/code/issue-3
```

这导致所有 worktree 与真实项目平级散落在 `code/` 目录下，时间一长根本无法分辨哪些是主仓库、哪些是临时工作树。用户要求**一次性根治**。

本 PRD 的目标：
1. **统一集中**：所有 worktree 归拢到 `<repo-name>-worktrees/` 下（如 `~/code/keda-worktrees/`）。
2. **自动归类**：`issue-*` 分支在该集中目录内默认落到 `tasks/` 子目录。
3. **三个脚本同步更新**：`create.sh`、`open.sh`、`merge.sh` 的路径解析统一适配新规则。

## 2. Requirement Shape

- **Actor**：开发者执行 `ai_worktree issue-3`、`ai_open issue-3` 或 `merge.sh issue-3`。
- **Trigger**：创建/打开/合并任意 worktree。
- **Expected Behavior**：
  - **create**：worktree 创建到 `$repo_parent_path/$(basename $repo_root)-worktrees/[<subdir>/]$branch_name`。
  - **open**：按分支名查找时，优先 `git worktree list`，回退路径改为 `<repo>-worktrees/branch_name`。
  - **merge**：cleanup 时的回退路径同样改为 `<repo>-worktrees/branch_name`。
  - `issue-*` 分支且未显式传 `--subdir` 时，默认 `subdir=tasks`。
  - 显式 `--subdir` 时仍优先用户指定值。
- **Scope Boundary**：
  - 改动 `scripts/worktree/` 下的 `create.sh`、`open.sh`、`merge.sh`。
  - 不迁移已创建的存量 worktree（用户自行清理 `~/code/issue-3` 等旧目录）。
  - 不引入配置文件或环境变量。

## 3. Repository Context And Architecture Fit

### 相关模块

| 文件 | 职责 | 改动类型 |
|---|---|---|
| `scripts/worktree/create.sh` | worktree 创建入口 | 修改（计算新 base 路径 + issue 默认 subdir） |
| `scripts/worktree/open.sh` | worktree 打开入口 | 修改（回退查找路径更新） |
| `scripts/worktree/merge.sh` | worktree 合并/清理 | 修改（cleanup 回退路径更新） |

### 目录结构对比

**Before：**
```
~/code/
  keda/              <- 主仓库
  issue-3/           <- worktree，散落
  feature-login/     <- worktree，散落
  refactor/          <- worktree，散落
```

**After：**
```
~/code/
  keda/                    <- 主仓库
  keda-worktrees/
    tasks/
      issue-3/             <- issue 分支自动归类
    feature-login/         <- 普通分支直接放
```

## 4. Recommendation

### Recommended Approach：统一 base 路径 + 分支名默认 subdir 推导

#### Step 1：统一计算 `<repo>-worktrees` 基础路径

三个脚本共享同一套路径约定。新增一个辅助推导逻辑：

```bash
repo_name="$(basename "$repo_root_path")"
worktrees_base_path="$repo_parent_path/${repo_name}-worktrees"
```

#### Step 2：create.sh —— 路径计算与 issue 自动归类

参数解析后、路径计算前：

```bash
# issue-* 分支默认归入 tasks 子目录
if [ -z "$subdir_name" ] && [[ "$branch_name" == issue-* ]]; then
    subdir_name="tasks"
fi
```

路径组装：

```bash
repo_name="$(basename "$repo_root_path")"
worktrees_base_path="$repo_parent_path/${repo_name}-worktrees"
if [ -n "$subdir_name" ]; then
    target_abs_path="$worktrees_base_path/$subdir_name/$branch_name"
else
    target_abs_path="$worktrees_base_path/$branch_name"
fi
```

#### Step 3：open.sh —— 更新回退查找路径

```bash
# 回退到约定路径: <repo_parent>/<repo-name>-worktrees/<branch_name>
worktree_path="$repo_parent_path/$(basename "$repo_root_path")-worktrees/$branch_name"
```

（`git worktree list --porcelain` 优先查找不受影响，因为 git 已记录绝对路径。）

#### Step 4：merge.sh —— 更新 cleanup 回退路径

两处 fallback：

1. `run_worktree_doctor` 分支特定扫描：
```bash
resolved_cleanup_worktree_path="$(dirname "$repo_root")/$(basename "$repo_root")-worktrees/$doctor_feature_branch"
```

2. `cleanup_feature_branch`：
```bash
resolved_cleanup_worktree_path="$(dirname "$repo_root")/$(basename "$repo_root")-worktrees/$feature_branch"
```

### 路径计算总览

| 分支类型 | `--subdir` | 最终路径 |
|---|---|---|
| `issue-3` | 无 | `keda-worktrees/tasks/issue-3` |
| `issue-3` | `foo` | `keda-worktrees/foo/issue-3` |
| `feature-login` | 无 | `keda-worktrees/feature-login` |
| `feature-login` | `bar` | `keda-worktrees/bar/feature-login` |

## 5. Acceptance Checklist

- [ ] `ai_worktree issue-3` 创建到 `~/code/keda-worktrees/tasks/issue-3`。
- [ ] `ai_worktree feature-login` 创建到 `~/code/keda-worktrees/feature-login`。
- [ ] `ai_worktree issue-3 --subdir foo` 创建到 `~/code/keda-worktrees/foo/issue-3`（显式优先）。
- [ ] `ai_open issue-3` 能正确打开 `~/code/keda-worktrees/tasks/issue-3`。
- [ ] `merge.sh issue-3 --cleanup` 能正确清理 `~/code/keda-worktrees/tasks/issue-3`。
- [ ] 目录已存在时报错信息正确显示新路径。
- [ ] `create.sh` 帮助文案更新，说明新集中目录和 issue 默认归类行为。
- [ ] 存量旧路径（如 `~/code/issue-3`）不受影响、不自动迁移。

## 6. Out of Scope / Future Work

- 存量 worktree 迁移：用户自行删除旧目录，脚本不做任何处理。
- 若后续需要更灵活的分支→子目录映射（如 `feature-*` → `features/`），可在 `case` 语句中追加分支模式，不改动整体结构。
- 若用户后续想把 `keda-worktrees` 改名或放到别处，可引入环境变量（如 `WORKTREE_BASE_DIR`）覆盖，本次不做。
