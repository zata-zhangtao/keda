# PRD: iar CLI 一键安装 + 必备 Skill 打包分发

- GitHub Issue: https://github.com/zata-zhangtao/keda/issues/95

Status: pending
Owner: iAR core
Created: 2026-06-17
Source: 用户要把 keda 项目产品化,使 `iar` CLI 变成"任何机器一行命令即可装上并可直接驱动 Claude/Codex 工作流"的对外工具。

## 1. Introduction & Goals

### Problem statement

`iar` CLI 当前安装链路要求用户先 `git clone keda` 仓库再 `uv tool install --editable .`,对**外部用户**而言门槛过高,且有两个隐性阻塞:

1. **没有任何对外安装入口**。`pyproject.toml` 已经把 `iar` 注册成 `[project.scripts]` 入口,但包并未发布,`uv tool install keda` / `pipx install keda` 在公开源上都解析不到。
2. **强依赖用户本地 Skill**。`prd` 与 `code-reviewer` 是 `iar` 工作流的两块拼图(前者驱动 PRD 生成链路,后者驱动 pre-push review / post-PR supervisor)。它们当前只存在于作者本人 `~/.claude/skills/`,新用户即使装上 `iar` 也跑不通闭环,体验断裂。

### Measurable goals

- G-1: 任何机器(macOS / Linux,Python ≥ 3.11)执行 `curl -fsSL https://raw.githubusercontent.com/zata/keda/main/install.sh | bash` 后,`command -v iar` 命中且 `iar --version` 返回稳定版本号。
- G-2: `git tag vX.Y.Z` 推送后,GitHub Actions 自动构建 sdist + wheel 并把两个产物 attach 到 GitHub Release,无需人工操作。
- G-3: 在 wheel 装好的 `iar` 里执行 `iar init` 时,`prd` 与 `code-reviewer` 两份 Skill 自动从包内复制到目标仓库的 `<repo>/.claude/skills/prd/` 与 `<repo>/.claude/skills/code-reviewer/`(当目标目录不存在时新建),保证外部用户开箱即用。
- G-4: CI `install-smoke` 矩阵(ubuntu-latest × macos-latest × Python 3.11 / 3.12)持续绿:每条矩阵都能成功装上 `iar` 并完成一次 `iar init --dry-run`,且复制出的 Skill 文件与 wheel 内的字节一致(SHA256 校验)。
- G-5: 不引入 sudo / 系统包改动,所有副作用落在 `~/.local` 与目标仓库目录内;安装失败有清晰错误信息并保留非零退出码。

### Realistic Validation

除单元测试和集成测试外,本 PRD 要求通过**真实项目入口点**验证关键行为,确保真实使用路径生效,而非仅在隔离 fixture 中通过。

- [ ] **`curl | bash` 真实验证**:在干净的 ubuntu-latest + Python 3.11 runner 与 macos-latest + Python 3.12 runner 上跑 `curl -fsSL https://raw.githubusercontent.com/zata/keda/main/install.sh | bash -s -- --version <dry-run-tag>`,验证 `command -v iar` 返回非空,`iar --version` 退出码 0。
- [ ] **`iar init` 真实 Skill 复制验证**:在上一步的 `iar` 安装环境里,`mkdir /tmp/fake-repo && cd /tmp/fake-repo && git init && iar init --dry-run`,验证控制台输出明确指出"will copy skills: prd, code-reviewer"并指向 `<repo>/.claude/skills/`;紧接着去掉 `--dry-run` 真跑一次,验证 `prd/SKILL.md` 与 `code-reviewer/SKILL.md` 真的落盘且 SHA256 与 wheel 包内一致。
- [ ] **GitHub Release 真实产物流验证**:`git tag v0.1.0-test` 推送到临时分支,验证 `.github/workflows/release.yml` 自动跑完 build + upload,Release 页面同时出现 `keda-0.1.0.tar.gz` 与 `keda-0.1.0-py3-none-any.whl`,且 wheel 中包含 `backend/engines/agent_runner/skills/prd/SKILL.md` 与 `backend/engines/agent_runner/skills/code-reviewer/SKILL.md`(`python -m zipfile -l` 可列)。
- [ ] **回退与 `pipx` 路径验证**:在同台 ubuntu runner 上把 `uv` 临时挪走(`mv $(command -v uv) /tmp/uv.bak`),执行 `KEDA_INSTALL_METHOD=pipx bash install.sh`,验证最终 `iar --version` 仍可用,`pipx list` 出现 `keda`。
- [ ] **为什么单元测试不够**:本 PRD 真正要证明的是"一条对外命令把外部用户从零带到可用 `iar` + 可用 Skill",其中包含网络下载、tarball 解压、wheel 打包路径、PATH 注入、目标仓库文件落盘等多段外部副作用,任何一段在 fixture 里 mock 都无法反映真实链路。

