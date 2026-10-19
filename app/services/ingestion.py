import asyncio
import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import redis.asyncio as aioredis
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import FlightDeal
from app.schemas import CabinClass, DealIngestionAck, DealPayload, DealSource, DealStatus

logger = logging.getLogger(__name__)
settings = get_settings()


class DealIngestionService:
    """
    Application service responsible for persisting normalized DealPayload objects
    into PostgreSQL (idempotent upsert) and publishing events to Redis Streams.
    """

    def __init__(self, session: AsyncSession, redis: aioredis.Redis) -> None:
        self.session = session
        self.redis = redis

    async def ingest_deal(self, deal: DealPayload) -> DealIngestionAck:
        """
        1. Upsert the normalized DealPayload into `flight_deals` table.
        2. Publish the normalized payload into the Redis Stream (`stream:flight_deals`).
        """
        now = datetime.now(UTC)
        initial_status = (
            DealStatus.ACTIVE.value if deal.seats_available > 0 else DealStatus.SOLD_OUT.value
        )

        stmt = (
            pg_insert(FlightDeal)
            .values(
                deal_id=deal.deal_id,
                source=deal.source.value,
                flight_number=deal.flight_number,
                airline_code=deal.airline_code,
                origin=deal.origin,
                destination=deal.destination,
                departure_at=deal.departure_at,
                arrival_at=deal.arrival_at,
                price=deal.price,
                currency=deal.currency,
                cabin_class=deal.cabin_class.value,
                seats_available=deal.seats_available,
                expires_at=deal.expires_at,
                raw_metadata=deal.raw_metadata,
                status=initial_status,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[FlightDeal.deal_id],
                set_={
                    "price": deal.price,
                    "seats_available": deal.seats_available,
                    "expires_at": deal.expires_at,
                    "raw_metadata": deal.raw_metadata,
                    "status": initial_status,
                    "updated_at": now,
                },
            )
        )

        await self.session.execute(stmt)
        await self.session.commit()

        # Publish event to Redis Stream for asynchronous consumption by the Booking Engine
        stream_msg_id = await self.redis.xadd(
            name=settings.REDIS_DEALS_STREAM,
            fields=deal.to_stream_message(),
            maxlen=10_000,
            approximate=True,
        )

        logger.info(
            "Deal ingested & published [deal_id=%s, source=%s, route=%s->%s, price=%s %s, seats=%d, stream_id=%s]",
            deal.deal_id,
            deal.source.value,
            deal.origin,
            deal.destination,
            deal.price,
            deal.currency,
            deal.seats_available,
            stream_msg_id,
        )

        return DealIngestionAck(
            status="ACCEPTED",
            deal_id=deal.deal_id,
            stream_message_id=str(stream_msg_id),
            source=deal.source,
            message="Deal normalized, persisted in PostgreSQL, and published to Redis Streams.",
        )


class GDSPollerService:
    """
    Simulated Inbound Adapter (Source 1) polling public GDS distribution APIs
    (Duffel / Amadeus Sandbox) for last-minute distressed seat inventory and
    normalizing raw offers into `DealPayload`.
    """

    SAMPLE_GDS_OFFERS: ClassVar[list[dict[str, Any]]] = [
        {
            "carrier": "AT",
            "flight_no": "AT850",
            "dep": "CMN",
            "arr": "ORY",
            "duration_hours": 3,
            "base_price": "39.00",
            "reference_price": 190.00,
            "cabin": CabinClass.ECONOMY,
            "seats": 2,
        },
        {
            "carrier": "FR",
            "flight_no": "FR4102",
            "dep": "RBA",
            "arr": "CDG",
            "duration_hours": 3,
            "base_price": "44.90",
            "reference_price": 165.00,
            "cabin": CabinClass.ECONOMY,
            "seats": 3,
        },
        {
            "carrier": "TO",
            "flight_no": "TO4810",
            "dep": "ORY",
            "arr": "LIS",
            "duration_hours": 2,
            "base_price": "49.90",
            "reference_price": 145.00,
            "cabin": CabinClass.ECONOMY,
            "seats": 2,  # Matches Claire's wildcard rule ORY -> *
        },
        {
            "carrier": "VY",
            "flight_no": "VY8001",
            "dep": "ORY",
            "arr": "BCN",
            "duration_hours": 2,
            "base_price": "55.00",
            "reference_price": 140.00,
            "cabin": CabinClass.ECONOMY,
            "seats": 2,
        },
        {
            "carrier": "AF",
            "flight_no": "AF006",
            "dep": "CDG",
            "arr": "JFK",
            "duration_hours": 8,
            "base_price": "265.00",
            "reference_price": 680.00,
            "cabin": CabinClass.ECONOMY,
            "seats": 1,  # 1 seat left -> triggers high-concurrency race between Alice & Bob!
        },
    ]

    def __init__(self, redis: aioredis.Redis, interval_seconds: int | None = None) -> None:
        self.redis = redis
        self.interval_seconds = interval_seconds or settings.POLLER_INTERVAL_SECONDS
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run_loop(), name="gds-poller-task")
            logger.info("GDS Poller started (interval=%ds).", self.interval_seconds)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("GDS Poller stopped cleanly.")

    def _build_normalized_deal_from_gds_raw(self, raw_offer: dict[str, Any]) -> DealPayload:
        """Transform a simulated raw Duffel/Amadeus offer into the unified DealPayload model."""
        now = datetime.now(UTC)
        departure_at = now + timedelta(hours=random.randint(3, 18))
        arrival_at = departure_at + timedelta(hours=int(raw_offer["duration_hours"]))
        expires_at = departure_at - timedelta(hours=1)

        short_uid = uuid.uuid4().hex[:8].upper()
        deal_id = f"GDS-{raw_offer['flight_no']}-{short_uid}"

        return DealPayload(
            deal_id=deal_id,
            source=DealSource.GDS_POLLER,
            flight_number=raw_offer["flight_no"],
            airline_code=raw_offer["carrier"],
            origin=raw_offer["dep"],
            destination=raw_offer["arr"],
            departure_at=departure_at,
            arrival_at=arrival_at,
            price=Decimal(str(raw_offer["base_price"])),
            currency="EUR",
            cabin_class=raw_offer["cabin"],
            seats_available=int(raw_offer["seats"]),
            expires_at=expires_at,
            raw_metadata={
                "gds_provider": random.choice(["DUFFEL_SANDBOX", "AMADEUS_TEST_API"]),
                "polled_at": now.isoformat(),
                "fare_basis": "LASTSEAT_FLASH",
                "reference_price": raw_offer.get("reference_price", 150.00),
            },
        )

    async def poll_once(self) -> DealIngestionAck:
        """Execute a single polling cycle and ingest one simulated last-minute GDS deal."""
        raw_offer = random.choice(self.SAMPLE_GDS_OFFERS)
        normalized_deal = self._build_normalized_deal_from_gds_raw(raw_offer)
        async with AsyncSessionLocal() as session:
            service = DealIngestionService(session=session, redis=self.redis)
            return await service.ingest_deal(normalized_deal)

    async def _run_loop(self) -> None:
        # Initial brief delay so the consumer group is ready
        await asyncio.sleep(2.0)
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in GDSPollerService polling cycle")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                continue

