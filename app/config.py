from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnterpriseSettings(BaseSettings):
    # Core Security
    secret_key: str = Field(..., alias="SECRET_KEY")
    algorithm: str = Field("HS256", alias="ALGORITHM")
    access_token_expire_minutes: int = Field(60, alias="ACCESS_TOKEN_EXPIRE_MINUTES")

    # Database
    # SQLite remains supported for development; production Compose uses PostgreSQL.
    database_url: str = Field(
        "sqlite+aiosqlite:///./server_manager.db",
        alias="DATABASE_URL",
    )
    database_pool_size: int = Field(20, alias="DATABASE_POOL_SIZE")
    database_max_overflow: int = Field(40, alias="DATABASE_MAX_OVERFLOW")
    database_pool_timeout: int = Field(30, alias="DATABASE_POOL_TIMEOUT")
    database_pool_recycle: int = Field(1800, alias="DATABASE_POOL_RECYCLE")

    # Secure Admin Bootstrapping
    first_superuser: str | None = Field(None, alias="FIRST_SUPERUSER")
    first_superuser_password: str | None = Field(None, alias="FIRST_SUPERUSER_PASSWORD")

    # Strict CORS
    cors_origins: str = Field(
        "http://localhost:3000,http://localhost:8000",
        alias="CORS_ORIGINS",
    )

    # Deployment
    app_version: str = Field("2.0.0", alias="APP_VERSION")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = EnterpriseSettings()