## 2. Requirement Shape

- **Actor**:
  - 外部用户(开发者 / 团队 lead),无 keda 仓库访问权限。
  - keda 维护者(打 tag、合并 PR)。
  - CI runner(GitHub Actions,ubuntu-latest / macos-latest)。
- **Trigger**:
  1. 外部用户从 README / 文档站复制 `curl | bash` 命令运行。
  2. 维护者 `git tag vX.Y.Z && git push --tags`,触发 release workflow。
  3. PR 合并到 `main`,触发 `install-smoke` workflow 跑全矩阵冒烟。
- **Expected behavior**:
  1. `install.sh` 探测 OS / Python,按 `uv → pipx → pip --user` 优先级选择安装器;缺失时自动 bootstrap uv(`astral.sh/uv/install.sh`),不要求 sudo。
  2. 默认从 GitHub Releases 下载 source tarball(`uv tool install https://github.com/zata/keda/archive/refs/tags/vX.Y.Z.tar.gz`),保留 `KEDA_PYPI=1` 切到 PyPI 源的扩展位(本 MVP 不实现 PyPI 发布,只留 hook)。
  3. 安装完成后自动跑 `iar --version` 校验;若 `~/.local/bin` 不在 PATH,打印明确提示(包括要 `source` 的 rc 文件名),并以非零退出码失败。
  4. `iar init` 在生成 `.iar.toml` 的同时,把 wheel 内 `backend/engines/agent_runner/skills/{prd,code-reviewer}/` 整树复制到 `<repo>/.claude/skills/{prd,code-reviewer}/`,支持 `--dry-run`、`--force`、已存在则跳过(SHA256 一致)或覆盖(SHA256 不一致且用户传 `--force`)。
  5. Release workflow 仅对 `v*` tag 触发,产出 sdist + wheel + SHA256SUMS,上传到 GitHub Release,不发布 PyPI。
- **Scope boundary (in)**:
  - 新增 `scripts/install/install.sh` + 仓库根 `install.sh`(软链或副本)。
  - 扩展 `pyproject.toml` 的 `[tool.setuptools.package-data]` 把 Skill 加入打包路径。
  - 把作者本机 `~/.claude/skills/prd/` 与 `~/.claude/skills/code-reviewer/` 复制进 `src/backend/engines/agent_runner/skills/`,加 `SKILL_BUNDLE_VERSION` 元数据。
  - 扩展 `iar init`(`cli_typer.py:259` 调用链)增加 Skill 复制阶段。
  - 新增 `.github/workflows/release.yml`(tag 驱动)。
  - 新增 `.github/workflows/install-smoke.yml`(PR + main 触发,矩阵冒烟)。
  - 复用现有 `just reinstall-iar` 做本地冒烟。
- **Scope boundary (out)**:
  - PyPI 发布(留 `KEDA_PYPI=1` 钩子,本 MVP 不接)。
  - 完整文档站(README 顶部加一行 + `docs/getting-started/installation.md` 草稿留待第二轮)。
  - Homebrew / Scoop / conda-forge。
  - Windows 原生支持(脚本只承诺 POSIX;README 写明建议 WSL)。
  - 自动更新(`iar self-update`),后续 PRD。
  - `templates/` 目录本身的产物(preview workflow 等)的打包范围调整(本 MVP 不动)。
  - 对 `engines/agent_runner/skills/` 之外任何 Skill 的分发。

## 3. Repository Context And Architecture Fit

### Existing patterns to reuse

