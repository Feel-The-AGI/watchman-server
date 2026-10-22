"""Stripe subscription state.

Paystack charges stay in app.routes.payments. This module only decides what a
Stripe subscription is worth: which plan, when Pro ends, and whether a
founding seat counts.
"""

from datetime import datetime, timedelta
from typing import Optional

from app.billing import as_utc, parse_timestamp

FOUNDING_LIMIT = 100
# Webhook lag after a renewal should not flicker Pro off for a few hours.
ACCESS_GRACE = timedelta(hours=6)
PLANS = ("monthly", "yearly", "founding")
PAYMENTS_NOT_CONFIGURED = "Payments not configured"
FOUNDING_FULL = "Founding 100 is full"

OPEN_STATUSES = {"active", "past_due", "unpaid", "paused"}
ENDED_STATUSES = {"canceled", "cancelled", "deleted"}


def seats_left(taken: int) -> int:
    return max(0, FOUNDING_LIMIT - int(taken))


def enum_status(stripe_status: str) -> str:
    """Map a Stripe status onto the subscriptions.status enum."""
    if stripe_status in {"active", "trialing"}:
        return "active"
    if stripe_status in {"past_due", "unpaid", "paused", "incomplete"}:
        return "past_due"
    if stripe_status in ENDED_STATUSES or stripe_status == "incomplete_expired":
        return "cancelled"
    return "expired"


def normalize_stripe_status(status: str) -> str:
    if status in {"cancelled", "deleted"}:
        return "canceled"
    return status or ""


def preserve_existing_grant(
    user: dict,
    tier: str,
    expires: Optional[datetime],
    period_end: Optional[datetime],
    stored: Optional[dict],
    now: Optional[datetime] = None,
) -> tuple[str, Optional[datetime]]:
    """Keep a Pro expiry that is longer than this Stripe period plus grace.

    Paystack still writes pro_expires_at. A Stripe period must not cut that
    time off. An expiry that is only the Stripe grace past the stored period
    is this subscription, so a deletion still ends it.
    """
    if user.get("tier") != "pro":
        return tier, expires
    current = parse_timestamp(user.get("pro_expires_at"))
    moment = as_utc(now)
    if current is None or current <= moment:
        return tier, expires
    if expires is not None and current <= as_utc(expires):
        return tier, expires
    stored_end = parse_timestamp((stored or {}).get("current_period_end"))
    if stored_end is not None:
        reference = stored_end + ACCESS_GRACE
    elif period_end is not None:
        reference = as_utc(period_end) + ACCESS_GRACE
    else:
        return tier, expires
    if current > reference:
        return "pro", current
    return tier, expires


def access_for(
    status: str,
    period_end: Optional[datetime],
    now: Optional[datetime] = None,
) -> tuple[str, Optional[datetime]]:
    """Return (tier, pro_expires_at) from a Stripe subscription status.

    Active, past_due, and unpaid keep Pro until the period ends, plus a short
    grace so a late renewal webhook does not drop access. There is no trial.
    A canceled or deleted subscription ends at the period end with no grace.
    """
    current = as_utc(now)
    normalized = normalize_stripe_status(status)
    end = as_utc(period_end) if period_end is not None else None
    if normalized in OPEN_STATUSES and end is not None:
        until = end + ACCESS_GRACE
        return ("pro", until) if until > current else ("free", until)
    if normalized == "canceled":
        if end is not None and end > current:
            return "pro", end
        return "free", end if end is not None else current
    return "free", end


def counts_as_founding_seat(status: str) -> bool:
    return normalize_stripe_status(status) in {"active", "past_due", "unpaid", "paused", "canceled"}


def unix_to_datetime(value) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=as_utc().tzinfo)
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=as_utc().tzinfo)
    return parse_timestamp(text)


def object_id(value) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        inner = value.get("id")
        if isinstance(inner, str) and inner:
            return inner
    return None


def _item_period(subscription: dict, field: str) -> Optional[datetime]:
    items = subscription.get("items") or {}
    data = items.get("data") if isinstance(items, dict) else None
    if not data:
        return None
    return unix_to_datetime(data[0].get(field))


def subscription_period_end(subscription: dict) -> Optional[datetime]:
    """Read current_period_end from the subscription or its first item.

    Newer Stripe API versions moved the period onto the subscription item.
    """
    direct = unix_to_datetime(subscription.get("current_period_end"))
    if direct is not None:
        return direct
    return _item_period(subscription, "current_period_end")


