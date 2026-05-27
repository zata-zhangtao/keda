# PRD: Agent Runner Local Config - Use Pydantic Settings Model

## 1. Introduction & Goals

**Problem**: `build_repository_local_config_text()` 在 `repository_local.py` 中使用硬编码 TOML 字符串拼接，生成的 `.iar.toml` 格式与 `AgentRunnerLocalSettings` Pydantic 模型定义不一致。后续对配置模型的任何修改都不会同步到 `build_repository_local_config_text()` 中，导致配置生成和配置解析出现偏差。

**Goal**: 使用 `AgentRunnerLocalSettings` Pydantic 模型作为唯一真实数据源，通过 Pydantic 序列化生成 `.iar.toml`，保持配置生成和解析的一致性。

### Realistic Validation

- [ ] `build_repository_local_config_text()` 输出与 `AgentRunnerLocalSettings` 模型定义的字段完全一致
- [ ] 所有字段的序列化格式符合 TOML 规范（无 Python 特定的 JSON 格式）
- [ ] `just test` 通过

---

## 2. Requirement Shape

| 元素 | 内容 |
|------|------|
| **Actor** | IAR CLI (`iar init`) |
| **Trigger** | 用户执行 `iar init` 生成 `.iar.toml` |
| **Expected Behavior** | 生成的 `.iar.toml` 使用 `AgentRunnerLocalSettings` Pydantic 模型序列化，格式与模型定义完全一致 |
| **Explicit Boundary** | 仅修改 `repository_local.py` 的配置生成逻辑，不修改 `AgentRunnerLocalSettings` 模型定义本身 |

---

## 3. Repository Context And Architecture Fit

### 3.1 Current Relevant Modules

| 文件 | 作用 | 改动点 |
|------|------|--------|
| `src/backend/engines/agent_runner/repository_local.py` | 生成 `.iar.toml` 的入口 | 修改 `build_repository_local_config_text()` 使用 Pydantic 序列化 |
| `src/backend/infrastructure/config/settings.py` | 包含 `AgentRunnerLocalSettings` 模型 | 无改动 |

### 3.2 Existing Architecture Pattern

- `AgentRunnerLocalSettings` 是 Pydantic model，用于解析和验证 `.iar.toml`
- `settings_to_toml_string()` 负责将 settings 序列化回 TOML
- 使用 `tomli_w` 库生成 TOML 格式

---

## 4. Recommendation

### 4.1 Implementation

1. 在 `repository_local.py` 中新增 `settings_to_toml_string()` 函数：
   - 调用 `settings.model_dump()`
   - 过滤掉所有 `None` 值
   - 使用 `tomli_w.dumps()` 生成 TOML 字符串
   - 外层包装 `{"agent_runner": data}` 以匹配 `.iar.toml` 结构

2. 新增 `_filter_none_dict()` 辅助函数：
   - 递归过滤掉所有 `None` 值
   - 保持字典和列表结构

3. 修改 `build_repository_local_config_text()`：
   - 移除手动的 TOML 字符串拼接
   - 改用 `AgentRunnerLocalSettings` 模型构造配置
   - 调用 `settings_to_toml_string()` 序列化

4. 删除 `_toml_quote()` 函数

5. 在 `pyproject.toml` 添加 `tomli-w>=1.0.0` 依赖

### 4.2 Why This Is The Best Fit

- 使用 Pydantic 模型作为唯一数据源，避免重复定义
- `model_dump()` + `tomli_w.dumps()` 是标准 Python 序列化模式
- 过滤 None 值确保生成的 TOML 不包含空字段

---

## 5. Implementation Guide

### 5.1 Code Changes

```python
def settings_to_toml_string(settings: AgentRunnerLocalSettings) -> str:
    """Serialize AgentRunnerLocalSettings to formatted TOML string."""

    data = _filter_none_dict(settings.model_dump())
    # Wrap in [agent_runner] to match .iar.toml structure
    wrapped = {"agent_runner": data}
    return tomli_w.dumps(wrapped)


def _filter_none_dict(data: dict) -> dict:
    """Recursively remove None values from dict for TOML serialization."""

    def filter_value(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, dict):
            return filter_dict(v)
        if isinstance(v, list):
            return [filter_value(item) for item in v]
        return v

    def filter_dict(d: dict) -> dict:
        result = {k: filter_value(v) for k, v in d.items() if v is not None}
        return result if result else {}

    return filter_dict(data)
```

### 5.2 Dependencies

```toml
# pyproject.toml
[project]
dependencies = [
    ...
    "tomli-w>=1.0.0",
]
```

---

## 6. Definition Of Done

- `build_repository_local_config_text()` 使用 Pydantic 模型序列化
- `_toml_quote()` 函数被移除
- `settings_to_toml_string()` 和 `_filter_none_dict()` 函数存在
- `tomli-w` 依赖已添加到 `pyproject.toml`
- `just test` 通过

---

## 7. Acceptance Checklist

### Architecture Acceptance

- [x] 配置生成使用 `AgentRunnerLocalSettings` 作为数据源
- [x] `settings_to_toml_string()` 函数正确序列化 Pydantic 模型
- [x] `_filter_none_dict()` 正确过滤 None 值

### Behavior Acceptance

- [x] 生成的 `.iar.toml` 格式与 `AgentRunnerLocalSettings` 定义一致
- [x] 不包含 Python 特定格式（如 JSON 风格的字符串）
- [x] `just test` 通过

### Dependencies Acceptance

- [x] `tomli-w>=1.0.0` 已添加到 `pyproject.toml`
- [x] `uv.lock` 已更新

---

## 8. Functional Requirements

**FR-1**: `build_repository_local_config_text()` 使用 `AgentRunnerLocalSettings` 模型构造配置，不直接拼接 TOML 字符串。

**FR-2**: `settings_to_toml_string()` 正确序列化 Pydantic 模型为 TOML 格式。

**FR-3**: `_filter_none_dict()` 递归过滤掉所有 `None` 值。

**FR-4**: 生成的 `.iar.toml` 不包含值为 `None` 的字段。

---

## 9. Non-Goals

- 不修改 `AgentRunnerLocalSettings` 模型定义
- 不修改配置解析逻辑（`AgentRunnerLocalSettings.from_toml()` 等）
- 不新增配置验证规则

---

## 10. Decision Log

| ID | Decision | Chosen | Rejected | Rationale |
|----|----------|--------|---------|-----------|
| D-01 | 配置序列化方式 | Pydantic model_dump() + tomli_w.dumps() | 手动拼接 TOML | Pydantic 模型是唯一数据源，手动拼接会导致不一致 |
| D-02 | None 值处理 | 过滤掉所有 None 值 | 保留 None 值为 null | TOML 中 null 不可读，保留默认值更清晰 |
| D-03 | TOML 库选择 | tomli_w | toml | tomli_w 是纯 Python 实现，与 tomli 配套使用 |

---

## 11. Validation Evidence

本次修改已完成并通过以下验证：

```bash
# 依赖验证
uv sync
# ✓ tomli-w 1.2.0 已添加到 uv.lock

# 测试验证
just test
# ✓ 所有测试通过
```

---

## 12. Implementation Date

2026-05-27
