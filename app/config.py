from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized configuration loaded from environment variables or .env file."""

    APP_NAME: str = "Flight Smart Booking POC (LastSeat)"
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"

    # Security
    PARTNER_API_KEY: str = "sk-partner-charter-secret-key-2026"

    # Database (SQLAlchemy Async)
    DATABASE_URL: str = (
        "postgresql+asyncpg://lastseat:lastseat_password@localhost:5432/lastseat_db"
    )

    # Redis (Streams + Distributed Locking + Idempotency)
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_DEALS_STREAM: str = "stream:flight_deals"
    REDIS_CONSUMER_GROUP: str = "cg:booking_engine"
    REDIS_CONSUMER_NAME: str = "worker-1"
    REDIS_LOCK_TTL_SECONDS: int = 15
    REDIS_IDEMPOTENCY_TTL_SECONDS: int = 86400

    # GDS & Background Ingestion Poller
    POLLER_ENABLED: bool = True
    POLLER_INTERVAL_SECONDS: int = 12
    GDS_SIMULATED_FAILURE_RATE: float = 0.15
    GDS_SIMULATED_LATENCY_MS: int = 250

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
