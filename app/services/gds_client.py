import asyncio
import logging
import random
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings
from app.schemas import DealPayload

logger = logging.getLogger(__name__)
settings = get_settings()


class GDSIssuanceError(Exception):
    """Raised when the upstream GDS (Amadeus/Duffel) rejects or fails ticket issuance."""

    def __init__(self, message: str, error_code: str = "GDS_UPSTREAM_ERROR") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class GDSOrderConfirmation:
    pnr_reference: str
    eticket_number: str
    issued_at: datetime
    provider: str


class GDSBookingClient:
    """
    Outbound Adapter (Port implementation) for GDS Ticket Issuance.
    Simulates an asynchronous HTTP call using httpx.AsyncClient to Duffel/Amadeus Orders API,
    including realistic network latency and configurable failure injection to trigger Saga compensation.
    """

    def __init__(
        self,
        failure_rate: float | None = None,
        latency_ms: int | None = None,
    ) -> None:
        self.failure_rate = (
            failure_rate if failure_rate is not None else settings.GDS_SIMULATED_FAILURE_RATE
        )
        self.latency_ms = (
            latency_ms if latency_ms is not None else settings.GDS_SIMULATED_LATENCY_MS
        )

    async def issue_ticket(
        self,
        deal: DealPayload,
        user_id: str,
        passenger_info: dict[str, Any],
        idempotency_key: str,
    ) -> GDSOrderConfirmation:
        """
        Attempt to issue a confirmed PNR with the upstream GDS.
        Supports deterministic failure simulation via `raw_metadata.force_gds_failure = true`
        or flight numbers ending in 'FAIL', as well as stochastic failure rate.
        """
        # Simulate network round-trip latency to GDS API using httpx MockTransport
        await asyncio.sleep(self.latency_ms / 1000.0)

        force_fail = bool(deal.raw_metadata.get("force_gds_failure", False)) or (
            deal.flight_number.upper().endswith("FAIL")
        )
        force_success = bool(deal.raw_metadata.get("force_gds_success", False))

        async def _mock_gds_handler(request: httpx.Request) -> httpx.Response:
            if force_fail or (not force_success and random.random() < self.failure_rate):
                return httpx.Response(
                    status_code=502,
                    json={
                        "error": "GDS_FARE_EXPIRED_OR_INVENTORY_TIMEOUT",
                        "message": (
                            f"Upstream GDS failed to issue PNR for flight {deal.flight_number} "
                            f"(deal={deal.deal_id}, user={user_id})"
                        ),
                    },
                )

            pnr = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            eticket = f"057-{''.join(random.choices(string.digits, k=10))}"
            return httpx.Response(
                status_code=201,
                json={
                    "pnr_reference": pnr,
                    "eticket_number": eticket,
                    "provider": "DUFFEL_AMADEUS_SANDBOX",
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                },
            )

        transport = httpx.MockTransport(_mock_gds_handler)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://api.sandbox.gds.internal"
        ) as client:
            response = await client.post(
                "/v1/orders",
                headers={
                    "Idempotency-Key": idempotency_key,
                    "X-Deal-Source": deal.source.value,
                },
                json={
                    "deal_id": deal.deal_id,
                    "flight_number": deal.flight_number,
                    "origin": deal.origin,
                    "destination": deal.destination,
                    "price": str(deal.price),
                    "currency": deal.currency,
                    "passenger": passenger_info,
                },
                timeout=5.0,
            )

        if response.status_code >= 400:
            body = response.json()
            error_msg = body.get("message", "Unknown GDS issuance failure")
            error_code = body.get("error", "GDS_HTTP_ERROR")
            logger.warning(
                "GDS ticket issuance rejected [code=%s]: %s",
                error_code,
                error_msg,
            )
            raise GDSIssuanceError(message=error_msg, error_code=error_code)

        payload = response.json()
        logger.info(
            "GDS ticket issued successfully [PNR=%s, eTicket=%s, deal=%s, user=%s]",
            payload["pnr_reference"],
            payload["eticket_number"],
            deal.deal_id,
            user_id,
        )
        return GDSOrderConfirmation(
            pnr_reference=payload["pnr_reference"],
            eticket_number=payload["eticket_number"],
            issued_at=datetime.fromisoformat(payload["issued_at"]),
            provider=payload["provider"],
        )
