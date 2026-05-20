"""请求接入层（api）。

负责接收 HTTP、WebSocket、CLI 等外部请求。
只做参数校验、DTO 转换和用例调用，不写业务规则。

依赖规则：
    - 只能向内调用 backend/core/ 暴露的用例和 DTO
    - 不得直接依赖 backend/engines/ 或 backend/infrastructure/
"""
