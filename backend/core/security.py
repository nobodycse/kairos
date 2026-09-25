"""JWT 签发/校验与密码认证（design.md §1.1 / §3.1）。"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models import User

_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


async def authenticate(db: AsyncSession, username: str, password: str) -> bool:
    """查 users 表 + bcrypt 校验（种子用户由 Alembic 0002 迁移创建）。

    查询走 SQLAlchemy ORM 参数化表达式（编译为占位符绑定）。
    """
    stored = await db.scalar(select(User.password_hash).filter_by(username=username))
    if stored is None:
        # 不存在的用户也走一次哈希比较，避免响应时间侧信道暴露账号存在性
        bcrypt.checkpw(password.encode(), bcrypt.hashpw(b"dummy", bcrypt.gensalt()))
        return False
    return bcrypt.checkpw(password.encode(), stored.encode())


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


def decode_access_token(token: str) -> Optional[str]:
    """校验并解析 JWT，失败返回 None（不抛异常，由调用方决定 401）。"""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM]).get("sub")
    except jwt.InvalidTokenError:
        return None
