"""平台能力引擎层（engines）。

放置 skills、RAG、registry 等可插拔能力。
实现 backend/core/shared/interfaces 定义的端口契约。

依赖规则：
    - 可以依赖 backend/core/shared/interfaces 中的抽象
    - 可以依赖 backend/infrastructure/ 的具体实现
    - 不得包含业务编排逻辑
"""