- **打包机制**:`pyproject.toml` 已经用 `[tool.setuptools.package-data]` 把 `backend.engines.agent_runner.templates` 全部纳入 wheel(见 `pyproject.toml:41`)。Skill 复用同一机制,新增一行 `"backend.engines.agent_runner.skills" = ["**/*"]` 即可,无需引入新依赖。
- **`iar init` 入口**:`src/backend/api/cli_typer.py:259` 已存在 `init_command`,通过 `_run_typer_command("init", ...)` 路由到 `engines.agent_runner` 层。新增 Skill 复制阶段应在该层 (`engines/agent_runner/init_flow.py` 或类似模块)中作为独立 step 串入,避免污染现有 `.iar.toml` 渲染逻辑。
- **Skill 同步参考实现**:`scripts/template/sync_template.sh:374` 起已经有 `_collect_template_skill_updates` / `_install_template_skills`(把 `template_root/skills/` 复制到 `~/.codex/skills/` 或 `~/.claude/skills/`),其 diff 比对 + 交互选择模式可借鉴。差异:模板版目标是开发者本地机,本 PRD 的目标是"wheel 装好的目标仓库内 `.claude/skills/`",无交互(全自动)。
- **包内资源定位**:与 `templates/` 一致,使用 `importlib.resources.files("backend.engines.agent_runner.skills.prd")`,避免依赖 cwd。
- **本地可编辑安装**:`just reinstall-iar`(`justfile`)已支持 `uv tool install --reinstall --editable .`,本 PRD 的 Skill 复制在 editable 模式与 wheel 模式都必须工作。
- **CI 模板**:`.github/workflows/ci.yml` 已存在,矩阵结构可参考;新建 `release.yml` / `install-smoke.yml` 用同样 setup-python + uv 引导模式。

### Architecture boundaries

- Skill 复制逻辑位于 `engines/agent_runner/` 层,符合 `api → core → engines → infrastructure` 依赖方向:`cli_typer.py`(api)→ `engines/agent_runner/init_flow.py`(engines)→ `importlib.resources`(Python stdlib,等同 infrastructure)。
- 不在 `core/` 层新增 Skill 相关概念,避免与 `core/shared/interfaces/ISkillRegistry`(已有,语义是运行时 Skill registry,跟打包分发无关)混淆。
- 不修改 `core/use_cases/idea_inbox.py` 引用的 "idea-inbox skill"(那是项目内 mention,不是分发依赖)。

### Constraints

- Skill 文件是作者本人创作,版权需在 `src/backend/engines/agent_runner/skills/<name>/LICENSE` 同步保留(若原始 Skill 目录有 LICENSE)。
- 单代码文件非空行 ≤ 1000 行(`just lint` 会警告),`install.sh` 若超限需拆 `lib/*.sh`。
- 不破坏 `pyproject.toml` 的 `requires-python = ">=3.11"` 底线;`install.sh` 检测低于 3.11 直接报错退出。

## 4. Recommendation

### Recommended approach

采用**单包 + 内部资源 + release tarball** 的组合方案:

1. **包内资源**:把 Skill 完整目录复制到 `src/backend/engines/agent_runner/skills/{prd,code-reviewer}/`,`pyproject.toml` 加一行 `package-data` 声明。
2. **对外安装**:仓库根 `install.sh`(同 `scripts/install/install.sh`)通过 `uv tool install <GitHub tarball>` 安装,GitHub Release 作为产物的 canonical source of truth。
3. **iar init 注入 Skill**:在 `engines/agent_runner/init_flow.py` 增加 `copy_bundled_skills(repo_root)` 步骤,使用 `importlib.resources` 读取 wheel 内 Skill 树,写入 `<repo>/.claude/skills/<name>/`,遵循 `--dry-run` / `--force` 语义。
4. **CI 双工作流**:
   - `release.yml`:仅 `v*` tag 触发,`uv build` 出 sdist + wheel,计算 SHA256,`gh release upload`。
   - `install-smoke.yml`:PR + main + tag 触发,矩阵 `ubuntu/macos × 3.11/3.12`,拉 `install.sh --check` 跑 dry-run + SHA256 对比。

### Why this is the best fit

- **复用现有打包链**:`setuptools.package-data` 已经为 `templates/` 工作,Skill 直接对齐,零新依赖、零新工具。
- **不破坏四层架构**:Skill 复制逻辑天然属于 engines 层,不会污染 api/core。
- **版本对齐简单**:wheel 版本号(`pyproject.toml:version`)直接进 GitHub Release tag,`uv tool install <tarball>` 自动锁版本。
- **回退成本低**:若路线三(Docker 一键)后续要做,`install.sh` 只是一种 installer 实现,不影响包内资源布局。

### Rejected alternatives

- **走 PyPI 路线**:用户已确认首发以 GitHub 为准。PyPI 留 `KEDA_PYPI=1` 钩子后续接。
- **不打包、远程拉 Skill**:`iar init` 时 `git clone` 或 HTTP 拉 Skill,会引入外网依赖 + 版本管理复杂,且 wheel 内已有 Skill 时再外拉是浪费。
- **不打包、只打印指引**:`iar init` 提示用户手动运行 `claude skill install`,破坏 G-3(开箱即用)。
- **打进 Docker 镜像**:与路线三重叠,且 CLI 用户场景多为本地开发机,镜像不是首选载体。

