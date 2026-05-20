# Documentation Standards

## Docs Are Part Of The Product

`docs/` 和 `mkdocs.yml` 是代码库的一部分。文档错误按代码错误对待。

## Synchronization Rules

出现以下变化时，必须同步更新文档：

- 业务逻辑变化
- 配置项变化
- 公共函数签名变化
- 新增模块、流程或开发规范

## Navigation Maintenance

新增长期文档页时：

1. 创建对应的 Markdown 文件
2. 更新 `mkdocs.yml` 的 `nav`
3. 确保页面在文档站点中可发现

## API Documentation

不要在 Markdown 中手工复制 Python 函数说明。优先使用 `mkdocstrings`：

```markdown
::: backend.core.agent.KnowledgeRetrievalAgent
    handler: python
    options:
      members:
        - execute_task
```

## Markdown Encoding

- 所有 Markdown 文件必须使用 UTF-8 编码
- PowerShell 中读取 Markdown 时使用 `-Encoding utf8`

## Standards Hub Maintenance

如果规范变化与 AI 工作方式相关，优先更新 `docs/ai-standards/`，再同步各工具入口文件。
