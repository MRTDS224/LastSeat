import json
import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DealSource(StrEnum):
    GDS_POLLER = "GDS_POLLER"
    PARTNER_WEBHOOK = "PARTNER_WEBHOOK"


class CabinClass(StrEnum):
    ECONOMY = "ECONOMY"
    PREMIUM_ECONOMY = "PREMIUM_ECONOMY"
    BUSINESS = "BUSINESS"
    FIRST = "FIRST"


class DealStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SOLD_OUT = "SOLD_OUT"
    EXPIRED = "EXPIRED"


class BookingStatus(StrEnum):
    PENDING_GDS = "PENDING_GDS"
    CONFIRMED = "CONFIRMED"
    COMPENSATED_FAILED = "COMPENSATED_FAILED"


class DealPayload(BaseModel):
    """
    Canonical domain model representing a normalized last-minute flight deal.
    Both the GDS Poller and Partner Webhook normalize their payloads into this model
    before publishing to Redis Streams and persisting to PostgreSQL.
    """

    deal_id: str = Field(..., min_length=3, max_length=128, description="Unique deal identifier")
    source: DealSource = Field(..., description="Originating ingestion source")
    flight_number: str = Field(..., min_length=2, max_length=32, examples=["AF006"])
    airline_code: str = Field(..., min_length=2, max_length=8, examples=["AF"])
    origin: str = Field(..., min_length=3, max_length=3, description="Origin IATA 3-letter code")
    destination: str = Field(
        ..., min_length=3, max_length=3, description="Destination IATA 3-letter code"
    )
    departure_at: datetime = Field(..., description="Flight departure timestamp (UTC)")
    arrival_at: datetime = Field(..., description="Flight arrival timestamp (UTC)")
    price: Decimal = Field(..., ge=Decimal("0.01"), decimal_places=2)
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    cabin_class: CabinClass = Field(default=CabinClass.ECONOMY)
    seats_available: int = Field(..., ge=1, le=500)
    expires_at: datetime = Field(..., description="Deal expiration timestamp (UTC)")
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("origin", "destination", "currency", "airline_code", mode="before")
    @classmethod
    def normalize_uppercase(cls, v: str) -> str:
        if isinstance(v, str):
            return v.strip().upper()
        return v

    @model_validator(mode="after")
    def validate_chronology_and_route(self) -> "DealPayload":
        if self.origin == self.destination:
            raise ValueError("Origin and destination IATA codes must be distinct.")
        if self.arrival_at <= self.departure_at:
            raise ValueError("arrival_at must be strictly after departure_at.")
        return self

    def to_stream_message(self) -> dict[str, str]:
        """Serialize DealPayload into a flat string dict suitable for Redis XADD."""
        return {"payload": self.model_dump_json()}

    @classmethod
    def from_stream_message(cls, data: dict[str, Any]) -> "DealPayload":
        """Deserialize a Redis Stream entry back into a validated DealPayload."""
        raw_payload = data.get("payload")
        if not raw_payload:
            raise ValueError("Missing 'payload' field in Redis stream message.")
        if isinstance(raw_payload, str):
            return cls.model_validate_json(raw_payload)
        return cls.model_validate(json.loads(raw_payload))


