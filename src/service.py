"""Booking logic. Routes in main.py stay thin; everything that touches
money or inventory lives here."""
import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import aiosqlite

from . import paystack
from .database import connect, iso, write_tx
from .models import (
    HOLD_MINUTES,
    AvailableUnit,
    BookingStatus,
    BookingStatusResponse,
    CreateHoldResponse,
    CreatePaymentLinkResponse,
    InitializePaymentRequest,
    PaymentStatus,
    utcnow,
)
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("shortlet")

HOLD_SECONDS = int(os.getenv("HOLD_SECONDS", HOLD_MINUTES * 60))
ABANDONED_HOLD_LIMIT = 3
ABANDONED_HOLD_WINDOW = timedelta(minutes=60)
HOLD_COOLDOWN = timedelta(minutes=15)
WAT = timezone(timedelta(hours=1))  # Lagos time, for guest-facing messages


# ---------------------------------------------------------------- errors
class DomainError(Exception):
    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class NotFound(DomainError):
    status_code = 404


class Conflict(DomainError):
    status_code = 409


class UpstreamError(DomainError):
    status_code = 502


class DatesUnavailable(Conflict):
    def __init__(self, alternatives: list[AvailableUnit]):
        super().__init__("Those dates are no longer available.")
        self.alternatives = alternatives


# --------------------------------------------------------------- helpers
async def _one(db: aiosqlite.Connection, sql: str, params=()):
    cur = await db.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return row


async def _all(db: aiosqlite.Connection, sql: str, params=()):
    cur = await db.execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return rows


def naira(kobo: int) -> str:
    whole, rem = divmod(kobo, 100)
    return f"₦{whole:,}" if rem == 0 else f"₦{whole:,}.{rem:02d}"


