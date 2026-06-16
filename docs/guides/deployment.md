# 部署指南

本文档提供模板项目在不同环境下的部署建议。

## 部署前检查

- 确保 `uv lock` 与 `uv sync` 能正常执行。
- 校验环境变量完整性，特别是数据库和模型 API Key。
- 执行测试：`just test`。
- 构建文档：`uv run mkdocs build --strict`。

## GitHub Actions 模板

模板仓库内置两条 GitHub Actions 工作流，默认放在 `.github/workflows/`：

1. `ci.yml`
   - 触发时机：`pull_request`、推送到 `main`、手动触发。
   - 执行内容：`uv sync --all-extras --all-groups --frozen`、`pre-commit`、本地测试集、`mkdocs build --strict`、发布包烟雾构建。
2. `cd.yml`
   - 触发时机：推送 `v*` 标签、手动触发。
   - 执行内容：重复执行发布前校验，生成 `dist/*.zip`，上传构建产物；当事件来自标签推送时，同时创建 GitHub Release。

如果下游项目直接继承此模板，通常只需要根据自身情况调整：

- `PYTHON_VERSION`
- 标签规则（默认 `v*`）
- 测试命令或额外构建步骤
- 是否保留 GitHub Release 发布逻辑

## 推荐部署流程

1. 拉取代码并同步依赖。
2. 注入生产环境变量。
3. 执行数据库初始化（按项目实际实现）。
4. 启动服务入口（如 `src/backend/main.py`、根目录 `main.py` 包装器，或任务调度器）。

## 环境变量管理

- 使用平台密钥管理工具保存敏感信息。
- 避免把真实密钥写入仓库。
- 为不同环境准备差异化配置，例如开发、测试、生产。

## 可观测性建议

- 接入集中日志平台采集 `logs/app.log`。
- 补充错误告警策略。
- 针对关键任务建立成功率与耗时监控。

## 容器化部署模板

项目提供两种容器化部署路径：

### 生产部署（Dokploy）

使用仓库根目录的 `docker-compose.dokploy.yml`，通过 Dokploy UI 指向该文件进行部署：

1. 在 Dokploy 中设置 `DOMAIN`。
2. 配置生产数据库 `DATABASE_URL`。
3. 确认 `dokploy-network` 已存在。

### PR 预览部署（每 PR 临时 Docker + Traefik）

`.github/workflows/deploy-preview.yml` 在 PR 打开、更新或重新打开时，自动构建并部署一个独立的预览栈：

- 每个 PR 拥有独立的 Compose project、网络与命名卷。
- 预览栈包含 `frontend`、`backend` 与临时 `postgres`。
- 通过 `pr-<N>.<base_domain>` 暴露，URL 以 sticky 评论形式回写到 PR。
- PR 关闭时自动拆除对应栈。

#### 服务器初始化（一次性）

预览服务器在首次部署前需装好 Docker、Traefik、ACME 证书、deploy user，并设好 GitHub Secrets。仓库提供 `scripts/provision_preview_server.py` 一步完成所有这些。

> 💡 **每个服务器跑一次，不按项目计**：脚本操作的是**服务器**（装 Docker、起 Traefik、建目录结构、设 Secrets），不是单个项目。同 VPS 上跑多个项目时只跑一次；新增项目不需要重跑，只在自己项目的 `config.toml [preview]` 配 `base_domain`（与服务器一致）即可。重跑触发条件：换 VPS、换域名、换 Let's Encrypt 账号、Traefik 大版本升级、轮换 GitHub Actions deploy key、或加 `--force` 全量重置。

##### 选哪种命令

按你机器的当前状态挑一条。

**A. 一台全新 VPS，第一次跑**（推荐用这个——一次到位）：

```bash
uv run python scripts/provision_preview_server.py \
  --host YOUR_VPS_IP --user root --key '~/.ssh/your_login_key' \
  --domain preview.example.com --email you@example.com \
  --skip-traefik --skip-docker \
  --create-deploy-user --generate-deploy-key \
  --apply-secrets --apply-config
```

