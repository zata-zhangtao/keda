# Naming Standards

本仓库采用 AI-Native 命名规范，核心原则之一就是 **Fully Qualified Naming**，目标是让数据来源、状态和职责一眼可见。

## Core Rules

- 拒绝 `data`、`item`、`res`、`obj` 这类泛型变量名
- 变量名应包含来源、类型或状态
- 尽量让名称本身就解释数据流，而不是依赖上下文猜测

## Preferred Style

- 好例子：`raw_user_query_text`
- 好例子：`parsed_model_config_dict`
- 好例子：`final_agent_response_obj`
- 差例子：`data`
- 差例子：`result`
- 差例子：`tmp`

## SSA And Immutability

优先使用 Single Static Assignment 风格：

- 每一步产生新变量名
- 不反复覆盖同一个中间变量
- 让解析、校验、转换、调用和输出各有自己的名字

更推荐：

```python
sanitized_search_intent: str = self._sanitize_input(raw_user_query_text)
raw_tool_outputs_list: list[ExternalToolOutput] = self._call_search_tool(sanitized_search_intent)
```

不推荐：

```python
data = self._sanitize_input(data)
data = self._call_search_tool(data)
```

## Types As Prompts

- 为函数参数和返回值添加类型注解
- 对结构化数据优先使用 Pydantic 模型或显式类型
- 当名称仍不足以表达约束时，用类型把约束补齐

## Scope Notes

- 本页主要约束 Python 与通用工程语义
- `tests/playwright-e2e/` 是独立 TypeScript 包，遵循 TypeScript 社区命名习惯，不强制照搬 Python SSA 变量命名风格
