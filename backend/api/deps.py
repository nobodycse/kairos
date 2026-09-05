"""依赖注入：JWT 校验（architecture.md §6.1）。

豁免端点（/auth/login、/health、/webhooks/*，design.md §1.1）不挂本依赖；
SSE 端点因 EventSource 不能设请求头，单独走 query 参数 token。
"""
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.security import decode_access_token

bearer = HTTPBearer(auto_error=False)


async def require_user(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> str:
    """校验 Authorization: Bearer <JWT>，返回用户名。"""
    if cred is None:
        raise HTTPException(status_code=401, detail="未认证")
    username = decode_access_token(cred.credentials)
    if username is None:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    return username