这条命令做这些事：① SSH 登录（用 key）→ ② 建 deploy user + 加 docker 组 + chown /opt/preview → ③ 服务器现场生成 GitHub Actions 用的 deploy key（私钥 scp 下来，scp 完就 `rm`）→ ④ 探测到已有 traefik/docker 就跳过 → ⑤ 交互式设 6 个 GitHub Secrets（自动创建 `preview` environment）→ ⑥ 交互式写 `config.toml [preview]` 段（带 diff 确认 + 自动 .bak 备份）。

> **如果你的机器上 80/443 是空的（没装任何 Traefik）**：去掉 `--skip-traefik`，脚本会自己装 `preview-traefik`；同理 Docker 没装就去掉 `--skip-docker`。脚本默认行为是"假设机器是空的"。

**B. VPS 已经按 A 跑过，只想重设 Secrets / config.toml**（不能加 `--generate-deploy-key`，会撞）：

```bash
uv run python scripts/provision_preview_server.py \
  --host YOUR_VPS_IP --user root --key '~/.ssh/your_login_key' \
  --domain preview.example.com \
  --skip-traefik --skip-docker \
  --create-deploy-user \
  --apply-secrets --apply-config
```

跳过 `--generate-deploy-key` 因为 server 上 `~/.ssh/preview_deploy_key` 已经在用了，脚本拒绝覆盖。其他都是幂等的：`deploy` user 已存在 → 跳过；`/opt/preview` chown 幂等；环境 `preview` 已存在 → 直接进 set secret 流程。

**C. 只用 key 登录、不用 create-deploy-user**（个人/小项目、不在乎 root 部署）：

```bash
uv run python scripts/provision_preview_server.py \
  --host YOUR_VPS_IP --user root --key '~/.ssh/your_login_key' \
  --domain preview.example.com --email you@example.com \
  --skip-traefik --skip-docker \
  --generate-deploy-key
```

所有 provision 步骤仍以 root 跑；不创建 deploy user。`SERVER_USER` secret 填 `root`（不是 `deploy`）。

**D. 轮换 GitHub Actions 用的 deploy key**（怀疑旧 key 泄露或定期轮换）：

```bash
# 1. 先 SSH 上 server 把旧 key 删掉（pub 在 deploy user authorized_keys 里不动）
ssh root@YOUR_VPS_IP 'rm -f ~/.ssh/preview_deploy_key ~/.ssh/preview_deploy_key.pub'

# 2. 再跑 A 命令（带 --generate-deploy-key）
uv run python scripts/provision_preview_server.py ... --generate-deploy-key --apply-secrets
# 3. apply-secrets 步骤里：SERVER_SSH_KEY 已有 → 脚本问"Overwrite?" → yes
# 4. 私钥 scp 下来 → gh secret set SERVER_SSH_KEY < 新路径 → rm 新路径
```

`authorized_keys` 里的 pub 不用动；GitHub Secrets 里的旧私钥被新私钥覆盖；deploy 用同 key pair 的新私钥 + 旧 pub，CI 一切正常。

> **为什么 `--generate-deploy-key` 不能直接再跑覆盖？**：脚本拒绝覆盖已存在的 key，防止运维误操作把已部署 workflow 用的 key 静默换掉。要轮换必须先 rm。

##### 跳过标志 / 行为标志语义

- **`--skip-docker`**：假设 Docker 已装好；其它步骤（写 Traefik 配置、起 preview-traefik、装 key、打印清单）照常。
- **`--skip-traefik`**：假设服务器上已经有自己的 Traefik 占着 80/443。脚本不会装 `preview-traefik`，只确保存在 `traefik` docker network（preview 栈要加入才能被发现），并按 image 名检测现有 Traefik 是否在该 network（没加入会打印 `docker network connect traefik <容器名>` 提示）。这个模式 ACME 由你现有 Traefik 管，**不用 `--email`**。
- `--skip-docker` 和 `--skip-traefik` 独立可组合；可只跳一个。
- **`--generate-deploy-key`**：在**服务器**上现场生成新的 ed25519 key pair。公钥装到 deploy user 的 `authorized_keys`（如果传了 `--create-deploy-user`，否则装到 SSH bootstrap user 的）；私钥 `scp` 到本地临时文件，**只活到 `gh secret set` 那一次**。脚本末尾自动给出 `gh secret set SERVER_SSH_KEY --env preview < /tmp/preview-deploy-key-XXXX/id_ed25519` 一行命令和"上传完就 `rm`"的警告。已存在 `~/.ssh/preview_deploy_key` 时**拒绝覆盖**。
- **`--create-deploy-user`**：生产推荐。脚本先用 `--user root` 权限 SSH 进去 `useradd --deploy-user <name>` + `usermod -aG docker` + `chown <deploy_user> <deploy_user> /opt/preview`。**不**装 `--key` 公钥到 deploy user 的 `authorized_keys`——`--key` 是给运维人员登录用的，**只**进 bootstrap user（默认 root）的 `authorized_keys`；deploy user 的唯一 SSH key 来自 `--generate-deploy-key`。GitHub Actions `SERVER_USER` secret 填 `<deploy_user>`（默认 `deploy`）。要求 `--deploy-user != --user`（SSH 用的 user 必须已存在）。第二次跑幂等：user 已存在时跳过 useradd。

