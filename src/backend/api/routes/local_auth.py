"""Local single-operator session endpoints.

本应用按「本机单用户部署」信任边界运行：管理终端与监控面板都假设
访问者就是机器的所有者，因此这里不做真实认证，只提供一个固定的
本地 operator 会话，使前端的会话守卫（``RequireSession``）能够放行。

若未来需要真实多用户认证，应整体替换本模块而不是在其上叠加逻辑。
"""

from __future__ import annotations

import getpass

from fastapi import APIRouter, Response

router = APIRouter(tags=["auth"])

_LOCAL_OPERATOR_USER_ID = "local-operator"


def _build_local_session() -> dict:
    """Return the fixed local operator session payload."""
    try:
        operator_name = getpass.getuser()
    except Exception:  # noqa: BLE001 - environment without a resolvable user.
        operator_name = _LOCAL_OPERATOR_USER_ID
    return {
        "user_id": _LOCAL_OPERATOR_USER_ID,
        "display_name": operator_name,
        "email": f"{operator_name}@localhost",
    }


@router.get("/auth/me")
def get_current_session() -> dict:
    """Return the local operator session (always authenticated)."""
    return _build_local_session()


@router.post("/auth/login")
def login() -> dict:
    """Accept any credentials and return the local operator session."""
    return _build_local_session()


@router.post("/auth/logout", status_code=204)
def logout() -> Response:
    """No-op logout for the local single-operator deployment."""
    return Response(status_code=204)
