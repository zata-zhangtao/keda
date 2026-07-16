# Evidence Report: 用户级 Skill 安装迁移

对应 PRD：`tasks/archive/P1-REFACTOR-20260716-132633-user-level-skill-installation.md`

## rv-1 · 真实 CLI 初始化

- 已运行 `uv run iar init --help`：输出不含 `--copy-skills`、`--no-copy-skills`、`--skip-skills`；传入三项旧选项均以标准参数校验非零拒绝。
- 已在真实临时 Git 仓库中预存 `.claude/skills/sentinel` 后运行 `uv run --project /Users/zata/code/keda iar init`：生成 `.iar.toml` 与 IAR `.gitignore` 块，sentinel 保留，且 `.claude/skills/prd/SKILL.md`、`.codex/skills` 均不存在。
- 临时仓库写入的全局 registry 条目已通过 `iar registry remove --repo-id tmp.it64ycxen1` 清理。

## rv-2 · 用户级 PRD skill 读取

- `uv run pytest -o addopts='' tests/test_generated_content.py`：37 passed。
- 新增的 resolver 测试覆盖：显式路径 → `IAR_PRD_SKILL_PATH` → `CC_SWITCH_SKILLS_DIR` → cc-switch → Codex → Claude → 缺失时稳定 fallback。
- 已以真实本机用户级安装运行 `load_prd_skill_spec()`，实际读取 `/Users/zata/.cc-switch/skills/prd/SKILL.md` 且内容非空。

## rv-3 · 打包与遗留代码清理

- `uv build`：成功构建 `dist/keda-0.2.0.tar.gz` 和 `dist/keda-0.2.0-py3-none-any.whl`。
- 已检查 wheel 与 SDist 清单：均不含 `agent_runner/skills/(prd|code-reviewer)/SKILL.md`。
- `rg` 确认 `src` 和 `tests` 中不含 `copy_bundled_skills`、`init_flow` 或 `backend.engines.agent_runner.skills` 引用。
- GitHub Release workflow 已改为同样断言 wheel 与 SDist 都不重新包含 bundled skills。

## rv-4 · 文档与门禁

- `uv run mkdocs build --strict`：通过。
- README、安装文档、agent runner 指南和 `config.toml` 已同步为模板用户级安装及解析顺序。
- `just lint --reuse`：通过。
- `just test`：通过；本次完整运行中 86 passed、253 deselected，随后变更仅为 CI/PRD，最后一次 testmon 运行无测试需要执行并成功刷新标记。
- `just lint --full`：通过。它对工作区已删除文件打印 Git 路径警告，但退出码为 0，所有 hook 均通过。

## 待独立审查

独立 verifier 已给出 PASS；详情见同目录的 `P1-REFACTOR-20260716-132633-user-level-skill-installation.verifier-report.md`。
