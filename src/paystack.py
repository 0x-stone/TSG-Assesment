"""Thin Paystack client (test mode with an sk_test_ key).
"""
import hashlib
import hmac
import os

import httpx

BASE_URL = "https://api.paystack.co"


class PaystackError(Exception):
    pass


def secret_key() -> str:
    return os.getenv("PAYSTACK_SECRET_KEY", "")


def configured() -> bool:
    return bool(secret_key())





def signature_valid(raw_body: bytes, header_value: str) -> bool:
    """Paystack signs the raw request body with HMAC-SHA512 using the secret key."""
    key = secret_key()
    if not key or not header_value:
        return False
    expected = hmac.new(key.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, header_value)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {secret_key()}", "Content-Type": "application/json"}


async def _call(method: str, path: str, **kwargs) -> dict:
    if not configured():
        raise PaystackError("PAYSTACK_SECRET_KEY is not set")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.request(method, f"{BASE_URL}{path}", headers=_headers(), **kwargs)
    except httpx.HTTPError as exc:
        raise PaystackError(f"Could not reach Paystack: {exc!r}") from exc
    try:
        body = resp.json()
    except ValueError:
        raise PaystackError(f"Paystack returned non-JSON (HTTP {resp.status_code})")
    if resp.status_code != 200 or not body.get("status"):
        raise PaystackError(body.get("message") or f"Paystack HTTP {resp.status_code}")
    return body["data"]


async def initialize_transaction(
    *, email: str, amount_kobo: int, reference: str, metadata: dict | None = None
) -> dict:
    payload = {
        "email": email,
        "amount": amount_kobo,  # kobo
        "currency": "NGN",
        "reference": reference,
        "metadata": metadata or {},
    }
    callback = os.getenv("PAYSTACK_CALLBACK_URL")
    if callback:
        payload["callback_url"] = callback
    return await _call("POST", "/transaction/initialize", json=payload)


async def verify_transaction(reference: str) -> dict:
    """Returns Paystack's transaction object: status, amount (kobo), currency, ..."""
    return await _call("GET", f"/transaction/verify/{reference}")