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

## 复制到目标仓库的 Skill

安装完 `iar` 之后，进入任意 Git 仓库执行：

```bash
git init
iar init
```

`iar init` 会：

1. 写入仓库根目录的 `.iar.toml`。
2. 从 wheel 包内复制 `prd` 与 `code-reviewer` 两份 Skill 到 `<repo>/.claude/skills/`，SHA256 一致时跳过，不一致且传 `--force` 时覆盖。
3. 同步标准 GitHub label（`agent`、`rework-prd` 等）。

跳过 Skill 复制的写法：

```bash
iar init --skip-skills
```

## 排错

- `command -v iar` 没命中：把 `~/.local/bin` 加入 `PATH`，或在新 shell 中重试。
- macOS GUI 终端未继承 `PATH`：在 shell rc 中显式追加 `export PATH="$HOME/.local/bin:$PATH"`。
- 安装器报 Python 版本过低：升级到 Python >= 3.11，或使用 `uv python install 3.12` 后重试。

## 卸载

```bash
bash install.sh --uninstall
```

会清理 `keda` tool 目录与 `~/.local/bin/iar`。
