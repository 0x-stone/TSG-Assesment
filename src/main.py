import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated

import aiosqlite
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import paystack, service
from .database import get_db, init_db
from .models import (
    AvailabilityRequest,
    AvailabilityResponse,
    BookingStatusResponse,
    CreatePaymentLinkResponse,
    HoldConflict,
    InitializePaymentRequest,
)

logging.basicConfig(level=logging.INFO)

DB = Annotated[aiosqlite.Connection, Depends(get_db)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    sweeper = asyncio.create_task(service.run_sweeper())
    try:
        yield
    finally:
        sweeper.cancel()


app = FastAPI(title="Shortlet Booking API", lifespan=lifespan)


@app.exception_handler(service.DomainError)
async def domain_error_handler(_: Request, exc: service.DomainError) -> JSONResponse:
    if isinstance(exc, service.DatesUnavailable):
        body = HoldConflict(alternatives=exc.alternatives).model_dump(mode="json")
    else:
        body = {"detail": exc.detail}
    return JSONResponse(body, status_code=exc.status_code)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "paystack_configured": paystack.configured()}


@app.get("/rooms")
async def list_rooms(db: DB) -> dict:
    return {"rooms": await service.list_units(db)}


@app.get("/availability", response_model=AvailabilityResponse)
async def check_availability(q: Annotated[AvailabilityRequest, Query()], db: DB):
    available = await service.list_available(db, q.check_in, q.check_out, q.guests)
    return AvailabilityResponse(check_in=q.check_in, check_out=q.check_out, available=available)


@app.post(
    "/payments/initialize",
    response_model=CreatePaymentLinkResponse,
    status_code=201,
    responses={409: {"model": HoldConflict}},
    summary="Start a guest-requested booking",
    description=(
        "Call only after the guest explicitly says they want to book or pay. "
        "Atomically checks availability, creates a three-minute hold, and returns a "
        "Paystack checkout link. A payment link or hold is not a booking confirmation; "
        "verify final status with GET /bookings/{reference}. Use GET /availability "
        "for read-only date checks."
    ),
)
async def initialize_payment(req: InitializePaymentRequest, db: DB):
    return await service.initialize_payment(db, req)


@app.post("/webhooks/paystack")
async def paystack_webhook(request: Request, db: DB) -> dict:
    raw = await request.body()
    if not paystack.signature_valid(raw, request.headers.get("x-paystack-signature", "")):
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON")
    event_type = event.get("event", "")
    data = event.get("data") or {}
    # Paystack has no dedicated event id; event type + transaction id is stable across retries.
    event_id = f"{event_type}:{data.get('id') or data.get('reference')}"

    if not await service.record_webhook(db, event_id, event_type, raw.decode()):
        return {"status": "duplicate_ignored"}
    outcome = "ignored"
    if event_type == "charge.success" and data.get("reference"):
        # Re-verify with Paystack rather than trusting the payload.
        # If this raises (502), the event stays unprocessed so Paystack's retry works.
        outcome = await service.reconcile(db, data["reference"])
    await service.mark_webhook_processed(db, event_id)
    return {"status": outcome}


@app.get("/bookings/{reference}", response_model=BookingStatusResponse)
async def get_booking(reference: str, db: DB):
    return await service.booking_status(db, reference)
