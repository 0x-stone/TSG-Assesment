# StayMate

A WhatsApp booking assistant for a shortlet operator, built on Chord for the TSG Labs build exercise.

A guest can browse apartments, check dates, agree to book, pay through a Paystack checkout link, and ask whether the booking is confirmed. The Chord teammate does the talking. A FastAPI backend decides what is available, what is paid and what is confirmed.

Everything here runs in test mode with made-up guests and a made-up business.

## The problem

A small shortlet host on WhatsApp spends the day answering "is it free on these dates?" and checking Paystack by hand to see who has paid. Slow replies lose bookings. Two guests promised the same apartment means a refund and a bad review. StayMate handles that loop and never confirms a booking on a guest's word alone.

## How it works

```text
Guest (WhatsApp simulator)
        |
Chord teammate  (shortlet-booking skill + 4 tools)
        |
FastAPI backend ---- SQLite
        |
     Paystack  --signed webhook-->  backend settles the booking
```

1. `list_rooms` returns the apartments.
2. `check_availability` checks dates and guest count. It is read-only and reserves nothing.
3. The guest picks a unit and clearly agrees to book. `initialize_payment` rechecks availability, creates a short hold and returns a Paystack checkout link with the hold expiry.
4. Paystack calls the backend webhook. The backend verifies the signature, then verifies the transaction with Paystack before settling anything.
5. `get_booking_status` returns the real state by booking reference. The agent only reports what the backend says.

The Paystack webhook is server-to-server. It is not a tool the teammate can call.

## Tools

| Tool | Endpoint | Notes |
|---|---|---|
| `list_rooms` | `GET /rooms` | No parameters |
| `check_availability` | `GET /availability` | `check_in`, `check_out` (YYYY-MM-DD), `guests` |
| `initialize_payment` | `POST /payments/initialize` | `check_in`, `check_out`, `unit_id`, `guest_whatsapp`, `guests`, optional `guest_name`, and `booking_intent_confirmed: true` only after the guest agrees |
| `get_booking_status` | `GET /bookings/{reference}` | Exact reference from the backend |

Also: `POST /webhooks/paystack` (Paystack only) and `GET /health`.

## Booking safety rules

- **No double bookings.** The hold is created inside a database write transaction, and an overlap constraint rejects two active bookings for the same unit and dates.
- **Nothing is confirmed on trust.** A checkout link, a screenshot or "I've paid" does not confirm a booking. Only a verified payment does.
- **Consent first.** Checkout starts only after the guest agrees.
- **Failed payments free the dates.** A failed, reversed or abandoned payment releases the hold at once by marking the booking `expired`. If another payment attempt on that booking is still pending, the hold stays until it resolves. `charge.failed` webhooks are verified with Paystack before the booking changes. The guest is told to start a new booking.
- **Late payments are flagged, not double-confirmed.** If money arrives after a hold expired and the dates were taken, the booking is not confirmed. The payment is marked for manual resolution for follow-up.
- **Duplicate webhooks are ignored.** Each event is recorded once and cannot settle a payment twice.
- **Abuse limits.** One active hold per guest record, and a cooldown after three expired unpaid holds in 60 minutes (15-minute cooldown).
- The agent never asks for card details, PINs or OTPs in chat.

## Run it

Requirements: Docker, a Paystack **test** account, and a public URL for the webhook.

1. Clone the repo.
2. Copy `.env.example` to `.env` and put your Paystack **test** secret key in it, along with the other values listed there. Never commit `.env` or a real key.
3. Build the image: `docker build -t staymate .`
4. Run it on port 8000: `docker run --env-file .env -p 8000:8000 staymate`
5. Open `http://localhost:8000/docs` and check `/health`, `/rooms` and `/availability`.
6. Set your public URL as the Paystack test webhook: `https://<your-url>/webhooks/paystack`
7. In Chord, register the four tools against your backend URL and add the `shortlet-booking` skill to the teammate.

The SQLite database is a file inside the container. Mount a volume at its location, or the data is lost when the container is removed.

Optional settings: `HOLD_SECONDS` sets how long a hold lasts. It is currently set to 3 minutes (180 seconds) so expiry is easy to show in a demo. A real operator would want longer

## What I tested

Against the deployed backend, all of these passed:

- health, room listing and date-specific availability (no hold created)
- the full booking and payment flow in the WhatsApp simulator
- "pending" status reported before payment
- hold released after a failed or abandoned payment, with the dates open again
- two overlapping booking attempts: only one succeeds
- the same webhook delivered twice: the payment settles once
- checkout never starts before the guest agrees

## Known limits

- **Guest identity is self-reported.** The backend identifies a guest by the phone number typed into the request. I couldn't find a verified sender ID passed to tools, so someone could type another number to get around the hold limits. For that reason there is no "my bookings" lookup. The fix is a verified sender ID from the platform.
- **SQLite** is fine for one server and a demo. It is not a multi-instance setup. Move to a managed database before real volume.
- **No cancellations, refunds or host dashboard.**
- Test mode only. See the go-live note for what has to change for real guests.

## How I used AI

I used AI assistants to help draft code, tests and documentation, and I reviewed and edited what they produced. The agent itself is built and runs on Chord.