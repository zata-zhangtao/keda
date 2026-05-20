"""核心编排层（core）。

放置用例、Orchestrator、Planner、Memory 和领域契约。
负责业务规则和任务编排。

依赖规则：
    - 只能依赖抽象接口和纯业务模型（backend/core/shared/interfaces）
    - 不得直接依赖 backend/engines/ 或 backend/infrastructure/ 的具体实现
"""