## 5. Implementation Guide

> This section is a living implementation guide based on current repository analysis. If implementation discovers additional affected files, hidden dependencies, edge cases, or a better path, update this PRD before proceeding.

### Core Logic

数据与控制流:

1. **打包时**:`uv build` 读取 `pyproject.toml` 的 `package-data`,把 `src/backend/engines/agent_runner/skills/{prd,code-reviewer}/` 全部塞进 wheel 的 `backend/engines/agent_runner/skills/...` 路径。
2. **安装时**:`uv tool install <tarball>` 解压 wheel 到隔离 venv,`iar` 脚本入口指向 `backend.api.cli:main`(`pyproject.toml:28`)。
3. **运行时 `iar init`**:
   - 读取 `importlib.resources.files("backend.engines.agent_runner.skills")` 作为 Skill 根。
   - 遍历 `prd`、`code-reviewer` 两个子目录,对每个子目录:
     - 计算源 SHA256(`SKILL.md` 与同级全部文件 hash)。
     - 目标 `<repo>/.claude/skills/<name>/` 不存在 → 复制全部。
     - 存在且 SHA256 一致 → 跳过并打 `exists-identical` 日志。
     - 存在但 SHA256 不一致 + `--force` → 覆盖 + 打 `overwritten` 日志。
     - 存在但 SHA256 不一致 + 无 `--force` → 保留现有并打 `skipped-diverged` 警告(非致命,init 仍成功)。
4. **Release**:tag push → workflow 跑 `uv build` → `uv version`(或 read `pyproject.toml`)→ `gh release create <tag> dist/* --generate-notes`。
5. **Smoke**:workflow checkout 后跑 `bash install.sh --check`,然后用真装的 `iar` 在临时 `git init` 仓库里跑 `iar init`,断言 Skill 落盘。

### Change Impact Tree

```text
.
├── pyproject.toml
│   【修改】在 [tool.setuptools.package-data] 增加 skills 路径,version 升级
│
│   ├── 新增 "backend.engines.agent_runner.skills" = ["**/*"]
│   └── version: 0.1.0 → 0.2.0(首版对外)
│
├── src/backend/engines/agent_runner/skills/
│   【新增】打包两个 Skill 的载体目录
│   ├── prd/
│   │   【复制】来自作者本机 ~/.claude/skills/prd/
│   │   ├── SKILL.md
│   │   ├── scripts/  (按原样)
│   │   └── templates/  (按原样)
│   └── code-reviewer/
│       【复制】来自作者本机 ~/.claude/skills/code-reviewer/
│       └── SKILL.md
│
├── src/backend/engines/agent_runner/init_flow.py
│   【修改】增加 copy_bundled_skills 阶段,被 init_command 串入
│   ├── 新增 copy_bundled_skills(repo_root, force, dry_run) -> InitStepResult
│   ├── 使用 importlib.resources.files(...) 定位源
│   └── Init 渲染流程末尾追加该步骤
│
├── src/backend/api/cli_typer.py
│   【修改】init_command 增加 --copy-skills / --skip-skills 选项
│   └── 默认 --copy-skills=true,--skip-skills 留给高级用户
│
├── scripts/install/
│   【新增】对外安装脚本目录
│   ├── install.sh
│   │   【新增】主入口,POSIX bash,set -euo pipefail
│   │   ├── 参数: --check / --uninstall / --version / --method
│   │   ├── OS/arch 探测(uname)
│   │   ├── Python ≥ 3.11 探测
│   │   ├── uv 探测 / bootstrap
│   │   ├── 优先级: uv → pipx → pip --user
│   │   ├── 默认源: GitHub tarball,KEDA_PYPI=1 切换
│   │   ├── PATH 检测与提示
│   │   └── 装完 iar --version 校验
│   └── test/
│       【新增】bash 冒烟测试
│       └── install_smoke.sh  (在 docker / CI 跑)
│
├── install.sh
│   【新增】仓库根软链或副本,目标 = scripts/install/install.sh
│   └── 方便 https://raw.githubusercontent.com/zata/keda/main/install.sh 直读
│
├── .github/workflows/release.yml
│   【新增】tag v* 触发
│   ├── jobs.release: setup-python + uv → uv build → gh release upload
│   └── 产物: keda-X.Y.Z.tar.gz + keda-X.Y.Z-py3-none-any.whl + SHA256SUMS
│
└── .github/workflows/install-smoke.yml
    【新增】PR + push main + tag 触发
    ├── matrix: os=[ubuntu-latest,macos-latest] × python=["3.11","3.12"]
    ├── bash install.sh --check
    └── 用装的 iar 在临时 git init 仓库跑 iar init,断言 Skill 落盘
```

