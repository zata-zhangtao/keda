---
id: nightly-cleanup-product
schedule: "30 2 * * *"
repo_id: "<product-repo-id>"
priority: P1
issue_type: feature
agent: auto
labels:
  - loop/cleanup
publish_prd: true
queue_ready: true
run_now: true
timezone: Asia/Shanghai
slug: nightly-cleanup-product
# pre_command uses a single-line shell command; the simple-YAML frontmatter parser
# does not support `|` block scalars. Use `;` to chain so all KEY=value lines
# land on one logical line in stdout.
pre_command: "gh run list --limit 1 --json conclusion -q '.[0].conclusion // \"none\"' | awk '{ print \"last_ci_status=\" $0 }'; if [ -f uv.lock ]; then uv pip list --outdated --format json 2>/dev/null | jq 'length' | awk '{ print \"outdated_count=\" $0 }'; elif [ -f package-lock.json ]; then npm outdated --json 2>/dev/null | jq 'keys | length' | awk '{ print \"outdated_count=\" $0 }'; elif [ -f pnpm-lock.yaml ]; then pnpm outdated --format json 2>/dev/null | jq 'keys | length' | awk '{ print \"outdated_count=\" $0 }'; else echo outdated_count=0; fi"
---

# 夜间清理（CI / 重复代码 / 文档 / 依赖）— {{date}}

> 每晚 02:30 触发产品仓的整理 loop（与 keda 仓错峰 30 分钟，避免 GitHub API
> 配额与 `~/.iar/loop-state.json` upsert 竞争）。本次 PRD body 用 triage 决策树
> 给出 4 类 scope（CI 失败 / 重复代码合并 / 文档同步 / 依赖升级）的优先级与
> 判定指引。Agent 当晚选 1 个最高 ROI 的做；若 4 类都无事可做，关 Issue +
> comment 收尾，不出 PR。

- Loop id: `{{loop_id}}`
- Target repository: `{{repo_id}}`
- Trigger date: `{{date}}`
- Last CI status: `{{last_ci_status}}`
- Outdated dependency count: `{{outdated_count}}`

> **首次启用提醒**：把 frontmatter 里的 `repo_id: "<product-repo-id>"`
> 替换为 `config.toml` 中实际的产品仓 id。本 recipe 复用与 keda 仓相同的
> 4 类 scope 决策树，但 `pre_command` 与文档/依赖命令已按产品仓常见技术栈
> 适配（uv / npm / pnpm 三选一）。

## 1. CI 失败

- **触发条件**：`last_ci_status` 解析为 `failure` / `timed_out` / `cancelled` / `action_required` 任意非 `success` / `none`。
- **行动指引**：
  1. `gh run view <id> --log-failed` 抓失败日志；
  2. 在 worktree 内复现失败（按技术栈：`pnpm test` / `npm test` / `uv run pytest`）；
  3. 修代码 / 测试 / 配置后 `just test`（或产品仓等效命令）全绿；
  4. 出 draft PR，标题前缀 `cleanup(ci):`。
- **无事可做判定**：`last_ci_status` 为 `success` / `none`，或修复尝试一次后回归测试不通过且与失败无关。

## 2. 重复代码合并

- **触发条件**：在前端 / 后端源码内做相似度扫描（`jscpd` 或 linter 内置的 duplicate-code 检查），发现 ≥ 2 处重复块 / 近似函数 / 近似组件。
- **行动指引**：
  1. 选 ROI 最高的一处重复（被调用次数多 + 维护者最近修改频繁的优先）；
  2. 抽取公共 helper / 组件 / composable，调用方全量替换；
  3. 跑全量测试 / lint 确认无回归；
  4. 出 draft PR，标题前缀 `cleanup(refactor):`。
- **无事可做判定**：扫描结果为空，或所有重复块的 ROI 都很低（< 5 行 / 1–2 处调用方）。

## 3. 文档同步

- **触发条件**：`docs/` 与 `mkdocs.yml`（或 README）引用了源码里不存在的 import 路径 / 类名 / 模块 / 路由；新加入的 `docs/*.md` 条目未在 `mkdocs.yml` 列出。
- **行动指引**：
  1. 跑文档构建命令（`pnpm docs:build` / `npm run docs:build` / `uv run mkdocs build --strict`）看 broken link / missing file 报告；
  2. 修 `docs/guides/*.md` 与 `mkdocs.yml` 同步补齐；
  3. 再跑一次构建命令确认无 warning；
  4. 出 draft PR，标题前缀 `cleanup(docs):`。
- **无事可做判定**：构建命令全绿，无 missing / broken 报告。

## 4. 依赖升级

- **触发条件**：`outdated_count` > 5（含 5），或 0.0 ≤ X < 1.0 的小版本（patch）累积 ≥ 10。
- **行动指引**：
  1. 按技术栈锁文件升级：
     - uv: `uv lock --upgrade`
     - pnpm: `pnpm update --latest`
     - npm: `npm update`
  2. 跑全量测试 / lint 确认兼容性；
  3. 若失败，缩小到安全范围（patch only）重试；
  4. 出 draft PR，标题前缀 `cleanup(deps):`。
- **无事可做判定**：`outdated_count` 阈值未触发；或上次升级在 7 天内（避免每晚刷 PR）。

## Triage 优先级

- **当晚只做 1 个 scope**，优先级：`CI > refactor > docs > deps`。
- **判定流程**：
  1. 先按上节触发条件逐个判定（CI 看 `last_ci_status` → refactor 看扫描 → docs 看文档构建 → deps 看 `outdated_count`）。
  2. 命中最高优先级的 scope 即为当晚目标；跳过更低优先级。
  3. 4 类都未命中 → `gh issue close <N> --comment "no actionable cleanup tonight"`，**不**出 PR。
- **scope 标注**：选定 scope 后，在 worktree 内执行：
  ```bash
  gh issue edit <N> --add-label scope/<x>
  ```
  其中 `x ∈ {ci, refactor, docs, deps}`。`scope/<x>` 4 个 label 需先通过
  `iar labels sync` 在仓库创建（详见 `docs/guides/iar-loop.md`）。

## Delivery Notes

- Recommended branch: `task/<issue-number>-prd-nightly-cleanup-product-{{date}}`
- Worktree command: `iar worktree create --branch issue-<issue-number> --base-branch main`
- PR should include: `Closes #<issue-number>`
- Draft PR 标题前缀：`cleanup(<scope>): <一句话描述>`