def subscription_period_start(subscription: dict) -> Optional[datetime]:
    direct = unix_to_datetime(subscription.get("current_period_start"))
    if direct is not None:
        return direct
    return _item_period(subscription, "current_period_start")


def price_id_from_subscription(subscription: dict) -> Optional[str]:
    items = subscription.get("items") or {}
    data = items.get("data") if isinstance(items, dict) else None
    if not data:
        return None
    price = data[0].get("price")
    if isinstance(price, str) and price:
        return price
    if isinstance(price, dict) and price.get("id"):
        return price["id"]
    pricing = data[0].get("pricing") or {}
    details = pricing.get("price_details") or {}
    if isinstance(details, dict) and details.get("price"):
        return details["price"]
    return None


def resolve_plan(subscription: dict, price_map: dict) -> Optional[str]:
    """Prefer the Price id. Metadata covers events that do not include a price."""
    price = price_id_from_subscription(subscription)
    if price and price in price_map:
        mapped = price_map[price]
        if mapped in PLANS:
            return mapped
    metadata = subscription.get("metadata") or {}
    plan = metadata.get("plan") if isinstance(metadata, dict) else None
    if plan in PLANS:
        return plan
    return None


def is_stale(
    stored: Optional[dict],
    incoming_subscription_id: Optional[str],
    incoming_end: Optional[datetime],
    incoming_status: str,
) -> bool:
    """Ignore an older event that would undo a newer period or a cancellation."""
    if not stored:
        return False
    stored_end = parse_timestamp(stored.get("current_period_end"))
    stored_sub = stored.get("stripe_subscription_id")
    stored_status = normalize_stripe_status(stored.get("stripe_status") or stored.get("status") or "")
    incoming = normalize_stripe_status(incoming_status)
    incoming_end = as_utc(incoming_end) if incoming_end is not None else None
    if stored_sub and incoming_subscription_id and stored_sub != incoming_subscription_id:
        if stored_end and incoming_end and incoming_end < stored_end:
            return True
        return False
    # A deleted subscription id does not reopen. A new subscription id is a new purchase.
    if stored_status == "canceled" and incoming != "canceled":
        return True
    if stored_end and incoming_end and incoming_end < stored_end and incoming != "canceled":
        return True
    return False


def _line_price(line: dict) -> Optional[str]:
    price = line.get("price")
    if isinstance(price, str) and price:
        return price
    if isinstance(price, dict) and price.get("id"):
        return price["id"]
    pricing = line.get("pricing") or {}
    details = pricing.get("price_details") or {}
    if isinstance(details, dict) and details.get("price"):
        return details["price"]
    return None


def invoice_subscription_id(invoice: dict) -> Optional[str]:
    direct = object_id(invoice.get("subscription"))
    if direct:
        return direct
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") or {}
    if isinstance(details, dict):
        return object_id(details.get("subscription"))
    return None


def subscription_from_invoice(invoice: dict) -> dict:
    """Turn a paid invoice into a subscription-shaped dict for the same sync path."""
    lines = (invoice.get("lines") or {}).get("data") or []
    period_end = None
    period_start = None
    price = None
    for line in lines:
        period = line.get("period") or {}
        end = period.get("end")
        end_number = end if isinstance(end, (int, float)) else None
        current = period_end if isinstance(period_end, (int, float)) else None
        if end_number is not None and (current is None or end_number >= current):
            period_end = end
            period_start = period.get("start")
        if price is None:
            price = _line_price(line)
    metadata = dict(invoice.get("metadata") or {})
    parent = (invoice.get("parent") or {}).get("subscription_details") or {}
    if isinstance(parent, dict) and isinstance(parent.get("metadata"), dict):
        merged = dict(parent["metadata"])
        merged.update(metadata)
        metadata = merged
    items = {"data": [{"price": {"id": price}}]} if price else {"data": []}
    return {
        "id": invoice_subscription_id(invoice),
        "object": "subscription",
        "customer": object_id(invoice.get("customer")),
        "status": "active",
        "current_period_end": period_end,
        "current_period_start": period_start,
        "cancel_at_period_end": False,
        "metadata": metadata,
        "items": items,
    }
