#!/usr/bin/env python3
"""Generate a standard README.md for a new project."""

import sys
from pathlib import Path


def generate_readme(project_name: str, output_path: Path) -> None:
    """Generate a standard README.md file.

    Args:
        project_name: The name of the new project.
        output_path: The path where README.md will be written.
    """
    readme_content = f"""# {project_name}

> 项目描述：请在此处添加项目的简要描述。

## 快速开始

```bash
just dev
```

`just dev` 会执行完整依赖同步并安装 pre-commit hooks，适合作为开发环境的一键启动命令。

## 安装说明

### 前置要求

- Python >= 3.14
- [uv](https://docs.astral.sh/uv/) - Python 包管理器
- [just](https://github.com/casey/just) - 命令运行器

### 安装步骤

1. **克隆仓库**
   ```bash
   git clone <repository-url>
   cd {project_name}
   ```

2. **安装依赖**
   ```bash
   just dev
   ```

## 使用方法

```bash
# 运行主程序
just run

# 运行测试
just test

# 启动文档服务
just docs-serve
```

## 配置说明

配置文件位于 `config.toml`，敏感信息请使用 `.env` 文件管理。

主要配置项：
- `app.name` - 应用名称
- `app.log_level` - 日志级别
- `database.*` - 数据库配置
- `chat_model.*` - 聊天模型配置

## 开发指南

### 代码规范

- 使用 Google Style Docstrings
- 遵循 AI-Native 代码模式（详见 `AGENTS.md`）
- 提交前会自动运行 pre-commit hooks

### 常用命令

| 命令 | 说明 |
|------|------|
| `just dev` | 安装开发环境 |
| `just run` | 运行主程序 |
| `just test` | 运行测试 |
| `just docs-serve` | 启动文档服务 |
| `just clean` | 清理缓存文件 |

## 许可证

[请添加许可证信息]
"""

    output_path.write_text(readme_content, encoding="utf-8")
    print(f"Created README.md for {project_name}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python generate_readme.py <project_name> <output_path>")
        sys.exit(1)

    project_name = sys.argv[1]
    output_path = Path(sys.argv[2])
    generate_readme(project_name, output_path)
