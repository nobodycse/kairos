"""JWT 签发与校验（单管理员，design.md §1.1 / §3.1）。"""
import hmac
from datetime import datetime, timedelta, timezone

import jwt

from core.config import settings

_ALGORITHM = "HS256"


def authenticate(username: str, password: str) -> bool:
    """Phase 0 单管理员 admin/admin123（design.md §3.1 stub 约定）。

    Phase 1 换成 users 表 + 密码哈希比对。
    """
    expected = b"admin:admin123"
    return hmac.compare_digest(f"{username}:{password}".encode(), expected)


def create_access_token(username: str) -> tuple[str, int]:
    """签发 JWT，返回 (token, 有效期秒数)。"""
    expires_in = settings.jwt_expire_hours * 3600
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM), expires_in


def decode_access_token(token: str) -> str | None:
    """校验并解析 JWT，失败返回 None（不抛异常，由调用方决定 401）。"""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM]).get("sub")
    except jwt.InvalidTokenError:
        return None
