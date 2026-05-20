# Schema 设计

本项目使用 SQLAlchemy 进行数据库持久化。

数据库连接配置和会话管理位于 `src/backend/infrastructure/persistence/database.py`。
具体的实体模型（Entity）和表结构应在各项目的 `src/backend/core/` 或
`src/backend/infrastructure/persistence/` 中按业务需求定义。

## 占位说明

模板仓库不预设任何业务表结构。
新项目创建后，请在此文档中补充实际的数据库 schema 设计。