class PartnerDealIngestionRequest(BaseModel):
    """
    Partner Charter / Tour Operator REST payload schema.
    Allows partners to push distressed seat inventory that gets normalized into DealPayload.
    """

    partner_deal_ref: str = Field(
        ..., min_length=3, max_length=100, examples=["CHARTER-TUI-99812"]
    )
    carrier_iata: str = Field(..., min_length=2, max_length=8, examples=["TO"])
    flight_code: str = Field(..., min_length=2, max_length=32, examples=["TO3412"])
    departure_airport: str = Field(..., min_length=3, max_length=3, examples=["CDG"])
    arrival_airport: str = Field(..., min_length=3, max_length=3, examples=["JFK"])
    takeoff_utc: datetime
    landing_utc: datetime
    unit_price: Decimal = Field(..., ge=Decimal("1.00"), decimal_places=2)
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    cabin: CabinClass = Field(default=CabinClass.ECONOMY)
    remaining_seats: int = Field(..., ge=1, le=500)
    offer_valid_until_utc: datetime
    partner_notes: dict[str, Any] = Field(default_factory=dict)

    def to_deal_payload(self) -> DealPayload:
        """Adapter method normalizing partner format into the unified DealPayload model."""
        normalized_id = (
            self.partner_deal_ref
            if self.partner_deal_ref.startswith("PARTNER-")
            else f"PARTNER-{self.partner_deal_ref}"
        )
        return DealPayload(
            deal_id=normalized_id,
            source=DealSource.PARTNER_WEBHOOK,
            flight_number=self.flight_code,
            airline_code=self.carrier_iata,
            origin=self.departure_airport,
            destination=self.arrival_airport,
            departure_at=self.takeoff_utc,
            arrival_at=self.landing_utc,
            price=self.unit_price,
            currency=self.currency,
            cabin_class=self.cabin,
            seats_available=self.remaining_seats,
            expires_at=self.offer_valid_until_utc,
            raw_metadata={"partner_notes": self.partner_notes},
        )


class DealResponse(BaseModel):
    """Read representation of a stored flight deal."""

    model_config = ConfigDict(from_attributes=True)

    deal_id: str
    source: str
    flight_number: str
    airline_code: str
    origin: str
    destination: str
    departure_at: datetime
    arrival_at: datetime
    price: Decimal
    currency: str
    cabin_class: str
    seats_available: int
    expires_at: datetime
    status: str
    raw_metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class DealIngestionAck(BaseModel):
    """Acknowledgement returned upon deal ingestion."""

    status: str = "ACCEPTED"
    deal_id: str
    stream_message_id: str
    source: DealSource
    message: str


class PassengerInfo(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=64, examples=["Jean"])
    last_name: str = Field(..., min_length=1, max_length=64, examples=["Dupont"])
    email: str = Field(default="traveler@example.com")
    passport: str = Field(..., min_length=5, max_length=32, examples=["FR99887766"])


class AgentRuleCreate(BaseModel):
    user_id: str = Field(..., min_length=2, max_length=64, examples=["usr_thomas_09"])
    origin: str = Field(..., min_length=3, max_length=3, examples=["CDG"])
    destination: str | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="3-letter IATA destination code, or null for wildcard (any destination)",
        examples=["JFK"],
    )
    max_price: Decimal = Field(..., gt=Decimal("0.00"), decimal_places=2, examples=[280.00])
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    preferred_cabin: CabinClass = Field(default=CabinClass.ECONOMY)
    earliest_departure: datetime | None = None
    latest_departure: datetime | None = None
    auto_purchase_enabled: bool = True
    passenger_info: PassengerInfo

    @field_validator("origin", "destination", "currency", mode="before")
    @classmethod
    def normalize_iata(cls, v: str | None) -> str | None:
        if isinstance(v, str):
            v = v.strip().upper()
            return v if v else None
        return v


class AgentRuleUpdate(BaseModel):
    max_price: Decimal | None = Field(default=None, gt=Decimal("0.00"), decimal_places=2)
    preferred_cabin: CabinClass | None = None
    auto_purchase_enabled: bool | None = None
    earliest_departure: datetime | None = None
    latest_departure: datetime | None = None


class AgentRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: str
    origin: str
    destination: str | None
    max_price: Decimal
    currency: str
    preferred_cabin: str
    earliest_departure: datetime | None
    latest_departure: datetime | None
    auto_purchase_enabled: bool
    passenger_info: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class BookingTransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    idempotency_key: str
    deal_id: str
    rule_id: uuid.UUID
    user_id: str
    amount_paid: Decimal
    currency: str
    status: str
    pnr_reference: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
