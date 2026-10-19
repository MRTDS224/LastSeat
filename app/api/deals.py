import secrets
from decimal import Decimal
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db_session, get_redis
from app.models import FlightDeal
from app.schemas import (
    DealIngestionAck,
    DealPayload,
    DealResponse,
    PartnerDealIngestionRequest,
)
from app.services.ingestion import DealIngestionService, GDSPollerService

router = APIRouter(prefix="/api/v1/deals", tags=["Deals & Partner Ingestion"])
settings = get_settings()


async def verify_partner_api_key(
    x_partner_api_key: Annotated[str | None, Header(alias="X-Partner-API-Key")] = None,
) -> str:
    """Security dependency validating the Partner Charter / Tour Operator API key."""
    if not x_partner_api_key or not secrets.compare_digest(
        x_partner_api_key, settings.PARTNER_API_KEY
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Partner-API-Key header.",
        )
    return x_partner_api_key


@router.post(
    "",
    response_model=DealIngestionAck,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Inject a last-minute deal onto the market (Partner Simulator / Unified Endpoint)",
)
async def create_or_ingest_deal(
    payload: PartnerDealIngestionRequest | DealPayload,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis: Annotated[aioredis.Redis, Depends(get_redis)],
    x_partner_api_key: Annotated[str | None, Header(alias="X-Partner-API-Key")] = None,
) -> DealIngestionAck:
    """
    Unified market injection endpoint (`POST /api/v1/deals`).
    Accepts either a `PartnerDealIngestionRequest` or a canonical `DealPayload`.
    If `X-Partner-API-Key` is provided, validates it against `PARTNER_API_KEY`.
    """
    if x_partner_api_key and not secrets.compare_digest(
        x_partner_api_key, settings.PARTNER_API_KEY
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-Partner-API-Key header.",
        )

    normalized_deal: DealPayload = (
        payload.to_deal_payload()
        if isinstance(payload, PartnerDealIngestionRequest)
        else payload
    )
    ingestion_service = DealIngestionService(session=session, redis=redis)
    return await ingestion_service.ingest_deal(normalized_deal)


@router.post(
    "/partner",
    response_model=DealIngestionAck,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a partner charter / tour operator last-minute seat contingent (Source 2)",
)
async def ingest_partner_deal(
    payload: PartnerDealIngestionRequest,
    _api_key: Annotated[str, Depends(verify_partner_api_key)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis: Annotated[aioredis.Redis, Depends(get_redis)],
) -> DealIngestionAck:
    """
    Secured partner webhook endpoint.
    Normalizes the partner-specific payload into the canonical `DealPayload` model,
    upserts it into PostgreSQL, and publishes it to Redis Streams (`stream:flight_deals`)
    for immediate evaluation by the Autonomous Booking Engine.
    """
    normalized_deal: DealPayload = payload.to_deal_payload()
    ingestion_service = DealIngestionService(session=session, redis=redis)
    return await ingestion_service.ingest_deal(normalized_deal)


@router.post(
    "/ingest",
    response_model=DealIngestionAck,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Direct ingestion of a canonical DealPayload (Secured)",
)
async def ingest_canonical_deal(
    deal: DealPayload,
    _api_key: Annotated[str, Depends(verify_partner_api_key)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis: Annotated[aioredis.Redis, Depends(get_redis)],
) -> DealIngestionAck:
    """Directly ingest a normalized `DealPayload` into PostgreSQL and Redis Streams."""
    ingestion_service = DealIngestionService(session=session, redis=redis)
    return await ingestion_service.ingest_deal(deal)


@router.post(
    "/poll-now",
    response_model=DealIngestionAck,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger an immediate simulated GDS Poll cycle (Source 1)",
)
async def trigger_gds_poll_once(
    redis: Annotated[aioredis.Redis, Depends(get_redis)],
) -> DealIngestionAck:
    """Force an immediate GDS sandbox polling cycle and publish the normalized deal."""
    poller = GDSPollerService(redis=redis)
    return await poller.poll_once()


@router.get(
    "",
    response_model=list[DealResponse],
    summary="List normalized flight deals with optional route/status/price filtering",
)
async def list_deals(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    origin: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    destination: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    deal_status: Annotated[str | None, Query(alias="status")] = None,
    max_price: Annotated[Decimal | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[FlightDeal]:
    stmt = select(FlightDeal).order_by(FlightDeal.created_at.desc()).limit(limit)
    if origin:
        stmt = stmt.where(FlightDeal.origin == origin.upper())
    if destination:
        stmt = stmt.where(FlightDeal.destination == destination.upper())
    if deal_status:
        stmt = stmt.where(FlightDeal.status == deal_status.upper())
    if max_price is not None:
        stmt = stmt.where(FlightDeal.price <= max_price)

    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get(
    "/{deal_id}",
    response_model=DealResponse,
    summary="Retrieve a specific flight deal by its ID",
)
async def get_deal(
    deal_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FlightDeal:
    stmt = select(FlightDeal).where(FlightDeal.deal_id == deal_id)
    deal = (await session.execute(stmt)).scalar_one_or_none()
    if deal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Flight deal '{deal_id}' not found.",
        )
    return deal
