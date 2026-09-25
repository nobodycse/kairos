"""集中配置：pydantic-settings 读 .env（design.md §7）。

Phase 0 仅 JWT 相关实际生效；其余变量在此定义好，
Phase 1 接数据库/Redis/K8s/LLM 时直接使用。
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: int = 30
    database_url: str = "postgresql+asyncpg://kairos:kairos@localhost:5432/kairos"
    redis_url: str = "redis://localhost:6379/0"
    # 生产必须通过 .env 覆盖为随机 32+ 字符
    jwt_secret: str = "kairos-dev-secret-change-me"
    jwt_expire_hours: int = 24
    # 初始 admin 密码（Alembic 种子迁移用，服务器 .env 必须覆盖）
    admin_initial_password: str = "admin123"
    kubeconfig: str = "/kubeconfig/k3s.yaml"
    demo_namespace: str = "demo"

    model_config = {"env_file": ".env", "case_sensitive": False}


settings = Settings()
