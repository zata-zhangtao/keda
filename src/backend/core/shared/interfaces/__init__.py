"""抽象接口定义（interfaces）。

所有跨层依赖必须通过此处的抽象接口进行。
具体实现在 backend/engines/ 和 backend/infrastructure/ 中提供。

示例：
    - IModelClient：LLM 调用接口
    - IMemoryStore：记忆存储接口
    - ISkillRegistry：技能注册接口
"""
