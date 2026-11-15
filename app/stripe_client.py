"""Small Stripe client: form-encoded API calls and webhook signature checks.

Only the handful of endpoints Watchman uses are called. Requests pin one API
version. Webhook events arrive in whatever version the endpoint was created
with, so the readers below accept the older and the newer field layout.
"""

import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from urllib.parse import urlencode

STRIPE_API = "https://api.stripe.com/v1"
STRIPE_VERSION = "2024-06-20"
SIGNATURE_TOLERANCE_SECONDS = 300


class StripeError(Exception):
    """A Stripe call failed. ``message`` is safe to log, not to show raw."""

    def __init__(self, message: str, status: int = 0, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def encode_form(data: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten nested dicts and lists the way Stripe expects: a[b][0][c]=v."""
    pairs: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if value is None:
                continue
            name = f"{prefix}[{key}]" if prefix else str(key)
            pairs.extend(encode_form(value, name))
    elif isinstance(data, (list, tuple)):
        for index, value in enumerate(data):
            pairs.extend(encode_form(value, f"{prefix}[{index}]"))
    elif isinstance(data, bool):
        pairs.append((prefix, "true" if data else "false"))
    else:
        pairs.append((prefix, str(data)))
    return pairs


async def stripe_request(
    secret_key: str,
    method: str,
    path: str,
    data: Optional[dict] = None,
    *,
    idempotency_key: Optional[str] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict:
    """Call the Stripe API. Raises StripeError on any failure."""
    if not secret_key:
        raise StripeError("Stripe is not configured", status=503)
    headers = {
        "Authorization": f"Bearer {secret_key}",
        "Stripe-Version": STRIPE_VERSION,
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    params = encode_form(data or {})
    try:
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
            if method == "GET":
                response = await client.get(f"{STRIPE_API}{path}", params=params, headers=headers)
            else:
                response = await client.request(
                    method,
                    f"{STRIPE_API}{path}",
                    content=urlencode(params).encode("ascii"),
                    headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                )
    except httpx.HTTPError as exc:
        raise StripeError(f"Could not reach Stripe: {exc}") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400:
        error = body.get("error") if isinstance(body, dict) else None
        error = error if isinstance(error, dict) else {}
        raise StripeError(
            str(error.get("message") or f"Stripe returned {response.status_code}"),
            status=response.status_code,
            code=error.get("code"),
        )
    return body if isinstance(body, dict) else {}


def verify_signature(
    payload: bytes,
    header: Optional[str],
    secret: str,
    *,
    now: Optional[float] = None,
    tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    """Check a Stripe-Signature header against the raw body.

    The header looks like ``t=1700000000,v1=<hex>,v1=<hex>``. The signed text
    is ``<t>.<body>`` with HMAC-SHA256 and the endpoint secret as the key.
    """
    if not secret or not header:
        return False
    timestamp = None
    signatures = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if not timestamp or not signatures:
        return False
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - signed_at) > tolerance:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + payload,
        hashlib.sha256,
    ).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


def sign_payload(payload: bytes, secret: str, timestamp: Optional[int] = None) -> str:
    """Build a Stripe-Signature header. Used by tests."""
    stamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(
        secret.encode("utf-8"), f"{stamp}.".encode("utf-8") + payload, hashlib.sha256
    ).hexdigest()
    return f"t={stamp},v1={digest}"


def _object_id(value: Any) -> Optional[str]:
    """An id from a field that is either the id or the expanded object."""
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value else None


def from_epoch(value: Any) -> Optional[datetime]:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def subscription_period_end(subscription: dict) -> Optional[datetime]:
    """When the paid period ends. Newer API versions keep it on the items."""
    if not isinstance(subscription, dict):
        return None
    end = from_epoch(subscription.get("current_period_end"))
    if end:
        return end
    items = (subscription.get("items") or {}).get("data") or []
    ends = [from_epoch(item.get("current_period_end")) for item in items if isinstance(item, dict)]
    ends = [value for value in ends if value]
    return max(ends) if ends else None


def subscription_interval(subscription: dict) -> Optional[str]:
    """'month' or 'year' from the first item's price."""
    items = (subscription.get("items") or {}).get("data") or [] if isinstance(subscription, dict) else []
    for item in items:
        price = (item or {}).get("price") or {}
        interval = (price.get("recurring") or {}).get("interval")
        if interval:
            return str(interval)
    return None


def invoice_subscription_id(invoice: dict) -> Optional[str]:
    """The subscription an invoice bills, in either API layout."""
    if not isinstance(invoice, dict):
        return None
    found = _object_id(invoice.get("subscription"))
    if found:
        return found
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") or {}
    found = _object_id(details.get("subscription"))
    if found:
        return found
    for line in (invoice.get("lines") or {}).get("data") or []:
        if not isinstance(line, dict):
            continue
        found = _object_id(line.get("subscription"))
        if found:
            return found
        line_parent = (line.get("parent") or {}).get("subscription_item_details") or {}
        found = _object_id(line_parent.get("subscription"))
        if found:
            return found
    return None


def invoice_period_end(invoice: dict) -> Optional[datetime]:
    """The latest line period end on an invoice."""
    lines = (invoice.get("lines") or {}).get("data") or [] if isinstance(invoice, dict) else []
    ends = [from_epoch(((line or {}).get("period") or {}).get("end")) for line in lines]
    ends = [value for value in ends if value]
    return max(ends) if ends else None


def customer_id(value: dict) -> Optional[str]:
    return _object_id((value or {}).get("customer"))