### Executor Drift Guard

执行实现时,需用以下 `rg` 锚点确认无遗漏引用:

```bash
# 现有 iar init 渲染链路
rg -n "def init_command|init_flow|_run_typer_command" src/backend

# 现有打包配置
rg -n "package-data|include_package" pyproject.toml

# 现有 CI 工作流结构
rg -n "setup-python|astral-sh/setup-uv|softprops/action-gh-release" .github/workflows

# 现有 Skill 相关 mention(确认不与新分发混淆)
rg -n "ISkillRegistry|skill_registry|idea-inbox skill" src/backend
```

风险点:

- `importlib.resources` 在 zipapp / onefile 场景下行为差异,但本项目走 wheel 不是 zipapp,使用 `files()` + `as_file()` context manager 即可。
- `uv tool install <tarball>` 当前对 `v*` tag tarball 支持稳定,但若 GitHub archive 端点限速,需要退路(`gh release download` 备用)。
- `setuptools` 80+ 默认行为变化(`license-files` 替代 `include_package_data`),需在 `uv build` 实跑后用 `python -m zipfile -l dist/*.whl | grep skills` 断言 Skill 真的进了 wheel。
- `~/.local/bin` 在 macOS GUI app 启动的 shell(如 VS Code integrated terminal)未必继承 PATH,`install.sh` 必须显式提示。

### Flow or Architecture Diagram

```mermaid
flowchart LR
    A[外部用户] -->|curl -fsSL install.sh \| bash| B(install.sh)
    B --> C{uv 可用?}
    C -->|是| D[uv tool install tarball]
    C -->|否| E[bootstrap uv]
    E --> D
    D --> F[iar 装入 ~/.local/bin]
    F --> G[iar --version 校验]
    G --> H{校验通过?}
    H -->|否| I[打印 PATH 修复提示,exit 1]
    H -->|是| J[iar init 在目标仓库]
    J --> K[生成 .iar.toml]
    J --> L[importlib.resources 读 wheel 内 Skill]
    L --> M[复制到 repo/.claude/skills/]
    M --> N{SHA256 一致?}
    N -->|是| O[跳过,exists-identical]
    N -->|否 + --force| P[覆盖,overwritten]
    N -->|否 + 无 --force| Q[保留并警告,skipped-diverged]
    O --> R[init 完成]
    P --> R
    Q --> R

    subgraph Release Flow
        T[git tag v*] --> U[.github/workflows/release.yml]
        U --> V[uv build]
        V --> W[sdist + wheel]
        W --> X[gh release upload]
    end
```

### Realistic Validation Plan

| Behavior | Real Entry Point | Test Layer | Mock Boundary | Data/Env Needed | Command Or Procedure | Required For Acceptance |
|---|---|---|---|---|---|---|
| `install.sh` 在干净 Linux 装上 `iar` | `curl \| bash` 真入口 | smoke | 仅 mock 网络 → 走 release tarball | ubuntu-latest runner,Python 3.11 | `bash install.sh --check` 后 `command -v iar && iar --version` | Yes |
| `install.sh` 在干净 macOS 装上 `iar` | `curl \| bash` 真入口 | smoke | 同上 | macos-latest runner,Python 3.12 | 同上 | Yes |
| Skill 真落盘 | `iar init` 真入口 | smoke | 无 mock,跑真 git 仓库 | tmpdir + `git init` | `git init && iar init`,验证 `<repo>/.claude/skills/prd/SKILL.md` SHA256 == wheel 内源 SHA256 | Yes |
| Release 产物含 Skill | `.github/workflows/release.yml` | smoke | GitHub API | tag 推送 | `python -m zipfile -l keda-*.whl \| grep skills` | Yes |
| uv 缺失 fallback 到 pipx | `install.sh` 真入口 | smoke | 临时挪走 uv | ubuntu runner | `mv $(command -v uv) /tmp/uv.bak && KEDA_INSTALL_METHOD=pipx bash install.sh && pipx list \| grep keda` | Yes |
| Skill 升级幂等 | `iar init` 真入口 | integration | 无 mock | tmpdir 跑两次 | 第二次 init 时 SHA256 一致则跳过,文件无变化 | Yes |
| Skill 冲突 + `--force` 覆盖 | `iar init` 真入口 | integration | 无 mock | tmpdir,先手写一个差异 SKILL.md | `iar init --force`,验证 Skill 文件被覆盖且 SHA256 == wheel | Yes |
| 单元测试不替代真入口 | 单测仅覆盖 SHA256 计算 / IO 异常分支 | unit | mock 全部 IO | n/a | `pytest tests/` | No |

