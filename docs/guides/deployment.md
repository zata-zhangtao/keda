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
   - 执行内容：`uv sync --all-groups --frozen`、`pre-commit`、本地测试集、`mkdocs build --strict`、发布包烟雾构建。
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

## TODO

- TODO: 补充容器化部署模板。
