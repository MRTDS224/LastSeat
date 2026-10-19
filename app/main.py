import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy import text

from app.api.deals import router as deals_router
from app.api.rules import router as rules_router
from app.config import get_settings
from app.database import AsyncSessionLocal, Base, close_redis, engine, init_redis
from app.services.booking_engine import BookingEngineStreamWorker
from app.services.ingestion import GDSPollerService

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifecycle manager:
    - Connects to PostgreSQL and ensures ORM tables exist.
    - Initializes Redis connection pool and Consumer Group.
    - Starts the background BookingEngineStreamWorker and simulated GDSPollerService.
    - Performs clean shutdown of workers and connection pools on exit.
    """
    logger.info("Starting %s [env=%s]...", settings.APP_NAME, settings.ENVIRONMENT)

    # Ensure database schema is initialized
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    redis = await init_redis()
    await redis.ping()

    # Start Redis Stream Consumer Worker (Autonomous Booking Engine)
    booking_worker = BookingEngineStreamWorker(redis=redis)
    await booking_worker.ensure_consumer_group()
    booking_worker.start()

    # Start Simulated GDS Poller (Source 1) if enabled
    poller: GDSPollerService | None = None
    if settings.POLLER_ENABLED:
        poller = GDSPollerService(
            redis=redis,
            interval_seconds=settings.POLLER_INTERVAL_SECONDS,
        )
        poller.start()

    app.state.booking_worker = booking_worker
    app.state.gds_poller = poller

    try:
        yield
    finally:
        logger.info("Shutting down background workers and connection pools...")
        if poller is not None:
            await poller.stop()
        await booking_worker.stop()
        await close_redis()
        await engine.dispose()
        logger.info("Shutdown complete.")


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description=(
        "High-Concurrency Event-Driven Platform for Last-Minute Flight Deal Ingestion "
        "& Autonomous Agent Purchasing (FastAPI, Redis Streams, Distributed Locks, PostgreSQL Async)."
    ),
    lifespan=lifespan,
)

app.include_router(deals_router)
app.include_router(rules_router)


@app.get("/health", tags=["Observability"])
async def healthcheck() -> dict[str, Any]:
    """Liveness and readiness probe verifying PostgreSQL and Redis Streams health."""
    db_ok = False
    redis_ok = False
    stream_length = 0

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        logger.exception("Database healthcheck failed")

    try:
        redis = await init_redis()
        await redis.ping()
        redis_ok = True
        stream_length = await redis.xlen(settings.REDIS_DEALS_STREAM)
    except Exception:
        logger.exception("Redis healthcheck failed")

    return {
        "status": "healthy" if (db_ok and redis_ok) else "degraded",
        "service": settings.APP_NAME,
        "postgres_connected": db_ok,
        "redis_connected": redis_ok,
        "redis_stream": settings.REDIS_DEALS_STREAM,
        "stream_backlog": stream_length,
        "poller_enabled": settings.POLLER_ENABLED,
    }