Failure triage 笔记:

- 若 `command -v iar` 在 CI 通过但 `iar --version` 退出码非 0:检查 `install.sh` 装的 venv 是否完整,跑 `uv tool list` 看实际路径。
- 若 `<repo>/.claude/skills/` 没建出来:检查 `importlib.resources` 是否在 editable install 模式下解析到 `src/`(用 `as_file()` + `Traversable`)。
- 若 release workflow 报 403:检查 `GITHUB_TOKEN` permissions 是否含 `contents: write`,PR 来自 fork 时需手动 `workflow_dispatch`。

### Low-Fidelity Prototype

不需要交互原型,行为在 README + CI 日志即可呈现。

### ER Diagram

No data model changes in this PRD.

### Interactive Prototype Change Log

No interactive prototype file changes in this PRD.

### External Validation

No external validation required; repository evidence was sufficient. Skill 体积小(SKILL.md ~500 行 ×2),无版本兼容性风险;`uv tool install <tarball>` 是 uv 文档明确支持的安装形态。

## 6. Definition Of Done

- **Implementation validation**:`just reinstall-iar` 在 keda 本机可重现 editable 模式复制 Skill 的行为;`uv build` 出的 wheel 含 Skill(`python -m zipfile -l` 列出 `backend/engines/agent_runner/skills/`)。
- **Realistic validation**:`install-smoke` workflow 在 PR 合并前绿,矩阵 4 条全部命中 `install + iar init + Skill 落盘 + SHA256 一致`。
- **Release smoke**:`git tag v0.2.0-rc1` 推到分支后,`.github/workflows/release.yml` 自动产出 draft release,包含 sdist + wheel + SHA256SUMS,人工 `gh release publish` 后下游 `curl | bash` 链路可用。
- **Docs updates**:README 顶部加 `## 一键安装` 一行 + 链接到 `docs/getting-started/installation.md`(草稿即可,第二轮完善)。不在本 PRD 验收门内,但合并前必须存在文件骨架。
- **No regression**:现有 `pytest tests/` 全绿;`just lint` 无新增警告。
- **Architecture-fit check**:`grep -r "from backend.infrastructure" src/backend/api` 仍为空;Skill 复制代码位于 `engines/agent_runner/`,不破坏四层方向。

## 7. Acceptance Checklist

### Architecture Acceptance

- [ ] Skill 复制逻辑位于 `src/backend/engines/agent_runner/init_flow.py`,不引入 `api/` 到 `infrastructure/` 的反向导入(`rg -n "from backend\\.(api|infrastructure)" src/backend/engines/agent_runner` 不应有新增违规)。
- [ ] `pyproject.toml` 的 `[tool.setuptools.package-data]` 增加 `"backend.engines.agent_runner.skills" = ["**/*"]`,与现有 `templates` 声明并列。
- [ ] `src/backend/engines/agent_runner/skills/{prd,code-reviewer}/` 完整包含原作者 Skill 内容(`rg -c "^" src/backend/engines/agent_runner/skills/prd/SKILL.md` 应 > 500 行)。

### Dependency Acceptance

- [ ] 不引入新 PyPI 依赖(仅复用 `setuptools` / `importlib.resources` / `urllib`)。
- [ ] 不引入 sudo / 系统包操作;安装全程落在 `~/.local` 与目标仓库目录内。
- [ ] 文档化 `KEDA_PYPI=1` 钩子位置(README 一句话 + install.sh 注释),但本 MVP 不发布 PyPI。

### Behavior Acceptance

- [ ] `curl -fsSL https://raw.githubusercontent.com/zata/keda/main/install.sh | bash` 在 ubuntu-latest + Python 3.11 runner 上成功执行,`iar --version` 返回 `0.2.0`。
- [ ] `curl -fsSL ... | bash` 在 macos-latest + Python 3.12 runner 上成功执行,行为同上。
- [ ] `bash install.sh --check` 退出码 0 且只打印计划、不执行副作用。
- [ ] `bash install.sh --uninstall` 删除 `~/.local/share/uv/tools/keda` 与 `~/.local/bin/iar`。
- [ ] `KEDA_INSTALL_METHOD=pipx bash install.sh` 在 uv 缺失时仍能装上,`pipx list | grep keda` 命中。
- [ ] `iar init` 在临时 `git init` 仓库中创建 `<repo>/.claude/skills/prd/SKILL.md` 与 `<repo>/.claude/skills/code-reviewer/SKILL.md`,SHA256 与 wheel 内一致。
- [ ] `iar init` 第二次运行(目标 Skill 已存在且一致)跳过复制,文件 mtime 不变;`--force` 时覆盖。
- [ ] `iar init --skip-skills` 不复制 Skill,仅生成 `.iar.toml`。