def _wat(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(WAT).strftime("%H:%M WAT")


# ------------------------------------------------------------ inventory
async def list_units(db: aiosqlite.Connection) -> list[dict]:
    rows = await _all(db, "SELECT * FROM units WHERE is_active = 1 ORDER BY nightly_rate_kobo")
    return [
        {
            "unit_id": r["id"],
            "name": r["name"],
            "description": r["description"],
            "bedrooms": r["bedrooms"],
            "max_guests": r["max_guests"],
            "nightly_rate_kobo": r["nightly_rate_kobo"],
            "amenities": json.loads(r["amenities"]),
            "house_rules": r["house_rules"],
        }
        for r in rows
    ]


async def list_available(
    db: aiosqlite.Connection, check_in: date, check_out: date, guests: int = 1
) -> list[AvailableUnit]:
    nights = (check_out - check_in).days
    rows = await _all(
        db,
        """
        SELECT u.* FROM units u
        WHERE u.is_active = 1 AND u.max_guests >= :guests
          AND NOT EXISTS (
            SELECT 1 FROM bookings b
            WHERE b.unit_id = u.id
              AND (b.status = 'confirmed'
                   OR (b.status = 'held' AND b.hold_expires_at > :now))
              AND b.check_in < :check_out AND b.check_out > :check_in)
        ORDER BY u.nightly_rate_kobo
        """,
        {
            "guests": guests,
            "now": iso(utcnow()),
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
        },
    )
    return [
        AvailableUnit(
            unit_id=r["id"],
            name=r["name"],
            bedrooms=r["bedrooms"],
            amenities=json.loads(r["amenities"]),
            nightly_rate_kobo=r["nightly_rate_kobo"],
            nights=nights,
            total_kobo=nights * r["nightly_rate_kobo"],
        )
        for r in rows
    ]


# ----------------------------------------------------------------- holds
async def _sweep(db: aiosqlite.Connection, now: datetime) -> None:
    await db.execute(
        "UPDATE bookings SET status = 'expired' WHERE status = 'held' AND hold_expires_at <= ?",
        (iso(now),),
    )


async def sweep_expired(db: aiosqlite.Connection) -> None:
    async with write_tx(db):
        await _sweep(db, utcnow())


async def run_sweeper(interval: int = 30) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            db = await connect()
            try:
                await sweep_expired(db)
            finally:
                await db.close()
        except Exception:
            log.exception("hold sweeper failed")


async def create_hold(db: aiosqlite.Connection, req: InitializePaymentRequest) -> CreateHoldResponse:
    now = utcnow()
    expires = now + timedelta(seconds=HOLD_SECONDS)
    reference = f"LK-{secrets.token_hex(3).upper()}"
    try:
        async with write_tx(db):
            # Release stale holds first, otherwise an expired-but-unswept hold
            # would block a real guest.
            await _sweep(db, now)
            unit = await _one(db, "SELECT * FROM units WHERE id = ? AND is_active = 1", (str(req.unit_id),))
            if unit is None:
                raise NotFound("Unknown unit.")
            guest = await _one(db, "SELECT id FROM guests WHERE whatsapp_number = ?", (req.guest_whatsapp,))
            if guest is None:
                guest_id = str(uuid4())
                await db.execute(
                    "INSERT INTO guests (id, whatsapp_number, name, created_at) VALUES (?,?,?,?)",
                    (guest_id, req.guest_whatsapp, req.guest_name, iso(now)),
                )
            else:
                guest_id = guest["id"]
                if req.guest_name:
                    await db.execute("UPDATE guests SET name = ? WHERE id = ?", (req.guest_name, guest_id))
            if unit["max_guests"] < req.guests:
                raise Conflict(f"This apartment accommodates up to {unit['max_guests']} guests.")
            active_hold = await _one(
                db,
                "SELECT reference FROM bookings WHERE guest_id = ? AND status = 'held' "
                "AND hold_expires_at > ? LIMIT 1",
                (guest_id, iso(now)),
            )
            if active_hold:
                raise Conflict(
                    f"You already have an active hold ({active_hold['reference']}). "
                    "Complete payment or wait for it to expire before starting another booking."
                )
            cutoff = iso(now - ABANDONED_HOLD_WINDOW)
            abandoned = await _one(
                db,
                "SELECT COUNT(*) AS count, MAX(b.hold_expires_at) AS latest_expiry "
                "FROM bookings b WHERE b.guest_id = ? AND b.status = 'expired' "
                "AND b.hold_expires_at >= ? AND b.hold_expires_at <= ? "
                "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.booking_id = b.id "
                "AND p.status IN ('success', 'requires_resolution'))",
                (guest_id, cutoff, iso(now)),
            )
            if abandoned["count"] >= ABANDONED_HOLD_LIMIT:
                cooldown_until = (
                    datetime.fromisoformat(abandoned["latest_expiry"]) + HOLD_COOLDOWN
                )
                if now < cooldown_until:
                    minutes = max(1, int((cooldown_until - now).total_seconds() // 60) + 1)
                    raise Conflict(
                        f"Too many unpaid holds recently. Please try again in about {minutes} minutes."
                    )
            total = req.nights * unit["nightly_rate_kobo"]
            # The trigger raises 'booking_overlap' if another active booking overlaps.
            await db.execute(
                "INSERT INTO bookings (id, reference, unit_id, guest_id, check_in, check_out,"
                " status, total_kobo, hold_expires_at, created_at)"
                " VALUES (?,?,?,?,?,?, 'held', ?,?,?)",
                (
                    str(uuid4()), reference, unit["id"], guest_id,
                    req.check_in.isoformat(), req.check_out.isoformat(),
                    total, iso(expires), iso(now),
                ),
            )
    except sqlite3.IntegrityError as exc:
        if "booking_overlap" in str(exc):
            alternatives = await list_available(db, req.check_in, req.check_out, req.guests)
            raise DatesUnavailable([a for a in alternatives if a.unit_id != req.unit_id]) from exc
        raise
    return CreateHoldResponse(
        booking_reference=reference, unit_name=unit["name"], total_kobo=total, hold_expires_at=expires
    )


async def initialize_payment(
    db: aiosqlite.Connection, req: InitializePaymentRequest
) -> CreatePaymentLinkResponse:
    hold = await create_hold(db, req)
    try:
        return await start_payment(db, hold.booking_reference)
    except DomainError:
        async with write_tx(db):
            await db.execute(
                "UPDATE bookings SET status = 'expired' "
                "WHERE reference = ? AND status = 'held'",
                (hold.booking_reference,),
            )
        raise


# -------------------------------------------------------------- payments
_BOOKING_SQL = """
SELECT b.*, u.name AS unit_name, g.whatsapp_number AS guest_whatsapp
FROM bookings b
JOIN units u ON u.id = b.unit_id
JOIN guests g ON g.id = b.guest_id
WHERE b.reference = ?
"""


async def start_payment(db: aiosqlite.Connection, reference: str) -> CreatePaymentLinkResponse:
    now = utcnow()
    async with write_tx(db):
        b = await _one(db, _BOOKING_SQL, (reference,))
        if b is None:
            raise NotFound("Booking not found.")
        if b["status"] != "held" or b["hold_expires_at"] <= iso(now):
            raise Conflict(
                "This hold is no longer active. Check availability and create a new hold."
            )
        existing = await _one(
            db,
            "SELECT * FROM payments WHERE booking_id = ? AND status = 'pending'"
            " AND authorization_url IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (b["id"],),
        )
        if existing:  # idempotent: reuse the link instead of creating a second charge
            return CreatePaymentLinkResponse(
                booking_reference=reference,
                authorization_url=existing["authorization_url"],
                amount_kobo=existing["amount_kobo"],
                hold_expires_at=datetime.fromisoformat(b["hold_expires_at"]),
            )
        pay_ref = f"{reference}-{secrets.token_hex(3)}"
        await db.execute(
            "INSERT INTO payments (id, booking_id, paystack_reference, amount_kobo, created_at)"
            " VALUES (?,?,?,?,?)",
            (str(uuid4()), b["id"], pay_ref, b["total_kobo"], iso(now)),
        )

    # Network call happens outside the transaction: never hold the write lock on I/O.
    digits = re.sub(r"\D", "", b["guest_whatsapp"])
    try:
        data = await paystack.initialize_transaction(
            email=f"guest{digits}@example.com",
            amount_kobo=b["total_kobo"],
            reference=pay_ref,
            metadata={"booking_reference": reference},
        )
    except paystack.PaystackError as exc:
        async with write_tx(db):
            await db.execute(
                "UPDATE payments SET status = 'failed', note = ? WHERE paystack_reference = ?",
                (f"initialize failed: {exc}", pay_ref),
            )
        raise UpstreamError(f"Could not create a payment link right now: {exc}") from exc

    async with write_tx(db):
        await db.execute(
            "UPDATE payments SET authorization_url = ? WHERE paystack_reference = ?",
            (data["authorization_url"], pay_ref),
        )
    return CreatePaymentLinkResponse(
        booking_reference=reference,
        authorization_url=data["authorization_url"],
        amount_kobo=b["total_kobo"],
        hold_expires_at=datetime.fromisoformat(b["hold_expires_at"]),
    )


async def _settle_tx(
    db: aiosqlite.Connection, pay_ref: str, provider_status: str | None,
    paid_kobo: int | None, currency: str | None,
) -> str:
    """Settle a verified payment. Safe to call any number of times."""
    now = utcnow()
    async with write_tx(db):
        p = await _one(db, "SELECT * FROM payments WHERE paystack_reference = ?", (pay_ref,))
        if p is None:
            return "unknown_reference"
        if p["status"] in ("success", "requires_resolution"):
            return "already_processed"
        if provider_status in ("failed", "reversed", "abandoned"):
            new = "abandoned" if provider_status == "abandoned" else "failed"
            await db.execute("UPDATE payments SET status = ? WHERE id = ?", (new, p["id"]))
            pending = await _one(
                db,
                "SELECT 1 FROM payments WHERE booking_id = ? AND status = 'pending' LIMIT 1",
                (p["booking_id"],),
            )
            if pending is None:
                await db.execute(
                    "UPDATE bookings SET status = 'expired' "
                    "WHERE id = ? AND status = 'held'",
                    (p["booking_id"],),
                )
            return new
        if provider_status != "success":
            return "pending"

        b = await _one(db, "SELECT * FROM bookings WHERE id = ?", (p["booking_id"],))
        problem = None
        if paid_kobo != p["amount_kobo"] or (currency or "NGN") != p["currency"]:
            problem = f"amount mismatch: expected {p['amount_kobo']} {p['currency']}, got {paid_kobo} {currency}"
        elif b["status"] in ("held", "expired"):
            try:
                # Re-activating an expired booking is allowed ONLY if the dates are
                # still free; the trigger decides, not application code.
                await db.execute(
                    "UPDATE bookings SET status = 'confirmed', confirmed_at = ?,"
                    " hold_expires_at = NULL WHERE id = ?",
                    (iso(now), b["id"]),
                )
            except sqlite3.IntegrityError as exc:
                if "booking_overlap" not in str(exc):
                    raise
                problem = "paid after the hold expired and the dates were booked by someone else"
        elif b["status"] == "confirmed":
            problem = "duplicate payment: booking was already confirmed by another payment"
        else:
            problem = f"booking is {b['status']}"

        if problem is None:
            await db.execute(
                "UPDATE payments SET status = 'success', paid_amount_kobo = ?, verified_at = ? WHERE id = ?",
                (paid_kobo, iso(now), p["id"]),
            )
            return "confirmed"

        await db.execute(
            "UPDATE payments SET status = 'requires_resolution', paid_amount_kobo = ?,"
            " verified_at = ?, note = ? WHERE id = ?",
            (paid_kobo, iso(now), problem, p["id"]),
        )
        return "requires_resolution"


async def settle_payment(
    db: aiosqlite.Connection, pay_ref: str, provider_status: str | None,
    paid_kobo: int | None, currency: str | None,
) -> str:
    return await _settle_tx(db, pay_ref, provider_status, paid_kobo, currency)


async def reconcile(db: aiosqlite.Connection, pay_ref: str) -> str:
    """Never trust the guest or the webhook body: ask Paystack for the truth."""
    try:
        data = await paystack.verify_transaction(pay_ref)
    except paystack.PaystackError as exc:
        raise UpstreamError(f"Could not verify payment with Paystack: {exc}") from exc
    return await settle_payment(db, pay_ref, data.get("status"), data.get("amount"), data.get("currency"))


async def record_webhook(db: aiosqlite.Connection, event_id: str, event_type: str, payload: str) -> bool:
    """True if this event still needs processing. Duplicates already processed return False."""
    async with write_tx(db):
        await db.execute(
            "INSERT OR IGNORE INTO webhook_events (id, provider, event_id, event_type, payload, received_at)"
            " VALUES (?, 'paystack', ?, ?, ?, ?)",
            (str(uuid4()), event_id, event_type, payload, iso(utcnow())),
        )
        row = await _one(
            db, "SELECT processed_at FROM webhook_events WHERE provider = 'paystack' AND event_id = ?",
            (event_id,),
        )
    return row["processed_at"] is None


async def mark_webhook_processed(db: aiosqlite.Connection, event_id: str) -> None:
    async with write_tx(db):
        await db.execute(
            "UPDATE webhook_events SET processed_at = ? WHERE provider = 'paystack' AND event_id = ?",
            (iso(utcnow()), event_id),
        )


# ---------------------------------------------------------------- status
def _message(row, status: BookingStatus, pay: PaymentStatus | None) -> str:
    unit, ci, co = row["unit_name"], row["check_in"], row["check_out"]
    if pay == PaymentStatus.REQUIRES_RESOLUTION:
        return (
            "We received a payment but could not confirm this booking (the dates were taken "
            "or the amount did not match). Do not pay again. The host has been alerted and "
            "will arrange a refund or alternative dates."
        )
    if status == BookingStatus.CONFIRMED:
        return f"Booking confirmed: {unit}, {ci} to {co}. {naira(row['total_kobo'])} paid. Reference {row['reference']}."
    if status == BookingStatus.HELD:
        until = _wat(row["hold_expires_at"])
        if pay == PaymentStatus.FAILED:
            return (
                f"A payment attempt failed. The dates remain held until {until} "
                "while another payment attempt is pending."
            )
        if pay == PaymentStatus.PENDING:
            return f"Dates held until {until}. Waiting for payment; it is not confirmed yet."
        return f"Dates held until {until}. No payment link has been created yet."
    if status == BookingStatus.EXPIRED:
        return (
            "The hold on these dates has been released and no payment was confirmed. "
            "If you already paid, it may still be processing; check again shortly. "
            "Otherwise start a new booking."
        )
    return "This booking was cancelled."


async def booking_status(
    db: aiosqlite.Connection, reference: str, reconcile_pending: bool = True
) -> BookingStatusResponse:
    row = await _one(db, _BOOKING_SQL, (reference,))
    if row is None:
        raise NotFound("Booking not found.")

    async def latest_payment():
        return await _one(
            db,
            "SELECT * FROM payments WHERE booking_id = ? ORDER BY CASE status"
            " WHEN 'success' THEN 0 WHEN 'requires_resolution' THEN 1 ELSE 2 END, created_at DESC LIMIT 1",
            (row["id"],),
        )

    pay = await latest_payment()
    # If the webhook is late or lost, settle straight from Paystack when the guest asks.
    if (reconcile_pending and paystack.configured() and pay is not None
            and pay["status"] == "pending" and row["status"] in ("held", "expired")):
        try:
            await reconcile(db, pay["paystack_reference"])
        except UpstreamError:
            log.exception("reconcile on status check failed")
        row = await _one(db, _BOOKING_SQL, (reference,))
        pay = await latest_payment()

    status = BookingStatus(row["status"])
    if status == BookingStatus.HELD and row["hold_expires_at"] <= iso(utcnow()):
        status = BookingStatus.EXPIRED  # lapsed, sweeper just hasn't run yet
    pay_status = PaymentStatus(pay["status"]) if pay else None
    return BookingStatusResponse(
        booking_reference=reference,
        booking_status=status,
        payment_status=pay_status,
        unit_name=row["unit_name"],
        check_in=row["check_in"],
        check_out=row["check_out"],
        total_kobo=row["total_kobo"],
        message=_message(row, status, pay_status),
    )


# ------------------------------------------------------------- dev only
async def latest_payment_ref(db: aiosqlite.Connection, booking_reference: str) -> tuple[str, int]:
    row = await _one(
        db,
        "SELECT p.paystack_reference, p.amount_kobo FROM payments p JOIN bookings b ON b.id = p.booking_id"
        " WHERE b.reference = ? ORDER BY p.created_at DESC LIMIT 1",
        (booking_reference,),
    )
    if row is None:
        raise NotFound("No payment exists for that booking.")
    return row["paystack_reference"], row["amount_kobo"]


async def booking_reference_for_payment(
    db: aiosqlite.Connection, paystack_reference: str
) -> str | None:
    row = await _one(
        db,
        "SELECT b.reference FROM bookings b JOIN payments p ON p.booking_id = b.id "
        "WHERE p.paystack_reference = ?",
        (paystack_reference,),
    )
    return row["reference"] if row is not None else None


async def force_expire(db: aiosqlite.Connection, booking_reference: str) -> None:
    """Backdate a hold WITHOUT changing its status, to exercise the stale-hold path."""
    async with write_tx(db):
        await db.execute(
            "UPDATE bookings SET hold_expires_at = ? WHERE reference = ? AND status = 'held'",
            (iso(utcnow() - timedelta(minutes=1)), booking_reference),
        )