脚本支持 SSH key 或密码（密码需 `sshpass`，macOS 用 `brew install sshpass`）。完整参数与行为见 `uv run python scripts/provision_preview_server.py --help`。

#### 脚本每步具体产生什么

| 阶段 | 触发条件 | 行为 | 副作用（本地） | 副作用（远端） |
|------|----------|------|----------------|----------------|
| 1. SSH ControlMaster | 总是 | `sshpass` + 短命令 + `ControlPersist=20m` 复用一条连接 | 创建 `/tmp/pp-<pid>-<hex>.sock` | 仅一次 PAM/认证（之后所有 ssh/scp 走 socket，不再重认证） |
| 2. 探测 OS | 总是 | `cat /etc/os-release` | — | 无 |
| 3. 创建 deploy user | 传了 `--create-deploy-user` | `useradd --deploy-user <name>`、`usermod -aG docker <name>`、`chown <deploy_user> <deploy_user> --app-dir`。**不**装 `--key` 公钥到 deploy user 的 `authorized_keys` | — | 新 user + home dir；`/opt/preview` owner 改成 deploy |
| 4. 装 Docker | 未加 `--skip-docker` | `apt-get update + install docker-ce`、加 GPG key、加 Docker 官方源、`systemctl enable --now docker` | — | daemon `docker` 起来；`docker` / `docker compose` 可用；`docker` group 已建 |
| 5. 写 Traefik 配置 | 未加 `--skip-traefik` | scp 上传 `traefik.yml` + 准备 `letsencrypt` 目录 | — | `/opt/preview/traefik/traefik.yml` 新建/覆盖；`/opt/preview/traefik/letsencrypt/acme.json` 创建（mode 0600） |
| 6. 起 `preview-traefik` 容器 | 未加 `--skip-traefik` | `docker run -d ... traefik:v3.1`，监听 80/443，挂 Docker socket、Traefik 配置、ACME 存储，加入 `traefik` network | — | 容器 `preview-traefik` running；docker network `traefik` 创建（已存在则跳过） |
| 7. 端口 80/443 检查 | 未加 `--skip-traefik` 且 80/443 已被占 | 提前 abort + 提示 `--skip-traefik` | — | — |
| 8. 装公钥（登录 key） | 传了 `--key` | **只是把你登录用的那把 key 的公钥推上去**（到 SSH bootstrap user 的 home，即 `--user` 的 home，默认 `root`），方便以后用 key 而不是密码登录。**不是给 GitHub Actions 用的**——这把 key 仍只在你 Mac 上 | — | SSH bootstrap user 的 `~/.ssh/authorized_keys` 新增一行 pub（600 权限） |
| 9. 生成 deploy key | 传了 `--generate-deploy-key` | 服务器上 `ssh-keygen -t ed25519` 生成**新** key pair，**专给 GitHub Actions 用**；公钥装到**目标** `authorized_keys`：传了 `--create-deploy-user` 时是 `/home/<deploy_user>/.ssh/authorized_keys`（chown 给 deploy），否则是 SSH bootstrap user 的 `~/.ssh/authorized_keys`；**私钥 scp 到本地临时文件**（只活到 `gh secret set` 那一次） | 临时文件 `/tmp/preview-deploy-key-XXXX/id_ed25519`（0600） | 服务器 `~/.ssh/preview_deploy_key{,.pub}` 新建（bootstrap user 路径下）；公钥入目标 authorized_keys |
| 10. 打印 next steps / `--apply-secrets` / `--apply-config` | 总是 / 传了 flag | 打印 DNS + gh secret set + config.toml 模板；或交互式设 secret + 改 config.toml | — | — |

