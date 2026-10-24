"""Pine Labs Online (Plural) Pay by Link: create a checkout URL for an approved BOM.

Docs: https://www.pinelabs.com/docs/online-payments/payment-links/create-via-apis
Called only from the backend, as Pine Labs requires. Defaults to the UAT (test)
environment; set PINELABS_BASE_URL=https://api.pluralpay.in for production.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

_token: dict = {}


class PineLabsError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.getenv("PINELABS_CLIENT_ID") and os.getenv("PINELABS_CLIENT_SECRET"))


def _base() -> str:
    return os.getenv("PINELABS_BASE_URL", "https://pluraluat.v2.pinepg.in").rstrip("/")


def _headers() -> dict:
    return {
        "Request-ID": uuid.uuid4().hex,
        "Request-Timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "Content-Type": "application/json",
    }


async def _access_token(http: httpx.AsyncClient) -> str:
    if _token.get("value") and time.time() < _token.get("expires", 0) - 60:
        return _token["value"]
    r = await http.post(f"{_base()}/api/auth/v1/token", headers=_headers(), json={
        "client_id": os.environ["PINELABS_CLIENT_ID"],
        "client_secret": os.environ["PINELABS_CLIENT_SECRET"],
        "grant_type": "client_credentials",
    })
    if r.status_code >= 300:
        raise PineLabsError(f"Pine Labs token {r.status_code}: {r.text[:300]}")
    data = r.json()
    expires = time.time() + 600
    try:
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        pass
    _token.update(value=data["access_token"], expires=expires)
    return _token["value"]


async def create_payment_link(amount_inr: float, reference: str, description: str,
                              callback_url: str | None = None, mobile: str | None = None) -> dict:
    """Returns Pine Labs' payment link object (payment_link, payment_link_id, status, ...).
    With `mobile` (10-digit Indian number), Pine Labs sends the link to the customer."""
    if not configured():
        raise PineLabsError("Pine Labs is not configured (PINELABS_CLIENT_ID/SECRET)")
    body = {
        "amount": {"value": int(round(amount_inr * 100)), "currency": "INR"},
        "description": description[:200],
        "merchant_payment_link_reference": reference[:50],
        "expire_by": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "allowed_payment_methods": ["UPI", "CARD", "NETBANKING"],
    }
    if callback_url:
        body["callback_url"] = callback_url
    if mobile:
        body["customer"] = {"mobile_number": mobile, "country_code": "91"}
    async with httpx.AsyncClient(timeout=30) as http:
        token = await _access_token(http)
        r = await http.post(f"{_base()}/api/pay/v1/paymentlink",
                            headers={**_headers(), "Authorization": f"Bearer {token}"}, json=body)
    if r.status_code >= 300:
        raise PineLabsError(f"Pine Labs payment link {r.status_code}: {r.text[:300]}")
    return r.json()


async def get_payment_link(payment_link_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as http:
        token = await _access_token(http)
        r = await http.get(f"{_base()}/api/pay/v1/paymentlink/{payment_link_id}",
                           headers={**_headers(), "Authorization": f"Bearer {token}"})
    if r.status_code >= 300:
        raise PineLabsError(f"Pine Labs payment link status {r.status_code}: {r.text[:300]}")
    return r.json()
