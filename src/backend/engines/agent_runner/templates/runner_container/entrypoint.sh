#!/bin/sh
# -----------------------------------------------------------------------------
# iar Runner container entrypoint
#
# 容器以 root 身份启动。镜像内 ``/home/runner`` 默认属主是 ``runner:runner``
# (UID 1000/GID 1000)。当宿主 UID/GID 不是 1000 时,容器进程无权写
# ``/home/runner``,gh/claude/codex/kimi 会因为创建 ``~/.config`` / ``~/.cache``
# 等子目录失败而无法启动(即使 ``--version`` 也会触发路径别名持久化)。
#
# 这里先用 ``chown`` 把 ``/home/runner`` 对齐到 runtime UID/GID,再 ``gosu``
# 切到目标 UID/GID 跑 ``CMD``。``gosu UID:GID cmd`` 直接走 setresuid /
# setresgid,不需要 passwd 记录,因此宿主机 UID/GID 任意值都能跑。
#
# 注意 gosu 1.17 在切用户时会按 /etc/passwd 重置 HOME(找不到对应记录时
# 退回 /);而宿主映射的 UID 通常没有容器内的 passwd 记录,HOME 会被改成 /,
# 让 claude/gh 把配置写到错误的根目录。这里通过显式 ``env HOME=...`` 把
# HOME/IAR_HOME 注入到 gosu 之后的子进程,绕开 gosu 的 HOME 重置。
# -----------------------------------------------------------------------------

set -e

TARGET_UID="${RUNNER_UID:-1000}"
TARGET_GID="${RUNNER_GID:-1000}"
RUNNER_HOME="/home/runner"

# 一次性把 /home/runner 的属主改成 runtime UID/GID(递归覆盖子目录与隐藏文
# 件)。如果宿主 UID 就是 1000,chown 是 no-op,零成本。
# ``/home/runner/.iar`` 可能是宿主 bind-mount,其下 ``repos/<...>/.git/objects``
# 含 read-only 文件,chown 会刷大量 ``Permission denied``。这些是预期噪声
# (宿主那侧的 git objects 不需要改属主),用 ``2>/dev/null`` 抑制。
if [ -d "${RUNNER_HOME}" ]; then
    chown -R "${TARGET_UID}:${TARGET_GID}" "${RUNNER_HOME}" 2>/dev/null || true
fi

# 把 iar 安装目录(/opt/keda)的 PATH 暴露出来
export PATH="/opt/keda/.venv/bin:${PATH}"

# HOME 备用值:宿主注入的 ``HOME``(如果有)优先,否则用 /home/runner。
# gosu 切用户时按 /etc/passwd 重置 HOME;为了保留 ``HOME=/home/runner``
# 我们在 gosu 后包一层 ``env`` 把 HOME/IAR_HOME 显式传过去。
export HOME="${HOME:-${RUNNER_HOME}}"
export IAR_HOME="${IAR_HOME:-${RUNNER_HOME}/.iar}"

# 用 gosu 切到目标 UID/GID 跑 CMD(默认 ``iar daemon``)。
# gosu 接受 ``UID:GID`` 数字形式直接走 syscall,无需 passwd 记录。
# HOME 必须在 env 显式列出,否则 gosu 改回 /。
exec gosu "${TARGET_UID}:${TARGET_GID}" env HOME="${HOME}" IAR_HOME="${IAR_HOME}" IAR_REPO_ID="${IAR_REPO_ID:-}" REPO_PATH="${REPO_PATH:-}" GH_TOKEN="${GH_TOKEN:-}" PATH="${PATH}" "$@"
