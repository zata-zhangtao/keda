# 一键安装 iar CLI

> 草稿：本文档为骨架，详细内容将由后续 PRD 补充。当前提供 curl-pipe 模式、参数说明与排错指南。

## 最快路径

```bash
curl -fsSL https://raw.githubusercontent.com/zata-zhangtao/keda/main/install.sh | bash
iar --version
```

安装器会按 `uv → pipx → pip --user` 的优先级选择安装方式，缺失 `uv` 时自动从 `astral.sh` 引导；不需要 `sudo`。

## 常用参数

| 参数 / 环境变量 | 作用 |
| --- | --- |
| `--version <tag>` | 锁定具体 release tag，例如 `--version v0.2.0`。 |
| `--method uv\|pipx\|pip` | 强制使用指定安装器。 |
| `--check` | 打印安装计划但不做任何修改。 |
| `--uninstall` | 卸载 `keda` tool 与 `iar` 入口。 |
| `KEDA_INSTALL_METHOD` | 等价于 `--method`。 |
| `KEDA_VERSION` | 等价于 `--version`。 |
| `KEDA_PYPI=1` | 预留钩子，从 PyPI 拉取（尚未启用）。 |

## 初始化仓库与用户级 Skills

安装完 `iar` 之后，进入任意 Git 仓库执行：

```bash
git init
iar init
```

`iar init` 会：

1. 写入仓库根目录的 `.iar.toml`。
2. 管理 IAR 所需的 `.gitignore` 条目。
3. 同步标准 GitHub label（`agent`、`rework-prd` 等）。

`prd` 与 `code-reviewer` 不随 wheel 分发。`iar init` 会从远程
[`zata-codes-template`](https://github.com/zata-zhangtao/zata-codes-template) 下载且仅下载这两个
Skill，再安装到用户级目录；不会写入项目内 `.claude/skills`、`.codex/skills` 或
`.kimi-code/skills`。它优先使用 `CC_SWITCH_SKILLS_DIR`，随后选择已有的 cc-switch、Codex、Claude、
Kimi Code 配置目录；都不存在时创建 `~/.codex/skills`。因此该步骤需要能够访问 GitHub。

## 容器化运行（可选）

`iar` 还提供 `iar container` 子命令组，把 agent runner 跑进 Docker 容器：

- 容器内预装 claude / codex / kimi 三个 agent CLI + gh + Node + uv + just + git，避免污染本机工具链。
- 认证通过 `iar container auth import` 一次性快照到 `~/.iar/container-auth/`，与本机 cc-switch 当前 profile 隔离——本机切账号不影响容器内 agent 认证。
- 目标仓库挂载进容器，agent 在挂载目录的 `.iar-worktrees/` 建 worktree，宿主机可直接 `iar worktree open` 接管。
- runner 容器资产（Dockerfile / compose / .env.example）随 `iar` 包发布，无需克隆 keda 源码，全局安装后即可使用。

最小启动流程：

```bash
# 1. 准备认证（本机 cc-switch 切到要给容器用的账号）
iar container auth import

# 2. 准备 GitHub token（macOS keychain 容器读不到）
export GH_TOKEN="$(gh auth token)"

# 3. 启动容器 runner
iar container up --repo /absolute/path/to/your-repo --repo-id keda

# 4. 查看日志 / 停止
iar container logs
iar container down
```

完整说明见 `docs/guides/agent-runner.md` 的「容器化运行」章节。Docker 未安装时该子命令返回明确错误，不影响本机 `iar daemon` 用法。

## 排错

- `command -v iar` 没命中：把 `~/.local/bin` 加入 `PATH`，或在新 shell 中重试。
- macOS GUI 终端未继承 `PATH`：在 shell rc 中显式追加 `export PATH="$HOME/.local/bin:$PATH"`。
- 安装器报 Python 版本过低：升级到 Python >= 3.11，或使用 `uv python install 3.12` 后重试。

## 卸载

```bash
bash install.sh --uninstall
```

会清理 `keda` tool 目录与 `~/.local/bin/iar`。
