# Comments And Docstrings Standards

## Google Style Docstrings

由于仓库使用 `mkdocstrings`，公共 Python API 必须使用 Google Style Docstrings。

最低要求：

- 模块需要模块级 docstring
- 公共类需要类 docstring
- 公共函数需要完整 docstring
- 公共函数 docstring 至少包含 `Args`、`Returns`，有异常时包含 `Raises`

推荐结构：

```python
def function_name(param1: str, param2: int) -> bool:
    """Executes a specific function.

    Args:
        param1 (str): Description of parameter 1.
        param2 (int): Description of parameter 2.

    Returns:
        bool: Description of the return value.
    """
```

## Inline Comments

只在以下情况写注释：

- 解释复杂逻辑的意图
- 说明边界条件或异常路径
- 说明为什么这样做，而不是代码表面上做了什么

避免这种低信息量注释：

```python
# Assign value to x
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
