"""Infrastructure persistence package.

子模块按需导入，避免在包导入时产生副作用：

- ``database``：SQLAlchemy + 应用主库（导入即创建 engine，只在
  真正需要数据库的入口导入，如 ``alembic/env.py``）。
- ``console_store``：管理终端运行历史 / 审计的本地 SQLite 存储。
"""