> `APP_DIR`（默认 `/opt/preview`）是脚本期望的 preview 栈工作目录；`config.toml [preview].app_dir_root` 必须填同一个值，CI 才会用同样的路径 rsync `deploy-preview.sh` 上去。

#### 所有 GitHub Secrets（preview 环境）

下面 6 个 secret 是 `.github/workflows/deploy-preview.yml` 实际读到的，全部挂在名为 `preview` 的 GitHub environment 下。脚本末尾会逐条打印对应的 `gh secret set` 命令（用 `--apply-secrets` 可以交互式一条条 set，否则手动复制粘贴）：

| Secret 名 | 内容 | 在 workflow 哪里用 | 来源（脚本帮你怎么填） |
|-----------|------|---------------------|--------------------------|
| `SERVER_HOST` | 服务器 IP 或域名 | `deploy` / `teardown` step 的 `ssh "${SERVER_USER}@${SERVER_HOST}"` | `--host` 直接写死 |
| `SERVER_USER` | 服务器 SSH user | 同上 | 同上 |
| `SERVER_SSH_KEY` | **专给 GitHub Actions 用的私钥**（`webfactory/ssh-agent` 把它加到 runner ssh-agent） | `Add SSH key` step | 用 `--generate-deploy-key`（脚本在服务器上现场生成再 scp 下来）；**不要**把 `--key` 那把你自己的登录 key 私钥填进来——登录 key 私钥不该泄露给 CI |
| `REGISTRY_USERNAME` | ghcr.io 命名空间用户名（一般是 `gh repo view` 看到的 owner） | `Log in to container registry` step（`docker/login-action`） | `--apply-secrets` 模式提示输入；或手动 |
| `REGISTRY_PASSWORD` | **Classic** GitHub PAT：scope 必须勾 `write:packages` + `read:packages` + `repo`（ghcr.io 镜像挂在 repo 命名空间下）。**不能用 fine-grained PAT**——fine-grained 的 Repository permissions 列表里没有 "Packages" 这一项 | 同上 | `--apply-secrets` 模式用 `getpass` 输入（不回显）；或手动。获取步骤见下面"获取 Classic GitHub PAT"小节 |
| `POSTGRES_PASSWORD` | 预览 postgres 初始密码（每 PR compose 都启一个新 postgres 容器） | `deploy` step 里 `ssh ... POSTGRES_PASSWORD='...' bash deploy-preview.sh up` | `--apply-secrets` 模式自动 `secrets.token_urlsafe(32)` 生成一个并打印出来（**这个值要保存**，因为 destroy 后 re-deploy 不一定能恢复） |

> `GITHUB_TOKEN` 是 GitHub Actions 自带的临时 token（用来调 `gh pr comment` 写 sticky 评论），**不是 secret**、**不需要你 set**。

##### 获取 Classic GitHub PAT

1. 打开 https://github.com/settings/tokens/new
2. **Note**：`preview-deployment`（任意）
3. **Expiration**：90 天（classic 强制要设过期，不能 "No expiration"）
4. **Select scopes** 必勾这三个：
   - ☑️ `write:packages` — 推镜像
   - ☑️ `read:packages` — 拉镜像
   - ☑️ `repo` — ghcr.io 镜像挂在 repo 命名空间下
5. 点 **Generate token**；复制 `ghp_...`（**只显示一次**）
6. 验证（可选，但能提前确认 PAT 配对了）：

   ```bash
   echo "ghp_你的PAT" | docker login ghcr.io -u zata-zhangtao --password-stdin
   # → Login Succeeded 表示 OK
   ```

按 `--apply-secrets` 时脚本会按以下顺序逐条跟你确认：

