import asyncio
import html
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import aiosqlite
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

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
log = logging.getLogger("shortlet")

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
PAYMENT_RETURN_PAGE = Path(__file__).parent / "templates" / "payment_return.html"


@app.exception_handler(service.DomainError)
async def domain_error_handler(_: Request, exc: service.DomainError) -> JSONResponse:
    log.warning(
        "API domain error status=%s error=%s",
        exc.status_code,
        type(exc).__name__,
    )
    if isinstance(exc, service.DatesUnavailable):
        body = HoldConflict(alternatives=exc.alternatives).model_dump(mode="json")
    else:
        body = {"detail": exc.detail}
    return JSONResponse(body, status_code=exc.status_code)


@app.get("/health")
async def health() -> dict:
    configured = paystack.configured()
    log.info("API request endpoint=/health paystack_configured=%s", configured)
    return {"ok": True, "paystack_configured": configured}


@app.get("/rooms")
async def list_rooms(db: DB) -> dict:
    rooms = await service.list_units(db)
    log.info("API request endpoint=/rooms rooms_returned=%d", len(rooms))
    return {"rooms": rooms}


@app.get("/availability", response_model=AvailabilityResponse)
async def check_availability(q: Annotated[AvailabilityRequest, Query()], db: DB):
    log.info(
        "API request endpoint=/availability check_in=%s check_out=%s guests=%d",
        q.check_in,
        q.check_out,
        q.guests,
    )
    available = await service.list_available(db, q.check_in, q.check_out, q.guests)
    log.info("API response endpoint=/availability available_count=%d", len(available))
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
    log.info(
        "API request endpoint=/payments/initialize unit_id=%s check_in=%s "
        "check_out=%s guests=%d booking_intent_confirmed=%s "
        "guest_name_provided=%s guest_whatsapp_last4=%s",
        req.unit_id,
        req.check_in,
        req.check_out,
        req.guests,
        req.booking_intent_confirmed,
        req.guest_name is not None,
        req.guest_whatsapp[-4:],
    )
    result = await service.initialize_payment(db, req)
    log.info(
        "API response endpoint=/payments/initialize booking_reference=%s "
        "amount_kobo=%d hold_expires_at=%s",
        result.booking_reference,
        result.amount_kobo,
        result.hold_expires_at,
    )
    return result


@app.get(
    "/payments/return",
    response_class=HTMLResponse,
    summary="Show the Paystack payment return page",
    description=(
        "Paystack redirects here with a transaction reference. Set "
        "PAYSTACK_CALLBACK_URL to this route's public URL."
    ),
)
async def payment_return(reference: str = Query(default="")) -> HTMLResponse:
    log.info(
        "API request endpoint=/payments/return reference=%r",
        reference or None,
    )
    page = PAYMENT_RETURN_PAGE.read_text(encoding="utf-8")
    safe_reference = html.escape(reference) if reference else "Not provided"
    return HTMLResponse(
        content=page.replace("{{REFERENCE}}", safe_reference),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/webhooks/paystack")
async def paystack_webhook(request: Request, db: DB) -> dict:
    raw = await request.body()
    signature_is_valid = paystack.signature_valid(
        raw, request.headers.get("x-paystack-signature", "")
    )
    log.info(
        "API request endpoint=/webhooks/paystack body_bytes=%d signature_valid=%s",
        len(raw),
        signature_is_valid,
    )
    if not signature_is_valid:
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        event = json.loads(raw)
    except ValueError:
        log.warning("API webhook rejected reason=invalid_json")
        raise HTTPException(status_code=400, detail="invalid JSON")
    event_type = event.get("event", "")
    data = event.get("data") or {}
    # Paystack has no dedicated event id; event type + transaction id is stable across retries.
    event_id = f"{event_type}:{data.get('id') or data.get('reference')}"
    log.info(
        "API webhook event_type=%s transaction_id=%s reference=%s",
        event_type,
        data.get("id"),
        data.get("reference"),
    )

    if not await service.record_webhook(db, event_id, event_type, raw.decode()):
        log.info("API webhook duplicate_ignored event_id=%s", event_id)
        return {"status": "duplicate_ignored"}
    outcome = "ignored"
    if event_type == "charge.success" and data.get("reference"):
        # Re-verify with Paystack rather than trusting the payload.
        # If this raises (502), the event stays unprocessed so Paystack's retry works.
        outcome = await service.reconcile(db, data["reference"])
    await service.mark_webhook_processed(db, event_id)
    log.info("API webhook processed event_id=%s outcome=%s", event_id, outcome)
    return {"status": outcome}


@app.get("/bookings/{reference}", response_model=BookingStatusResponse)
async def get_booking(reference: str, db: DB):
    log.info("API request endpoint=/bookings/{reference} reference=%s", reference)
    status = await service.booking_status(db, reference)
    log.info(
        "API response endpoint=/bookings/{reference} reference=%s booking_status=%s payment_status=%s",
        reference,
        status.booking_status,
        status.payment_status,
    )
    return status
