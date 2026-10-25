from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_STAY_NIGHTS = 30
HOLD_MINUTES = 3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_hold_expiry() -> datetime:
    return utcnow() + timedelta(minutes=HOLD_MINUTES)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BookingStatus(str, Enum):
    HELD = "held"            # dates reserved, awaiting payment
    CONFIRMED = "confirmed"  # payment verified server-side
    EXPIRED = "expired"      # hold ended without confirmed payment; dates released
    CANCELLED = "cancelled"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    ABANDONED = "abandoned"
    # Money arrived but the booking could not be honoured (hold expired and
    # dates were taken, or amount mismatch). A human or refund flow must act.
    REQUIRES_RESOLUTION = "requires_resolution"


# --------------------------------------------------------------------------
# Stay dates (shared validation)
# --------------------------------------------------------------------------
class StayDates(BaseModel):
    check_in: date
    check_out: date

    @model_validator(mode="after")
    def _validate_dates(self) -> "StayDates":
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        if (self.check_out - self.check_in).days > MAX_STAY_NIGHTS:
            raise ValueError(f"stays are limited to {MAX_STAY_NIGHTS} nights")
        if self.check_in < utcnow().date():
            raise ValueError("check_in cannot be in the past")
        return self

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days



class Unit(ORMModel):
    id: UUID = Field(default_factory=uuid4)
    name: str                                   # e.g. "Palm Suite"
    description: str = ""
    bedrooms: int = Field(ge=0)
    max_guests: int = Field(ge=1)
    nightly_rate_kobo: int = Field(gt=0)
    amenities: list[str] = Field(default_factory=list)
    house_rules: str = ""
    is_active: bool = True


class Guest(ORMModel):
    id: UUID = Field(default_factory=uuid4)
    whatsapp_number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")  # E.164
    name: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Booking(ORMModel, StayDates):
    id: UUID = Field(default_factory=uuid4)
    reference: str                             
    unit_id: UUID
    guest_id: UUID
    status: BookingStatus = BookingStatus.HELD
    total_kobo: int = Field(gt=0)
    hold_expires_at: datetime | None = Field(default_factory=default_hold_expiry)
    created_at: datetime = Field(default_factory=utcnow)
    confirmed_at: datetime | None = None

    @property
    def hold_is_active(self) -> bool:
        return (
            self.status == BookingStatus.HELD
            and self.hold_expires_at is not None
            and self.hold_expires_at > utcnow()
        )


class Payment(ORMModel):
    id: UUID = Field(default_factory=uuid4)
    booking_id: UUID
    paystack_reference: str                     # unique; sent to Paystack
    amount_kobo: int = Field(gt=0)              # what we asked for
    paid_amount_kobo: int | None = None         # what Paystack says arrived
    currency: str = "NGN"
    status: PaymentStatus = PaymentStatus.PENDING
    authorization_url: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    verified_at: datetime | None = None


class WebhookEvent(ORMModel):
    """Raw inbound provider events. Unique on (provider, event_id) so a
    duplicate delivery is detected and ignored (idempotency)."""
    id: UUID = Field(default_factory=uuid4)
    provider: str = "paystack"
    event_id: str
    event_type: str
    payload: dict[str, Any]
    received_at: datetime = Field(default_factory=utcnow)
    processed_at: datetime | None = None


# --------------------------------------------------------------------------
# API request/response schemas (what the Chord tools send and receive)
# --------------------------------------------------------------------------
class AvailabilityRequest(StayDates):
    guests: int = Field(default=1, ge=1)


class AvailableUnit(BaseModel):
    unit_id: UUID
    name: str
    bedrooms: int
    amenities: list[str]
    nightly_rate_kobo: int
    nights: int
    total_kobo: int


class AvailabilityResponse(BaseModel):
    check_in: date
    check_out: date
    available: list[AvailableUnit]  # empty list = nothing free; say so plainly


class InitializePaymentRequest(StayDates):
    unit_id: UUID
    guest_whatsapp: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    guest_name: str | None = None
    guests: int = Field(default=1, ge=1)
    booking_intent_confirmed: Literal[True] = Field(
        description="Set true only after the guest explicitly agrees to book or pay."
    )


class CreateHoldResponse(BaseModel):
    booking_reference: str
    unit_name: str
    total_kobo: int
    hold_expires_at: datetime


class HoldConflict(BaseModel):
    """Returned (HTTP 409) when the dates were taken. Includes alternatives
    so the agent can offer something instead of dead-ending."""
    detail: str = "Those dates are no longer available."
    alternatives: list[AvailableUnit] = Field(default_factory=list)


class CreatePaymentLinkResponse(BaseModel):
    booking_reference: str
    authorization_url: str
    amount_kobo: int
    hold_expires_at: datetime


class BookingStatusResponse(BaseModel):
    """The ONLY thing the agent may rely on when telling a guest whether
    they are booked. `message` is plain-language for the agent to relay."""
    booking_reference: str
    booking_status: BookingStatus
    payment_status: PaymentStatus | None = None
    unit_name: str
    check_in: date
    check_out: date
    total_kobo: int
    message: str