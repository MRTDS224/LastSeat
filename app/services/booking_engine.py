import asyncio
import logging
import uuid
from datetime import UTC, datetime

import redis.asyncio as aioredis
from redis.exceptions import ResponseError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import AgentRule, BookingTransaction, FlightDeal
from app.schemas import BookingStatus, DealPayload, DealStatus
from app.services.gds_client import GDSBookingClient, GDSIssuanceError

logger = logging.getLogger(__name__)
settings = get_settings()

# Atomic Lua script to release a Redis distributed lock ONLY if the lock token matches
UNLOCK_LUA_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class DistributedLockManager:
    """
    High-concurrency Redis Distributed Lock & Idempotency Coordinator.
    Uses `SET key token NX EX ttl` for mutual exclusion and safe Lua token verification on unlock.
    """

    def __init__(self, redis: aioredis.Redis) -> None:
        self.redis = redis

    @staticmethod
    def deal_lock_key(deal_id: str) -> str:
        return f"lock:deal:{deal_id}"

    @staticmethod
    def idempotency_key(deal_id: str, user_id: str) -> str:
        return f"idempotency:booking:{deal_id}:{user_id}"

    async def acquire_deal_lock(self, deal_id: str, ttl_seconds: int | None = None) -> str | None:
        """
        Attempt to acquire an exclusive lock on a deal (`SET lock:deal:{deal_id} <token> NX EX <ttl>`).
        Returns the unique lock owner token if acquired, or None if already locked.
        """
        ttl = ttl_seconds or settings.REDIS_LOCK_TTL_SECONDS
        token = uuid.uuid4().hex
        acquired = await self.redis.set(
            name=self.deal_lock_key(deal_id),
            value=token,
            nx=True,
            ex=ttl,
        )
        return token if acquired else None

    async def release_deal_lock(self, deal_id: str, token: str) -> bool:
        """Atomically release the distributed lock if still owned by `token`."""
        res = await self.redis.eval(
            UNLOCK_LUA_SCRIPT,
            1,
            self.deal_lock_key(deal_id),
            token,
        )
        return bool(res == 1)

    async def claim_idempotency(
        self, deal_id: str, user_id: str, ttl_seconds: int | None = None
    ) -> bool:
        """
        Guarantee idempotence for a `(deal_id, user_id)` pair via `SET ... NX EX`.
        Returns True if this is the first time this user is attempting to book this deal.
        """
        ttl = ttl_seconds or settings.REDIS_IDEMPOTENCY_TTL_SECONDS
        claimed = await self.redis.set(
            name=self.idempotency_key(deal_id, user_id),
            value="PROCESSING",
            nx=True,
            ex=ttl,
        )
        return bool(claimed)

    async def mark_idempotency_status(self, deal_id: str, user_id: str, status: str) -> None:
        """Update the idempotency record value while keeping TTL."""
        await self.redis.set(
            name=self.idempotency_key(deal_id, user_id),
            value=status,
            keepttl=True,
        )