### Documentation Acceptance

- [ ] `README.md` 顶部存在 `## 一键安装` 一段,含 `curl | bash` 一行命令(不需要第二轮文档站完整内容,但骨架必须存在)。
- [ ] `docs/getting-started/installation.md` 存在文件骨架,内容可由后续 PRD 扩充。
- [ ] `mkdocs.yml` 导航中 `getting-started/installation.md` 条目已加入。

### Validation Acceptance

- [ ] `.github/workflows/install-smoke.yml` 矩阵 4 条全绿,且**至少一条直接调用 `curl -fsSL https://raw.githubusercontent.com/zata/keda/main/install.sh | bash` 真入口**(而非只调本地 `bash install.sh`)以验证 raw URL 可达。
- [ ] `.github/workflows/install-smoke.yml` 在该矩阵中至少一条跑 `iar init` 真入口,验证 Skill 文件落盘 + SHA256 校验通过。
- [ ] `.github/workflows/release.yml` 在 `v0.2.0-rc1` tag 上跑通,产出 draft release 含 sdist + wheel + SHA256SUMS。
- [ ] `just test` 在本仓库全绿,无新增 warning。

## 8. Functional Requirements

- **FR-1**:`install.sh` 必须探测 host OS(`uname -s` 取 `darwin` / `linux`)与 arch(`uname -m` 取 `arm64` / `x86_64`),不支持的组合立即退出非零。
- **FR-2**:`install.sh` 必须探测 Python ≥ 3.11;`python3 --version` 低于 3.11 立即报错并提示用户升级。
- **FR-3**:`install.sh` 必须按 `uv → pipx → pip --user` 优先级选择安装器;`uv` 缺失时自动 `curl -LsSf https://astral.sh/uv/install.sh | sh` 并 `source ~/.local/bin/env`。
- **FR-4**:`install.sh` 必须支持 `--check`(dry-run 打印计划)、`--uninstall`(反向清理)、`--version <vX.Y.Z>`(锁版本)、`--method uv|pipx|pip`(强制指定)、`KEDA_PYPI=1`(切换到 PyPI 源,本 MVP 留 hook)。
- **FR-5**:`install.sh` 必须把 `~/.local/bin` 加入 PATH 检测;未生效时打印提示并退出非零。
- **FR-6**:`install.sh` 必须校验 `iar --version` 退出码为 0 且 stdout 含版本号;否则退出非零。
- **FR-7**:`pyproject.toml` 的 `[tool.setuptools.package-data]` 必须包含 `"backend.engines.agent_runner.skills" = ["**/*"]`。
- **FR-8**:`iar init` 必须把 `prd` 与 `code-reviewer` 两个 Skill 复制到 `<repo>/.claude/skills/<name>/`,遵循 SHA256 一致跳过、不一致 + `--force` 覆盖、不一致 + 无 `--force` 保留并警告的策略。
- **FR-9**:`iar init` 必须支持 `--copy-skills=true|false` 与 `--skip-skills` 选项,默认 `true`。
- **FR-10**:`iar init --dry-run` 必须把 Skill 复制计划打到控制台而不写盘。
- **FR-11**:`.github/workflows/release.yml` 必须仅在 `v*` tag 推送时触发,产出 `keda-X.Y.Z.tar.gz` + `keda-X.Y.Z-py3-none-any.whl` + `SHA256SUMS` 并 `gh release upload`。
- **FR-12**:`.github/workflows/install-smoke.yml` 必须以矩阵 `ubuntu-latest × macos-latest × Python 3.11/3.12` 跑 `install.sh` + `iar init` + Skill SHA256 校验,任一失败必须 fail。

## 9. Non-Goals

