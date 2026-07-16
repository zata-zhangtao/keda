# Verification Plan: 用户级 Skill 安装迁移

对应 PRD：`tasks/archive/P1-REFACTOR-20260716-132633-user-level-skill-installation.md`

| RV | 验证入口 | 通过标准 |
|---|---|---|
| rv-1 | 在预存 `.claude/skills/sentinel` 的真实临时 Git 仓库运行 `uv run --project /Users/zata/code/keda iar init` | 存在 `.iar.toml`；sentinel 保留，不创建 `prd/SKILL.md` 或 `.codex/skills`；Typer 入口拒绝三项旧选项。 |
| rv-2 | `tests/test_generated_content.py::test_resolve_prd_skill_path_precedence` 与本机读取 | 显式、`IAR_PRD_SKILL_PATH`、`CC_SWITCH_SKILLS_DIR` 和三个用户级候选顺序正确；本机读取 `/Users/zata/.cc-switch/skills/prd/SKILL.md`。 |
| rv-3 | `uv build`、wheel/SDist 清单和 legacy 搜索 | 两种发行物不含 `agent_runner/skills/{prd,code-reviewer}`，源码不含复制器。 |
| rv-4 | `uv run mkdocs build --strict` 与文档 legacy 搜索 | 文档可构建，且不再承诺 `iar init` 复制 skill。 |

通用门禁：`just lint --reuse`、`just lint --full`、`just test`。独立 verifier 需审查本目录的报告和当前 diff。