- `SERVER_HOST` / `SERVER_USER` / `SERVER_SSH_KEY`：自动填好，直接确认
- `REGISTRY_USERNAME`：提示输入
- `REGISTRY_PASSWORD`：`getpass` 输入不回显
- `POSTGRES_PASSWORD`：自动生成 32 字节随机串并**打印到屏幕**，**务必保存**（destroy 后 re-deploy 同一个 PR 会复用这个密码重建数据库）

> 💡 **`base_domain` 两处填且保持一致**：脚本的 `--domain` 写到服务器的 Traefik 配置（凭此签证书），`config.toml [preview].base_domain` 由 CI 读取派生 PR 子域名。脚本输出的 Next steps 会把正确值直接打印到 `config.toml` 模板里，复制粘贴保证两处一致。

> ⚠️ **Let's Encrypt 限速**：当前为 HTTP-01 + per-SNI 模式，每注册域名每周限约 50 张证书。PR 频繁时容易触限；高 PR 量场景需切换到 DNS-01 通配证书（要 DNS API token，本脚本未实现）。

#### `--apply-secrets` / `--apply-config` 详细行为

`--apply-secrets`：

- 检查 `gh` CLI 已安装并已登录；不存在则降级为打印模式
- **自动检测 `preview` environment 是否存在，没有则询问创建**（`gh api -X PUT repos/<repo>/environments/preview -F wait_timer=0`；创建后 sleep 5s 等 GitHub 同步，避免首次 set secret 报 EOF）
- 逐个 secret 跟你确认：`SERVER_*` 三个自动填好；`REGISTRY_USERNAME` 提示输入；`REGISTRY_PASSWORD` 用 `getpass`（输入不显示）；`POSTGRES_PASSWORD` 自动生成 32 字节随机串
- 每个 `gh secret set` 失败会自动重试一次（应对 env 刚创建完的同步 race）
- 已存在的 secret 默认不覆盖（明确问后才覆盖）
- 需要本地仓库有 remote 指向 GitHub

`--apply-config`：

- 从当前目录向上查找 `config.toml`
- 自动推断 `registry_namespace`（用 `gh repo view` 拿用户名）
- 显示 `unified diff`，跟你确认后写入；同时备份 `config.toml.bak`
- 已有 `[preview]` 段则替换，没有则追加
- 不可达（无 `config.toml`）时降级为打印模式

两个 flag 都支持降级：脚本检测到前提不满足（gh 未装 / 不在仓库 / 无 config.toml）会自动打印 copy-paste 指令，不会卡住。

> 💡 **如果不用 `--apply-secrets` 而直接 `gh secret set --env preview`**：会报 `404 Not Found`——必须先有 `preview` environment。手动创建：
>
> ```bash
> gh api -X PUT repos/<owner>/<repo>/environments/preview -F wait_timer=0
> # 等几秒
> gh secret set SERVER_HOST --env preview --body "..."
> ```

#### 不使用 `--apply-*` 时：执行脚本输出的指令

如果不用 `--apply-secrets` / `--apply-config`，脚本会在最后打印一段「Next steps」清单，**直接复制粘贴执行即可**，不用再查文档：

- DNS 记录：去域名注册商手工加 A 记录
- `SERVER_HOST` / `SERVER_USER` / `SERVER_SSH_KEY`：脚本已把正确值印好，复制粘贴执行即可
- `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` / `POSTGRES_PASSWORD`：占位符需替换为你的真实值
- `config.toml [preview]`：把脚本输出的片段粘贴到仓库 `config.toml`，commit 即可

#### 配置

1. 在 `config.toml [preview]` 中维护非敏感结构（`base_domain`、`project_slug`、`app_dir_root`、`url_scheme` 等）。
2. 在 GitHub Secrets（推荐挂到 `preview` environment）维护敏感值：`SERVER_HOST`、`SERVER_SSH_KEY`、`SERVER_USER`、`REGISTRY_USERNAME`、`REGISTRY_PASSWORD`、`POSTGRES_PASSWORD`。
3. 完成服务器初始化（见上节）。

#### 触发方式

- PR `opened` / `synchronize` / `reopened`
- PR 评论 `/deploy`
- `workflow_dispatch` 并传入 PR 号

#### 失败行为

预览部署是独立的非必需检查，失败不会阻塞 review/merge；失败信息写入 sticky 评论。

详细模板与脚本位于 `deploy/vps-traefik/`。
