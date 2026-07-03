---
id: nightly-cleanup-keda
schedule: "0 2 * * *"
repo_id: keda
priority: P1
issue_type: feature
agent: auto
labels:
  - loop/cleanup
publish_prd: true
queue_ready: true
run_now: true
timezone: Asia/Shanghai
slug: nightly-cleanup-keda
# pre_command uses a single-line shell command; the simple-YAML frontmatter parser
# does not support `|` block scalars. Use `;` to chain so both KEY=value lines
# land on one logical line in stdout.
pre_command: "gh run list --limit 1 --json conclusion -q '.[0].conclusion // \"none\"' | awk '{ print \"last_ci_status=\" $0 }'; uv pip list --outdated --format json 2>/dev/null | jq 'length' | awk '{ print \"outdated_count=\" $0 }'"
---

# 夜间清理（CI / 重复代码 / 文档 / 依赖）— {{date}}

> 每晚 02:00 触发 keda 仓的整理 loop。本次 PRD body 用 triage 决策树给出
> 4 类 scope（CI 失败 / 重复代码合并 / 文档同步 / 依赖升级）的优先级与判定
> 指引。Agent 当晚选 1 个最高 ROI 的做；若 4 类都无事可做，关 Issue + comment
> 收尾，不出 PR。

- Loop id: `{{loop_id}}`
- Target repository: `{{repo_id}}`
- Trigger date: `{{date}}`
- Last CI status: `{{last_ci_status}}`
- Outdated dependency count: `{{outdated_count}}`

## 1. CI 失败

- **触发条件**：`last_ci_status` 解析为 `failure` / `timed_out` / `cancelled` / `action_required` 任意非 `success` / `none`。
- **行动指引**：
  1. `gh run view <id> --log-failed` 抓失败日志；
  2. 在 worktree 内复现失败（`uv run pytest <path> -v`）；
  3. 修代码 / 测试 / 配置后 `just test` 全绿；
  4. 出 draft PR，标题前缀 `cleanup(ci):`。
- **无事可做判定**：`last_ci_status` 为 `success` / `none`，或修复尝试一次后回归测试不通过且与失败无关。

## 2. 重复代码合并

- **触发条件**：在 `src/backend/` 内对 helper 类函数做相似度扫描（`pylint --disable=all --enable=duplicate-code` 或 `jscpd` 之类），发现 ≥ 2 处重复块 / 近似函数。
- **行动指引**：
  1. 选 ROI 最高的一处重复（被调用次数多 + 维护者最近修改频繁的优先）；
  2. 抽取公共 helper / 合并到单一函数，调用方全量替换；
  3. `just test` 确认无回归；
  4. 出 draft PR，标题前缀 `cleanup(refactor):`。
- **无事可做判定**：扫描结果为空，或所有重复块的 ROI 都很低（< 5 行 / 1–2 处调用方）。

## 3. 文档同步

- **触发条件**：`rg "mkdocs.yml|docs/"` 命中路径对比发现：① `docs/` 引用了源码里不存在的 import 路径 / 类名 / 模块；② `mkdocs.yml` 缺少新加入的 `docs/*.md` 条目。
- **行动指引**：
  1. 跑 `uv run mkdocs build --strict` 看 broken link / missing file 报告；
  2. 修 `docs/guides/*.md` 与 `mkdocs.yml` 同步补齐；
  3. 再跑一次 `mkdocs build --strict` 确认无 warning；
  4. 出 draft PR，标题前缀 `cleanup(docs):`。
- **无事可做判定**：`mkdocs build --strict` 全绿，无 missing / broken 报告。

## 4. 依赖升级

- **触发条件**：`outdated_count` > 5（含 5），或 0.0 ≤ X < 1.0 的小版本（patch）累积 ≥ 10。
- **行动指引**：
  1. `uv lock --upgrade` 一把锁，或手工选高 ROI 的几个直接依赖（被本仓 `import` 的多、安全公告紧的）；
  2. `just test` 确认兼容性；
  3. 若失败，缩小到安全范围（patch only）重试；
  4. 出 draft PR，标题前缀 `cleanup(deps):`。
- **无事可做判定**：`outdated_count` 阈值未触发；或上次升级在 7 天内（避免每晚刷 PR）。

## Triage 优先级

- **当晚只做 1 个 scope**，优先级：`CI > refactor > docs > deps`。
- **判定流程**：
  1. 先按上节触发条件逐个判定（CI 看 `last_ci_status` → refactor 看扫描 → docs 看 mkdocs → deps 看 `outdated_count`）。
  2. 命中最高优先级的 scope 即为当晚目标；跳过更低优先级。
  3. 4 类都未命中 → `gh issue close <N> --comment "no actionable cleanup tonight"`，**不**出 PR。
- **scope 标注**：选定 scope 后，在 worktree 内执行：
  ```bash
  gh issue edit <N> --add-label scope/<x>
  ```
  其中 `x ∈ {ci, refactor, docs, deps}`。`scope/<x>` 4 个 label 需先通过
  `iar labels sync` 在仓库创建（详见 `docs/guides/iar-loop.md`）。

## Delivery Notes

- Recommended branch: `task/<issue-number>-prd-nightly-cleanup-keda-{{date}}`
- Worktree command: `iar worktree create --branch issue-<issue-number> --base-branch main`
- PR should include: `Closes #<issue-number>`
- Draft PR 标题前缀：`cleanup(<scope>): <一句话描述>`
