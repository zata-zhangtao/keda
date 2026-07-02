# Comments And Docstrings Standards

## Google Style Docstrings

由于仓库使用 `mkdocstrings`，公共 Python API 必须使用 Google Style Docstrings。

### 语言

**所有内部注释、docstring、JSDoc/TSDoc 统一使用中文**。中文信息密度高，团队阅读效率更好。专有名词（如 DTO、HTTP、Redis、SQLAlchemy）可保留英文。

### 最低要求

- 模块需要模块级 docstring
- 公共类需要类 docstring
- 公共函数需要完整 docstring
- 公共函数 docstring 至少包含 `Args`、`Returns`，有异常时包含 `Raises`

### 推荐结构

```python
def function_name(param1: str, param2: int) -> bool:
    """执行特定功能。

    Args:
        param1 (str): 参数 1 的说明。
        param2 (int): 参数 2 的说明。

    Returns:
        bool: 返回值的说明。
    """
```

## Automated Checks

注释/docstring 规范由 lint 工具强制执行：

- **Python 后端**：启用 Ruff `pydocstyle` 规则（`D100`–`D107`、`D419`）。
  - `D100`–`D104`：模块、公共类、公共函数、包 `__init__` 必须有 docstring。
  - `D105`–`D107`：magic method、嵌套类、`__init__` 必须有 docstring。
  - `D419`：禁止空 docstring。
  - 配置位于 `pyproject.toml` 的 `[tool.ruff.lint]`。
  - 运行 `uv run ruff check src/backend tests hooks` 或 `just lint --full` 进行检查。
- **前端**：`frontend-admin` 与 `frontend-public` 均启用 `eslint-plugin-jsdoc`。
  - 公共函数、类、方法需要 JSDoc/TSDoc 说明。
  - 当前为 `warn` 级别，后续随代码补齐可升级为 `error`。
  - 运行 `cd frontend-admin && pnpm lint` / `cd frontend-public && pnpm lint`。

## Inline Comments

只在以下情况写注释：

- 解释复杂逻辑的意图
- 说明边界条件或异常路径
- 说明为什么这样做，而不是代码表面上做了什么

**内联注释必须使用中文**（专有名词除外）。

避免这种低信息量注释：

```python
# 把值赋给 x
x = value
```

## TODO Format

统一使用：

```python
# TODO: Description
```

## UTF-8 File I/O

涉及文件读写时，必须显式写出 `encoding="utf-8"`：

```python
with open("README.md", "r", encoding="utf-8") as readme_file:
    readme_content = readme_file.read()
```

同样适用于：

- `pathlib.Path.read_text(encoding="utf-8")`
- `pathlib.Path.write_text(..., encoding="utf-8")`
- `.txt` / `.json` / `.md` / `.log` 等文本文件

## Windows Notes

- 不要依赖系统默认编码
- 控制台输出应考虑 Windows 终端的兼容性
