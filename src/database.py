import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import json

import aiosqlite

DATABASE_PATH = Path(
    os.getenv("DATABASE_PATH") or Path(__file__).resolve().parent.parent / "hotel.db"
)


def iso(dt: datetime) -> str:
    """Fixed-width UTC timestamp so string comparison == time comparison."""
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
  id                TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  description       TEXT NOT NULL DEFAULT '',
  bedrooms          INTEGER NOT NULL CHECK (bedrooms >= 0),
  max_guests        INTEGER NOT NULL CHECK (max_guests >= 1),
  nightly_rate_kobo INTEGER NOT NULL CHECK (nightly_rate_kobo > 0),
  amenities         TEXT NOT NULL DEFAULT '[]',
  house_rules       TEXT NOT NULL DEFAULT '',
  is_active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS guests (
  id              TEXT PRIMARY KEY,
  whatsapp_number TEXT NOT NULL UNIQUE,
  name            TEXT,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
  id              TEXT PRIMARY KEY,
  reference       TEXT NOT NULL UNIQUE,
  unit_id         TEXT NOT NULL REFERENCES units(id),
  guest_id        TEXT NOT NULL REFERENCES guests(id),
  check_in        TEXT NOT NULL,
  check_out       TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'held'
                  CHECK (status IN ('held','confirmed','expired','cancelled')),
  total_kobo      INTEGER NOT NULL CHECK (total_kobo > 0),
  hold_expires_at TEXT,
  created_at      TEXT NOT NULL,
  confirmed_at    TEXT,
  CHECK (check_out > check_in),
  CHECK (status <> 'held' OR hold_expires_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_bookings_unit_status ON bookings (unit_id, status);

CREATE TABLE IF NOT EXISTS payments (
  id                 TEXT PRIMARY KEY,
  booking_id         TEXT NOT NULL REFERENCES bookings(id),
  paystack_reference TEXT NOT NULL UNIQUE,
  amount_kobo        INTEGER NOT NULL CHECK (amount_kobo > 0),
  paid_amount_kobo   INTEGER,
  currency           TEXT NOT NULL DEFAULT 'NGN',
  status             TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','success','failed','abandoned','requires_resolution')),
  authorization_url  TEXT,
  note               TEXT,
  created_at         TEXT NOT NULL,
  verified_at        TEXT
);

CREATE TABLE IF NOT EXISTS webhook_events (
  id           TEXT PRIMARY KEY,
  provider     TEXT NOT NULL DEFAULT 'paystack',
  event_id     TEXT NOT NULL,
  event_type   TEXT NOT NULL,
  payload      TEXT NOT NULL,
  received_at  TEXT NOT NULL,
  processed_at TEXT,
  UNIQUE (provider, event_id)
);

-- The core guarantee. No two ACTIVE bookings (held/confirmed) for the same
-- unit may overlap. Half-open ranges: check_out == other.check_in is fine.
CREATE TRIGGER IF NOT EXISTS bookings_no_overlap_insert
BEFORE INSERT ON bookings
WHEN NEW.status IN ('held','confirmed')
BEGIN
  SELECT RAISE(ABORT, 'booking_overlap')
  WHERE EXISTS (
    SELECT 1 FROM bookings b
    WHERE b.unit_id = NEW.unit_id
      AND b.status IN ('held','confirmed')
      AND b.check_in < NEW.check_out
      AND b.check_out > NEW.check_in
  );
END;

-- Same rule when a row is (re)activated, e.g. a late payment confirming an
-- expired booking whose dates may meanwhile have been taken.
CREATE TRIGGER IF NOT EXISTS bookings_no_overlap_update
BEFORE UPDATE OF status, unit_id, check_in, check_out ON bookings
WHEN NEW.status IN ('held','confirmed')
BEGIN
  SELECT RAISE(ABORT, 'booking_overlap')
  WHERE EXISTS (
    SELECT 1 FROM bookings b
    WHERE b.id <> NEW.id
      AND b.unit_id = NEW.unit_id
      AND b.status IN ('held','confirmed')
      AND b.check_in < NEW.check_out
      AND b.check_out > NEW.check_in
  );
END;
"""

SEED_UNITS = [
    {
        "name": "Palm Suite",
        "description": "Spacious 2-bedroom apartment in Lekki Phase 1, 5 minutes from the beach road.",
        "bedrooms": 2,
        "max_guests": 4,
        "nightly_rate_kobo": 12_000_000,  # NGN 120,000
        "amenities": ["Wi-Fi", "Air conditioning", "24/7 power (inverter)", "Smart TV", "Kitchen", "Parking"],
        "house_rules": "No parties. No smoking indoors. Quiet hours 10pm-7am.",
    },
    {
        "name": "Marina One-Bed",
        "description": "Modern 1-bedroom with a balcony and lagoon view.",
        "bedrooms": 1,
        "max_guests": 2,
        "nightly_rate_kobo": 8_500_000,  # NGN 85,000
        "amenities": ["Wi-Fi", "Air conditioning", "24/7 power (inverter)", "Smart TV", "Balcony"],
        "house_rules": "No parties. No smoking indoors. Quiet hours 10pm-7am.",
    },
    {
        "name": "Coral Studio",
        "description": "Compact, comfortable studio, ideal for solo travellers and short work trips.",
        "bedrooms": 0,
        "max_guests": 2,
        "nightly_rate_kobo": 6_500_000,  # NGN 65,000
        "amenities": ["Wi-Fi", "Air conditioning", "Backup power", "Workspace"],
        "house_rules": "No parties. No smoking indoors. Quiet hours 10pm-7am.",
    },
]


async def connect() -> aiosqlite.Connection:
    # isolation_level=None -> autocommit; we issue BEGIN/COMMIT explicitly.
    db = await aiosqlite.connect(DATABASE_PATH, isolation_level=None)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA busy_timeout = 5000")
    return db


async def init_db() -> None:
    db = await connect()
    try:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.executescript(SCHEMA)
        cur = await db.execute("SELECT COUNT(*) FROM units")
        if (await cur.fetchone())[0] == 0:
            for u in SEED_UNITS:
                await db.execute(
                    "INSERT INTO units (id, name, description, bedrooms, max_guests,"
                    " nightly_rate_kobo, amenities, house_rules)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        str(uuid4()), u["name"], u["description"], u["bedrooms"],
                        u["max_guests"], u["nightly_rate_kobo"],
                        json.dumps(u["amenities"]), u["house_rules"],
                    ),
                )
    finally:
        await db.close()


async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """FastAPI dependency: one fresh connection per request."""
    db = await connect()
    try:
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def write_tx(db: aiosqlite.Connection) -> AsyncIterator[None]:
    """BEGIN IMMEDIATE takes the write lock up front, so writers queue
    (up to busy_timeout) instead of failing mid-transaction."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if db.in_transaction:
            await db.execute("ROLLBACK")
        raise
    else:
        await db.execute("COMMIT")