- 发布到 PyPI / 私有 index(留 `KEDA_PYPI=1` 钩子但本 MVP 不接)。
- 完整产品化:品牌重塑、logo、市场文案、社区治理(issue 模板、行为准则、贡献指南)。
- 跨平台原生 Windows 支持(只承诺 POSIX;README 写 WSL)。
- `iar self-update` 命令。
- Homebrew tap / Scoop manifest / conda-forge recipe。
- 文档站深度内容(mkdocs 只增 `getting-started/installation.md` 骨架)。
- 把 `templates/`(preview workflow 等)也纳入 `iar init` 默认复制(本 MVP 只动 Skill)。
- 把 `idea-inbox` 等其他内部 Skill 加入分发(只在 code 里 mention,不分发)。

## 10. Risks And Follow-Ups

- **Risk**:Skill 作者本人与 keda 仓库维护者身份合并时,Skill 升级链路("改了 ~/.claude/skills/prd 之后怎么回流到仓库?")需要单独约定。Mitigation:MVP 先以一次性 copy + 手动 `git add` 为准,留 follow-up PRD 设计 `iar skill sync` 命令。
- **Risk**:`setuptools` ≥ 80 默认行为变化可能让 `package-data` 路径需要改成 `license-files` 风格。Mitigation:`uv build` 实跑后用 `python -m zipfile -l` 断言 Skill 真的进 wheel,失败立即修。
- **Risk**:GitHub archive tarball 在大仓库时下载慢(可能 30s+),`install.sh` 默认无超时易卡。Mitigation:加 `curl --max-time 120` 与 `retry 3`。
- **Risk**:macOS 上 `~/.local/bin` 不在 GUI app PATH,用户复制的命令可能在终端 OK 但 IDE 内 terminal 找不到 `iar`。Mitigation:`install.sh` 装完打印明确提示,文档里写 `codex`/`claude` 子进程如何继承。
- **Follow-up PRD 候选**:`iar skill sync` 双向同步命令 / PyPI 发布 / 文档站深度内容 / Windows 原生支持 / Homebrew tap。
- **Follow-up PRD 候选**:`install.sh` 校验发布产物的 SHA256SUMS(本 MVP 留 hook)。

## 11. Decision Log

| ID | Decision | Chosen | Rejected | Rationale |
|----|----------|--------|----------|-----------|
| D-01 | 首个发布渠道 | GitHub Releases tarball | PyPI、GitHub Container Registry | 用户确认"主要github",可立即落地,无需 PyPI 账号;`uv tool install <tarball>` 是 uv 文档支持的稳定形态 |
| D-02 | Skill 分发方式 | 打包进 wheel,iar init 时复制 | 远端 clone、只打印指引 | "开箱即用"是 G-3 硬指标;远端 clone 引入外网依赖,只打印指引破坏 G-3 |
| D-03 | 包内 Skill 路径 | `src/backend/engines/agent_runner/skills/{prd,code-reviewer}/` | `bundled_skills/`、`src/backend/skills/` | 与现有 `templates/` 同模块、同 package-data 机制,零新机制 |
| D-04 | install 入口 URL | `https://raw.githubusercontent.com/zata/keda/main/install.sh` | 自建 `keda.dev/install.sh` | 零运维成本;GitHub raw 稳定;后续若有 CDN 需求可重定向 |
| D-05 | install 脚本是否进 wheel | 否,只放仓库文件 + GitHub raw | 打进 wheel 让 `python -m keda install` 可用 | curl\|bash 是目标用户最熟悉的形态;wheel 内 `scripts/` 不可执行,跨平台差 |
| D-06 | 默认安装器优先级 | uv → pipx → pip --user | 只支持 uv | uv 已成为 Astral 主线且 `just` 生态已用,但仍保留 pipx fallback 兼容 Linux 老系统 |
| D-07 | 是否随 PRD 发文档站深度内容 | 否,只放骨架 | 同 PR 内做完整 mkdocs | 用户确认 MVP 范围,文档深度是第二轮;骨架必须存在以满足 G-3 验收 |
| D-08 | CI 矩阵 | ubuntu + macos × 3.11 + 3.12 | 单 OS / 单 Python | 与现有 `ci.yml` 风格一致,覆盖 uv 官方支持矩阵 |
| D-09 | Skill 冲突策略 | SHA256 一致跳过 / 不一致 + --force 覆盖 / 不一致无 --force 保留并警告 | 始终覆盖 / 始终保留 | 既保护用户已有定制,又支持 force 升级;非致命警告避免 init 整体失败 |
| D-10 | 是否支持 PyPI 钩子 | 留 `KEDA_PYPI=1` 变量与 README 一句话 | 完全不提 PyPI | 用户后续可能切 PyPI,钩子零成本;不提则未来改 install.sh 兼容性差 |
