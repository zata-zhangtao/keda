# Code Reuse Guardrails

本页定义 AI 与开发者修改代码时必须遵守的防重复规则。目标不是一次性清理历史重复，而是让新增和修改代码优先复用、参数收敛、职责单点化。

## 复用优先

新增或修改功能前，先搜索现有实现：

- Python 优先用 `rg` 搜索 `src/backend/core/`、`src/backend/api/`、`src/backend/engines/`
- 前端优先搜索 `frontend/src/lib/`、`frontend/shared/`、`frontend/src/hooks/`
- 业务规则优先复用 `src/backend/core/`
- 前端纯工具、格式化、API 客户端优先复用 `frontend/src/lib/` 或 `frontend/shared/`

禁止复制粘贴已有代码后微调。发现逻辑重复率明显超过 50% 时，优先直接调用已有函数；如果调用方向不合适，先提取公共业务规则或纯转换函数，再由调用方复用。

## 复用层次

复用前先判断要复用的是业务规则、数据转换，还是数据操作。

| 类型 | 示例 | 规则 |
|------|------|------|
| 业务规则/校验 | `is_valid_port_code()`、`can_approve()` | 必须复用，集中管理 |
| 数据转换/映射 | `normalize_port_code()`、`to_dto()` | 应该复用，保持纯函数 |
| 数据查询/加载 | `load_all_ports()`、`get_db_session()` | 禁止被业务代码直接当规则复用 |
| 流程编排 | `run_recommendation_pipeline()` | 谨慎复用，优先复用其中的规则片段 |

具体要求：

- 判断"是否合法/有效/满足条件"的逻辑必须提取为 `is_`、`can_` 或 `validate_` 开头的命名函数
- 业务代码不得直接调用"加载全量数据"的函数作为判断依据；调用方应只依赖规则函数，规则函数内部决定缓存、预计算或加载方式
- 同一判断逻辑在当前变更中出现 2 次以上，即使只有两行，也必须提取为命名函数
- `src/backend/core/` 是后端业务规则唯一可信源；`src/backend/api/` 只做入口适配、参数校验、DTO 转换和用例调用

## 参数收敛

参数游行是同一组相关参数沿调用链层层展开传递。新增或修改函数时遵守以下规则：

- 函数参数超过 4 个时，收敛到 `dataclass`、Pydantic Model、TypedDict 或已有上下文对象
- 3 个及以上参数总是一起出现时，提取为值对象或上下文对象
- 两个参数总是同时出现时，优先检查是否已经具备命名清晰的对象边界
- 跨层状态用 `RecommendationContext`、`FilterOptions`、`DateRange`、`PortPair`、`CostConfig` 这类对象传递
- 不透明实参，如布尔值、`None`、数字字面量，必须使用 Python 关键字参数或 keyword-only 参数

推荐：

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class DateRange:
    start_date: str
    end_date: str


def fetch_costs(*, route_date_range: DateRange, allow_partial: bool) -> list[float]:
    ...


cost_values = fetch_costs(
    route_date_range=DateRange(start_date="2026-01-01", end_date="2026-01-31"),
    allow_partial=True,
)
```

不推荐：

```python
cost_values = fetch_costs("2026-01-01", "2026-01-31", True)
```

## 文件与模块

文件大小采用分层阈值：

- 目标：单个 `.py` 文件非空行少于 500 行
- 上限：新增或重写模块不应超过 800 行
- 过渡：`hooks/check_max_file_lines.py` 仍以 1000 行 warn-only 兼容历史文件

模块拆分规则：

- 不创建只被引用一次的小 helper；只有当抽象减少真实复杂度、复用真实存在或匹配既有模式时才提取
- 不在同一文件里无限追加逻辑；接近 500 行时先判断是否需要新子模块
- 向 `src/backend/core/` 添加代码前，必须确认没有更合适的已有模块、能力层或共享前端目录
- 如果两个模块出现重复，第一反应是抽象层级错了，而不是再写一份

## Hook 防线

重复检测建议通过 dedicated commands 运行，不属于默认 commit hook 或默认 `just lint`：

- `jscpd`：跨 Python / TypeScript / JavaScript 的复制粘贴级重复检测
- `pylint duplicate-code`：Python 结构级重复检测，只启用 `duplicate-code`

这些 hook 使用候选文件和比较语料分离的策略：候选文件来自当前变更，`jscpd` 比较 `src/backend/` 与 `frontend/`，`pylint duplicate-code` 比较 `src/backend/`。`src/backend/core/`、`frontend/src/lib/` 和 `frontend/shared/` 必须始终作为优先复用目录参与判断。历史重复不会因为全量 lint 被一次性阻断，但新增或修改文件触达重复时必须修复。

## AI 编码自检清单

每次完成代码修改后，AI 必须在回复末尾显式回答：

- [ ] 我没有复制粘贴已有代码后微调（复用优先于复制）
- [ ] 我复用的是业务规则（`is_` / `can_` / `validate_`）而非数据加载操作（`load_` / `get_` / `fetch_`）
- [ ] 我的函数参数不超过 4 个，成组参数已收敛到对象
- [ ] 我没有在同一文件里无限追加逻辑（当前文件非空行少于 800 行，目标少于 500 行）
- [ ] 我的 import 方向符合四层架构（`api -> core -> engines -> infrastructure`）
- [ ] 我的变量名解释了数据来源和状态（没有 `data` / `item` / `res` / `tmp`）
- [ ] 我运行了 `just lint --reuse` 和 `just lint --full`（或等效门禁），且全绿通过，在跑任何测试之前
- [ ] 我同步更新了 `docs/` 和 `mkdocs.yml`（如有文档影响）
- [ ] 我没有创建只被引用一次的小 helper 函数

任何未勾选项目必须在交付前修复，或在最终回复中说明原因与风险。
