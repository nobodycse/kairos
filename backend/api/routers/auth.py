"""登录与 JWT 签发（design.md §3.1）。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.security import authenticate, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login", summary="登录签发 JWT（无鉴权）")
def login(req: LoginRequest):
    if not authenticate(req.username, req.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token, expires_in = create_access_token(req.username)
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in}
