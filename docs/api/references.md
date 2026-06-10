# API 参考

本页通过 `mkdocstrings` 自动渲染核心模块的公开 API。

## 基础设施模块

### `backend.infrastructure.config.settings`

::: backend.infrastructure.config.settings
    handler: python
    options:
      show_root_heading: true
      members_order: source

### `backend.infrastructure.logging.logger`

::: backend.infrastructure.logging.logger
    handler: python
    options:
      show_root_heading: true
      members_order: source

### `backend.infrastructure.persistence.database`

::: backend.infrastructure.persistence.database
    handler: python
    options:
      show_root_heading: true
      members_order: source

### `backend.infrastructure.helpers`

::: backend.infrastructure.helpers
    handler: python
    options:
      show_root_heading: true
      members_order: source

## Agent Runner 端点

### `GET /api/v1/agent-runner/status`

返回 Agent Runner 配置摘要与多仓库列表。该端点为**只读**，不会触发 label sync、agent 执行或任何 Git 变更操作。

响应示例：

```json
{
  "daemon_mode": false,
  "config": {
    "max_issues": 1,
    "default_agent": "auto",
    "max_recovery_attempts": 5,
    "recovery_retry_delay_seconds": 30,
    "ready_label": "agent/ready",
    "running_label": "agent/running",
    "review_label": "agent/review",
    "failed_label": "agent/failed",
    "base_branch": "main",
    "remote": "origin",
    "auto_merge": false,
    "forbidden_path_patterns": [".env", ".env.*", "secrets/*"]
  },
  "repositories": [
    {
      "repo_id": "keda",
      "display_name": "Keda",
      "enabled": true,
      "base_branch": "main",
      "remote": "origin"
    }
  ]
}
```

字段说明：

- `daemon_mode`：当前是否以 daemon 模式运行。
- `config`：全局 `[agent_runner]` 合并后的有效配置。
- `repositories`：已配置的仓库列表，每项包含 `repo_id`、`display_name`、`enabled`、`base_branch` 和 `remote`。

### `GET /api/v1/agent-runner/health`

返回运行器健康状态。该端点为**只读**，仅检测 `gh` CLI 是否可用。

响应示例：

```json
{
  "status": "healthy",
  "gh_cli_available": true
}
```

## 模型模块

### `backend.infrastructure.models.model_loader`

::: backend.infrastructure.models.model_loader
    handler: python
    options:
      show_root_heading: true
      members_order: source
