# Codex macOS 通知

本指南用于把 Codex CLI 的任务完成事件固化为 macOS 原生通知横幅。实现方式是：Codex CLI `notify` 调用仓库脚本，脚本再运行系统快捷指令。

## 前置条件

- 系统为 macOS。
- 已安装并登录 Codex CLI。
- 在 Shortcuts App 中创建一个名为 `codex通知` 的快捷指令。
- 快捷指令中包含“显示通知”动作，并允许 Shortcuts 发送通知。
- 若要显示仓库名，在快捷指令里先添加“从输入获取文本”，输入选择“快捷指令输入”，再把“显示通知”的正文设置为该文本。

如果你的快捷指令使用了其他名字，安装时把名字作为参数传入。

## 安装

在仓库根目录运行：

```bash
just codex-notify install codex通知
```

该命令会更新 `~/.codex/config.toml` 顶层配置：

```toml
notify = ["env", "CODEX_NOTIFY_SHORTCUT_NAME=codex通知", "bash", "/absolute/path/to/scripts/codex/notify_shortcut.sh"]
```

安装脚本会在修改前备份已有配置，备份文件形如：

```text
~/.codex/config.toml.bak.YYYYMMDD-HHMMSS
```

## 测试

运行：

```bash
just codex-notify test codex通知
```

也可以安装后立即测试：

```bash
./scripts/codex/install_macos_notify.sh codex通知 --test
```

如果命令成功退出但没有看到通知，优先检查：

- Shortcuts App 中是否存在同名快捷指令。
- 快捷指令是否包含“显示通知”动作。
- 系统设置中 Shortcuts 的通知权限是否开启。
- macOS 专注模式是否屏蔽了通知。

## 行为说明

- 脚本路径：`scripts/codex/notify_shortcut.sh`
- 安装脚本：`scripts/codex/install_macos_notify.sh`
- 默认快捷指令名：`codex通知`
- 处理事件：`agent-turn-complete`
- 非 macOS 系统会直接退出，不阻塞 Codex。

Codex 会把通知 JSON 作为参数传给脚本。脚本会使用该 JSON 过滤事件类型，并优先根据 payload 中的 `cwd` 判断当前任务所在仓库。
脚本会把仓库名和当前 Git 分支作为快捷指令输入传入，格式类似：

```text
Codex task complete: zata_code_template (main)
```

快捷指令需要读取输入文本并把它放到通知正文中，才能显示这段信息。
`just codex-notify test` 会在终端打印本次传给快捷指令的输入，便于确认脚本侧生成的内容。

## 生效范围

`notify` 是 Codex CLI 的全局配置项，写入的是当前用户的 `~/.codex/config.toml`。安装命令不会把脚本复制进 `~/.codex`，而是把本仓库脚本的绝对路径写入配置。

新启动的 Codex CLI 会话会读取该配置；已经运行中的会话不保证重新加载。全局配置可以服务其他仓库，因为 Codex 触发通知时会传入任务 `cwd`，脚本会优先用该路径显示实际任务仓库。若没有 `cwd`，则依次回退到调用时的 `PWD` 和脚本所在仓库。