class BookingEngineService:
    """
    Core Domain Service implementing:
    1. Matching incoming `DealPayload` events against active `AgentRule` criteria.
    2. High-concurrency seat reservation using Redis Distributed Locks (`SET ... NX EX`)
       + PostgreSQL row-level locking (`SELECT ... FOR UPDATE`).
    3. Idempotency enforcement via `deal_id + user_id`.
    4. Saga Orchestration with Compensating Transactions when GDS ticket issuance fails.
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        gds_client: GDSBookingClient | None = None,
    ) -> None:
        self.redis = redis
        self.lock_manager = DistributedLockManager(redis)
        self.gds_client = gds_client or GDSBookingClient()

    async def find_matching_rules(
        self, session: AsyncSession, deal: DealPayload
    ) -> list[AgentRule]:
        """
        Find all active autonomous buying rules matching the normalized deal:
        - `auto_purchase_enabled == True`
        - `origin == deal.origin`
        - `destination == deal.destination` OR `destination IS NULL` (wildcard)
        - `max_price >= deal.price`
        - `preferred_cabin == deal.cabin_class`
        - Optional departure time window (`earliest_departure` / `latest_departure`)
        """
        stmt = (
            select(AgentRule)
            .where(
                AgentRule.auto_purchase_enabled.is_(True),
                AgentRule.origin == deal.origin,
                or_(
                    AgentRule.destination == deal.destination,
                    AgentRule.destination.is_(None),
                ),
                AgentRule.max_price >= deal.price,
                AgentRule.preferred_cabin == deal.cabin_class.value,
            )
            .order_by(AgentRule.max_price.desc(), AgentRule.created_at.asc())
        )
        result = await session.execute(stmt)
        candidates = list(result.scalars().all())

        # Filter optional departure time window in domain layer
        matched: list[AgentRule] = []
        for rule in candidates:
            if rule.earliest_departure and deal.departure_at < rule.earliest_departure:
                continue
            if rule.latest_departure and deal.departure_at > rule.latest_departure:
                continue
            matched.append(rule)
        return matched

    async def process_deal_event(self, deal: DealPayload) -> list[BookingTransaction]:
        """
        Evaluate a newly ingested deal against all matching user rules and execute
        autonomous purchases concurrently to demonstrate anti-collision & distributed locking.
        """
        now = datetime.now(UTC)
        if deal.expires_at <= now:
            logger.info("Deal %s is already expired; skipping matching.", deal.deal_id)
            return []

        async with AsyncSessionLocal() as session:
            matching_rules = await self.find_matching_rules(session, deal)

        if not matching_rules:
            logger.info(
                "No matching autonomous agent rules for deal %s (%s->%s @ %s %s).",
                deal.deal_id,
                deal.origin,
                deal.destination,
                deal.price,
                deal.currency,
            )
            return []

        logger.info(
            "Found %d matching agent rule(s) for deal %s (%d seat(s) available). Triggering concurrent autonomous agents.",
            len(matching_rules),
            deal.deal_id,
            deal.seats_available,
        )

        # Launch candidate rule bookings concurrently to exercise distributed locking & anti-collision
        tasks = [
            self.attempt_autonomous_booking(deal=deal, rule=rule) for rule in matching_rules
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        completed_transactions: list[BookingTransaction] = []
        for res in results:
            if isinstance(res, BookingTransaction):
                completed_transactions.append(res)
            elif isinstance(res, Exception):
                logger.error("Unhandled exception during booking attempt: %s", res)
        return completed_transactions

    async def attempt_autonomous_booking(
        self,
        deal: DealPayload,
        rule: AgentRule,
        max_lock_retries: int = 8,
        retry_backoff_sec: float = 0.08,
    ) -> BookingTransaction | None:
        """
        Execute the Saga workflow for a single `(deal, rule)` pair:
        1. Check & claim Redis Idempotency key `deal_id:user_id` (`SET ... NX EX`).
        2. Acquire Redis Distributed Lock on `deal_id` (`SET lock:deal:{deal_id} <token> NX EX <ttl>`).
        3. Saga Step 1 (Local Reservation): Decrement seat in PostgreSQL & create `PENDING_GDS` transaction.
        4. Saga Step 2 (Remote GDS Issuance): Call `GDSBookingClient.issue_ticket(...)`.
        5. Saga Step 3 (Confirm or Compensate):
           - On success: transition transaction to `CONFIRMED` with PNR.
           - On failure: execute **Compensation** (restore seat count, set deal back to `ACTIVE`,
             transition transaction to `COMPENSATED_FAILED`, release lock).
        """
        idem_key_str = f"{deal.deal_id}:{rule.user_id}"

        # 1. Fast distributed idempotency guard
        is_first_attempt = await self.lock_manager.claim_idempotency(
            deal_id=deal.deal_id, user_id=rule.user_id
        )
        if not is_first_attempt:
            logger.info(
                "Idempotency guard prevented duplicate booking attempt [key=%s].",
                idem_key_str,
            )
            return None

        # 2. Acquire Redis Distributed Lock (`SET lock:deal:{deal_id} token NX EX ttl`)
        lock_token: str | None = None
        for attempt in range(1, max_lock_retries + 1):
            lock_token = await self.lock_manager.acquire_deal_lock(deal.deal_id)
            if lock_token is not None:
                break
            await asyncio.sleep(retry_backoff_sec * attempt)

        if lock_token is None:
            logger.warning(
                "Could not acquire distributed lock for deal %s (user=%s) after %d attempts.",
                deal.deal_id,
                rule.user_id,
                max_lock_retries,
            )
            # Release idempotency claim so a future retry could try if needed
            await self.redis.delete(
                self.lock_manager.idempotency_key(deal.deal_id, rule.user_id)
            )
            return None

        tx_id: uuid.UUID | None = None
        try:
            # 3. Saga Step 1: Reserve seat in DB & record PENDING_GDS transaction
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(FlightDeal)
                    .where(FlightDeal.deal_id == deal.deal_id)
                    .with_for_update()
                )
                db_deal = (await session.execute(stmt)).scalar_one_or_none()

                if (
                    db_deal is None
                    or db_deal.seats_available <= 0
                    or db_deal.status != DealStatus.ACTIVE.value
                ):
                    logger.info(
                        "Seat no longer available for deal %s [user=%s, seats=%s, status=%s]. Preventing double-booking.",
                        deal.deal_id,
                        rule.user_id,
                        getattr(db_deal, "seats_available", 0),
                        getattr(db_deal, "status", "MISSING"),
                    )
                    await self.lock_manager.mark_idempotency_status(
                        deal.deal_id, rule.user_id, "SKIPPED_SOLD_OUT"
                    )
                    return None

                # Decrement seat inventory atomically
                db_deal.seats_available -= 1
                if db_deal.seats_available == 0:
                    db_deal.status = DealStatus.SOLD_OUT.value
                db_deal.updated_at = datetime.now(UTC)

                booking_tx = BookingTransaction(
                    id=uuid.uuid4(),
                    idempotency_key=idem_key_str,
                    deal_id=deal.deal_id,
                    rule_id=rule.id,
                    user_id=rule.user_id,
                    amount_paid=deal.price,
                    currency=deal.currency,
                    status=BookingStatus.PENDING_GDS.value,
                )
                session.add(booking_tx)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    logger.info(
                        "Database unique constraint prevented duplicate booking for %s",
                        idem_key_str,
                    )
                    return None

                tx_id = booking_tx.id

            # 4. Saga Step 2: Call external GDS API to issue ticket/PNR
            try:
                confirmation = await self.gds_client.issue_ticket(
                    deal=deal,
                    user_id=rule.user_id,
                    passenger_info=rule.passenger_info,
                    idempotency_key=idem_key_str,
                )
            except (GDSIssuanceError, OSError, RuntimeError) as gds_exc:
                # 5b. Saga Step 3 (FAILURE PATH): Execute Compensating Transaction!
                reason = (
                    f"{gds_exc.error_code}: {gds_exc}"
                    if isinstance(gds_exc, GDSIssuanceError)
                    else f"UNEXPECTED_GDS_FAILURE: {gds_exc}"
                )
                logger.warning(
                    "Initiating Saga Compensation for deal=%s, user=%s, tx=%s due to: %s",
                    deal.deal_id,
                    rule.user_id,
                    tx_id,
                    reason,
                )
                compensated_tx = await self._compensate_failed_booking(
                    tx_id=tx_id,
                    deal_id=deal.deal_id,
                    user_id=rule.user_id,
                    failure_reason=reason,
                )
                return compensated_tx

            # 5a. Saga Step 3 (HAPPY PATH): Confirm transaction with PNR
            async with AsyncSessionLocal() as session:
                tx_stmt = (
                    select(BookingTransaction)
                    .where(BookingTransaction.id == tx_id)
                    .with_for_update()
                )
                confirmed_tx = (await session.execute(tx_stmt)).scalar_one()
                confirmed_tx.status = BookingStatus.CONFIRMED.value
                confirmed_tx.pnr_reference = confirmation.pnr_reference
                confirmed_tx.updated_at = datetime.now(UTC)
                await session.commit()
                await session.refresh(confirmed_tx)

            await self.lock_manager.mark_idempotency_status(
                deal.deal_id, rule.user_id, f"CONFIRMED:{confirmation.pnr_reference}"
            )
            logger.info(
                "Autonomous purchase CONFIRMED [tx_id=%s, deal=%s, user=%s, PNR=%s, price=%s %s]",
                confirmed_tx.id,
                deal.deal_id,
                rule.user_id,
                confirmation.pnr_reference,
                deal.price,
                deal.currency,
            )
            return confirmed_tx

        finally:
            # Always release the Redis distributed lock immediately
            if lock_token is not None:
                released = await self.lock_manager.release_deal_lock(deal.deal_id, lock_token)
                logger.debug(
                    "Released distributed lock for deal %s [token=%s, released=%s]",
                    deal.deal_id,
                    lock_token[:8],
                    released,
                )

    async def _compensate_failed_booking(
        self,
        tx_id: uuid.UUID,
        deal_id: str,
        user_id: str,
        failure_reason: str,
    ) -> BookingTransaction:
        """
        Saga Compensating Transaction:
        - Restores `FlightDeal.seats_available += 1` and resets `status = 'ACTIVE'`.
        - Marks `BookingTransaction.status = 'COMPENSATED_FAILED'` with `failure_reason`.
        - Updates Redis idempotency status to `COMPENSATED_FAILED`.
        """
        async with AsyncSessionLocal() as session:
            deal_stmt = (
                select(FlightDeal).where(FlightDeal.deal_id == deal_id).with_for_update()
            )
            db_deal = (await session.execute(deal_stmt)).scalar_one_or_none()
            if db_deal is not None:
                db_deal.seats_available += 1
                if db_deal.status == DealStatus.SOLD_OUT.value and db_deal.seats_available > 0:
                    db_deal.status = DealStatus.ACTIVE.value
                db_deal.updated_at = datetime.now(UTC)

            tx_stmt = (
                select(BookingTransaction)
                .where(BookingTransaction.id == tx_id)
                .with_for_update()
            )
            tx = (await session.execute(tx_stmt)).scalar_one()
            tx.status = BookingStatus.COMPENSATED_FAILED.value
            tx.failure_reason = failure_reason
            tx.updated_at = datetime.now(UTC)

            await session.commit()
            await session.refresh(tx)

        await self.lock_manager.mark_idempotency_status(
            deal_id, user_id, BookingStatus.COMPENSATED_FAILED.value
        )
        logger.info(
            "Saga Compensation completed: seat restored for deal %s and transaction %s marked COMPENSATED_FAILED.",
            deal_id,
            tx_id,
        )
        return tx


class BookingEngineStreamWorker:
    """
    Event-Driven Transport Adapter consuming normalized `DealPayload` messages
    from Redis Streams (`XREADGROUP`) and delegating to `BookingEngineService`.
    """

    def __init__(self, redis: aioredis.Redis) -> None:
        self.redis = redis
        self.engine_service = BookingEngineService(redis=redis)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def ensure_consumer_group(self) -> None:
        """Create the Redis Stream and Consumer Group idempotently (`MKSTREAM`)."""
        try:
            await self.redis.xgroup_create(
                name=settings.REDIS_DEALS_STREAM,
                groupname=settings.REDIS_CONSUMER_GROUP,
                id="0",
                mkstream=True,
            )
            logger.info(
                "Created Redis Consumer Group '%s' on stream '%s'.",
                settings.REDIS_CONSUMER_GROUP,
                settings.REDIS_DEALS_STREAM,
            )
        except ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                logger.debug(
                    "Redis Consumer Group '%s' already exists.",
                    settings.REDIS_CONSUMER_GROUP,
                )
            else:
                raise

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._consume_loop(), name="booking-engine-worker")
            logger.info(
                "BookingEngineStreamWorker started [group=%s, consumer=%s].",
                settings.REDIS_CONSUMER_GROUP,
                settings.REDIS_CONSUMER_NAME,
            )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("BookingEngineStreamWorker stopped cleanly.")

    async def _consume_loop(self) -> None:
        await self.ensure_consumer_group()

        while not self._stop_event.is_set():
            try:
                streams = await self.redis.xreadgroup(
                    groupname=settings.REDIS_CONSUMER_GROUP,
                    consumername=settings.REDIS_CONSUMER_NAME,
                    streams={settings.REDIS_DEALS_STREAM: ">"},
                    count=10,
                    block=2000,
                )
                if not streams:
                    continue

                for _stream_name, messages in streams:
                    for message_id, fields in messages:
                        try:
                            deal = DealPayload.from_stream_message(fields)
                            await self.engine_service.process_deal_event(deal)
                            await self.redis.xack(
                                settings.REDIS_DEALS_STREAM,
                                settings.REDIS_CONSUMER_GROUP,
                                message_id,
                            )
                        except Exception:
                            logger.exception(
                                "Failed processing stream message %s",
                                message_id,
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in Redis Stream consumer loop")
                await asyncio.sleep(1.0)

