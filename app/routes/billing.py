"""
Stripe card subscriptions.

Paystack stays on /api/payments. These routes are inert until STRIPE_SECRET_KEY
and STRIPE_WEBHOOK_SECRET are set and migration 015 has been applied: every
endpoint then returns 503 Payments not configured, and nothing else crashes.

Plans are Stripe Price ids from the environment. Founding is the first 100
people who take that price. The price itself renews at $36; this server only
refuses a new founding checkout once 100 active or past founding seats exist.
"""

import json
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from stripe import SignatureVerificationError, StripeClient, StripeError

from app.config import get_settings
from app.database import BillingStorageUnavailable, Database
from app.middleware.auth import get_current_user, get_effective_tier
from app.billing import parse_timestamp
from app.stripe_billing import (
    ACCESS_GRACE,
    FOUNDING_FULL,
    FOUNDING_LIMIT,
    OPEN_STATUSES,
    PAYMENTS_NOT_CONFIGURED,
    PLANS,
    access_for,
    counts_as_founding_seat,
    enum_status,
    invoice_subscription_id,
    is_stale,
    normalize_stripe_status,
    object_id,
    preserve_existing_grant,
    resolve_plan,
    seats_left,
    subscription_from_invoice,
    subscription_period_end,
    subscription_period_start,
)

router = APIRouter()

HANDLED_EVENTS = {
    "checkout.session.completed",
    "invoice.paid",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}
_portal_config_ids: dict[str, str] = {}


def _setting(name: str) -> str:
    value = getattr(get_settings(), name, "")
    if not isinstance(value, str):
        return ""
    return value.strip()


def _keys_configured() -> bool:
    return bool(_setting("stripe_secret_key") and _setting("stripe_webhook_secret"))


def _client_base() -> str:
    return (_setting("client_base_url") or "https://trywatchman.app").rstrip("/")


def _price_for(plan: str) -> str:
    return _setting({
        "monthly": "stripe_price_monthly",
        "yearly": "stripe_price_yearly",
        "founding": "stripe_price_founding",
    }[plan])


def _price_map() -> dict:
    mapping = {}
    for plan in PLANS:
        price = _price_for(plan)
        if price:
            mapping[price] = plan
    return mapping


def _not_configured() -> HTTPException:
    return HTTPException(status_code=503, detail=PAYMENTS_NOT_CONFIGURED)


def _db() -> Database:
    try:
        return Database(use_admin=True)
    except RuntimeError as exc:
        logger.error(f"[BILLING] Database is not configured: {exc}")
        raise _not_configured() from exc


async def _require_ready(db: Database) -> None:
    if not _keys_configured():
        raise _not_configured()
    try:
        ready = await db.stripe_billing_ready()
    except BillingStorageUnavailable as exc:
        logger.warning(f"[BILLING] Storage check failed: {exc}")
        raise _not_configured() from exc
    if not ready:
        raise _not_configured()


class StripeBillingClient:
    """Thin async wrapper so tests can replace Stripe without a network call."""

    def __init__(self, api_key: str, api_base: str = ""):
        kwargs = {"max_network_retries": 0}
        if api_base:
            kwargs["base_addresses"] = {"api": api_base}
        self._client = StripeClient(api_key, **kwargs)

    async def create_customer(self, params: dict) -> dict:
        obj = await self._client.v1.customers.create_async(params=params)
        return obj.to_dict()

    async def create_checkout_session(self, params: dict) -> dict:
        obj = await self._client.v1.checkout.sessions.create_async(params=params)
        return obj.to_dict()

    async def create_portal_configuration(self, params: dict) -> dict:
        obj = await self._client.v1.billing_portal.configurations.create_async(params=params)
        return obj.to_dict()

    async def create_portal_session(self, params: dict) -> dict:
        obj = await self._client.v1.billing_portal.sessions.create_async(params=params)
        return obj.to_dict()

    async def retrieve_subscription(self, subscription_id: str) -> dict:
        obj = await self._client.v1.subscriptions.retrieve_async(subscription_id)
        return obj.to_dict()


def _stripe() -> StripeBillingClient:
    return StripeBillingClient(_setting("stripe_secret_key"), _setting("stripe_api_base"))


def _provider_error(exc: Exception) -> HTTPException:
    logger.error(f"[BILLING] Stripe request failed: {exc}")
    return HTTPException(status_code=502, detail="Payment provider error")


async def _portal_configuration_id(client: StripeBillingClient) -> str:
    """Cancel at period end. Price changes stay off so Founding keeps renewing at $36."""
    cache_key = _setting("stripe_secret_key") + "|" + _setting("stripe_api_base")
    cached = _portal_config_ids.get(cache_key)
    if cached:
        return cached
    created = await client.create_portal_configuration({
        "features": {
            "customer_update": {"enabled": False},
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
            "subscription_update": {"enabled": False},
        }
    })
    config_id = created.get("id")
    if not config_id:
        raise HTTPException(status_code=502, detail="Payment provider error")
    _portal_config_ids[cache_key] = config_id
    return config_id


async def _load_user(db: Database, user_id: str) -> dict:
    fresh = await db.get_user_by_id(user_id)
    if not fresh:
        raise HTTPException(status_code=404, detail="User not found")
    return fresh


async def _find_user(
    db: Database,
    *,
    client_reference_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    customer_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
) -> Optional[dict]:
    if client_reference_id:
        user = await db.get_user_by_id(client_reference_id)
        if user:
            return user
    watchman_id = (metadata or {}).get("watchman_user_id")
    if watchman_id:
        user = await db.get_user_by_id(watchman_id)
        if user:
            return user
    if customer_id:
        user = await db.get_user_by_stripe_customer(customer_id)
        if user:
            return user
    if subscription_id:
        user = await db.get_user_by_stripe_subscription(subscription_id)
        if user:
            return user
    return None


def _is_admin(user: dict) -> bool:
    return user.get("tier") == "admin" or user.get("role") == "admin"


async def _sync_subscription(db: Database, user: dict, subscription: dict, *, deleted: bool = False) -> str:
    """Write tier, expiry, and the subscription row from one Stripe subscription."""
    if _is_admin(user):
        logger.info(f"[BILLING] Leaving admin {user.get('id')} unchanged")
        return "skipped"
    status = "canceled" if deleted else normalize_stripe_status(str(subscription.get("status") or ""))
    period_end = subscription_period_end(subscription)
    period_start = subscription_period_start(subscription)
    subscription_id = object_id(subscription.get("id"))
    customer_id = object_id(subscription.get("customer"))
    stored = await db.get_stripe_subscription_row(user["id"])
    if is_stale(stored, subscription_id, period_end, status):
        logger.info(f"[BILLING] Ignoring stale {status} event for user {user.get('id')}")
        return "stale"

    plan = resolve_plan(subscription, _price_map()) or (stored or {}).get("plan")
    ids = {}
    if customer_id:
        ids["stripe_customer_id"] = customer_id
    if subscription_id:
        ids["stripe_subscription_id"] = subscription_id

    if status in OPEN_STATUSES and period_end is None:
        logger.warning(f"[BILLING] {subscription_id} has no period end; tier left unchanged")
        if ids:
            await db.update_user_billing(user["id"], ids)
        return "skipped"

    tier, expires = access_for(status, period_end)
    tier, expires = preserve_existing_grant(user, tier, expires, period_end, stored)
    user_updates = dict(ids)
    user_updates["tier"] = tier
    if expires is not None:
        user_updates["pro_expires_at"] = expires.isoformat()
    saved_user = await db.update_user_billing(user["id"], user_updates)
    user.update(saved_user)

    row = {
        "tier": "pro" if tier == "pro" else "free",
        "status": enum_status(status),
        "stripe_status": status,
        "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
    }
    if plan:
        row["plan"] = plan
    if customer_id:
        row["stripe_customer_id"] = customer_id
    if subscription_id:
        row["stripe_subscription_id"] = subscription_id
    if period_end is not None:
        row["current_period_end"] = period_end.isoformat()
    if period_start is not None:
        row["current_period_start"] = period_start.isoformat()
    await db.save_stripe_subscription(user["id"], row)

    if plan == "founding" and counts_as_founding_seat(status):
        outcome = await db.record_founding_seat(user["id"], subscription_id, status)
        if outcome == "full":
            # Checkout already returns 409 at 100. A paid webhook still grants
            # the period; the seat row is what the cap counts.
            logger.error(
                f"[BILLING] Founding cap blocked a seat for paid subscription {subscription_id}"
            )
    logger.info(
        f"[BILLING] User {user['id']} stripe {status} plan={plan} "
        f"tier={tier} until={expires.isoformat() if expires else None} grace={ACCESS_GRACE}"
    )
    return "applied"


async def _apply_checkout_session(db: Database, session: dict, client: StripeBillingClient) -> None:
    if session.get("mode") not in (None, "subscription"):
        return
    metadata = session.get("metadata") or {}
    customer_id = object_id(session.get("customer"))
    user = await _find_user(
        db,
        client_reference_id=session.get("client_reference_id"),
        metadata=metadata,
        customer_id=customer_id,
    )
    if not user:
        logger.warning("[BILLING] checkout.session.completed did not match a user")
        return
    if customer_id and not _is_admin(user):
        saved = await db.update_user_billing(user["id"], {"stripe_customer_id": customer_id})
        user.update(saved)

    subscription = session.get("subscription")
    payload = None
    if isinstance(subscription, dict):
        payload = dict(subscription)
    elif isinstance(subscription, str) and subscription:
        payload = await client.retrieve_subscription(subscription)
    if not payload:
        return
    merged = dict(payload.get("metadata") or {})
    for key in ("plan", "watchman_user_id"):
        if not merged.get(key) and metadata.get(key):
            merged[key] = metadata[key]
    payload = {**payload, "metadata": merged}
    if not payload.get("customer") and customer_id:
        payload["customer"] = customer_id
    await _sync_subscription(db, user, payload)


async def _apply_invoice(db: Database, invoice: dict, client: StripeBillingClient) -> None:
    payload = subscription_from_invoice(invoice)
    if subscription_period_end(payload) is None:
        sub_id = invoice_subscription_id(invoice)
        if not sub_id:
            logger.info("[BILLING] invoice.paid has no period; nothing to grant")
            return
        payload = await client.retrieve_subscription(sub_id)
    metadata = payload.get("metadata") or {}
    user = await _find_user(
        db,
        metadata=metadata,
        customer_id=object_id(payload.get("customer")) or object_id(invoice.get("customer")),
        subscription_id=object_id(payload.get("id")),
    )
    if not user:
        logger.warning("[BILLING] invoice.paid did not match a user")
        return
    await _sync_subscription(db, user, payload)


async def _apply_subscription_notification(db: Database, subscription: dict, *, deleted: bool) -> None:
    metadata = subscription.get("metadata") or {}
    user = await _find_user(
        db,
        metadata=metadata,
        customer_id=object_id(subscription.get("customer")),
        subscription_id=object_id(subscription.get("id")),
    )
    if not user:
        logger.warning("[BILLING] subscription notification did not match a user")
        return
    await _sync_subscription(db, user, subscription, deleted=deleted)


async def apply_stripe_event(db: Database, event: dict, client: StripeBillingClient) -> None:
    event_type = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    if event_type == "checkout.session.completed":
        await _apply_checkout_session(db, obj, client)
    elif event_type == "invoice.paid":
        await _apply_invoice(db, obj, client)
    elif event_type == "customer.subscription.updated":
        await _apply_subscription_notification(db, obj, deleted=False)
    elif event_type == "customer.subscription.deleted":
        await _apply_subscription_notification(db, obj, deleted=True)


def _status_body(user: dict, row: Optional[dict], taken: int) -> dict:
    period = parse_timestamp((row or {}).get("current_period_end"))
    status = None
    plan = None
    cancel = False
    if row:
        plan = row.get("plan")
        status = row.get("stripe_status") or row.get("status")
        cancel = bool(row.get("cancel_at_period_end"))
    return {
        "plan": plan,
        "status": status,
        "current_period_end": period.isoformat() if period else None,
        "cancel_at_period_end": cancel,
        "founding_seats_left": seats_left(taken),
        "tier": get_effective_tier(user),
    }


@router.post("/checkout")
async def create_checkout(request: Request, user: dict = Depends(get_current_user)):
    """Start a Stripe Checkout subscription. The client redirects to url."""
    db = _db()
    await _require_ready(db)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")
    plan = payload.get("plan")
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail="Unknown plan")
    price = _price_for(plan)
    if not price:
        raise _not_configured()

    fresh = await _load_user(db, user["id"])
    if _is_admin(fresh):
        raise HTTPException(status_code=400, detail="Admin access is not billed")
    if not fresh.get("email"):
        raise HTTPException(status_code=400, detail="User email required for payment")

    if plan == "founding" and not await db.user_has_founding_seat(fresh["id"]):
        taken = await db.count_founding_seats()
        if taken >= FOUNDING_LIMIT:
            raise HTTPException(status_code=409, detail=FOUNDING_FULL)

    client = _stripe()
    try:
        customer_id = fresh.get("stripe_customer_id") or None
        if not customer_id:
            customer_params = {
                "email": fresh["email"],
                "metadata": {"watchman_user_id": str(fresh["id"])},
            }
            if fresh.get("name"):
                customer_params["name"] = fresh["name"]
            customer = await client.create_customer(customer_params)
            customer_id = customer.get("id")
            if not customer_id:
                raise HTTPException(status_code=502, detail="Payment provider error")
            fresh = await db.update_user_billing(fresh["id"], {"stripe_customer_id": customer_id})

        session = await client.create_checkout_session({
            "mode": "subscription",
            "customer": customer_id,
            "client_reference_id": str(fresh["id"]),
            "line_items": [{"price": price, "quantity": 1}],
            "success_url": f"{_client_base()}/settings?billing=success",
            "cancel_url": f"{_client_base()}/settings?billing=cancel",
            "allow_promotion_codes": False,
            "allowed_payment_method_types": ["card"],
            "metadata": {"watchman_user_id": str(fresh["id"]), "plan": plan},
            "subscription_data": {
                "metadata": {"watchman_user_id": str(fresh["id"]), "plan": plan},
            },
        })
    except HTTPException:
        raise
    except StripeError as exc:
        raise _provider_error(exc) from exc
    except BillingStorageUnavailable as exc:
        raise _not_configured() from exc

    url = session.get("url")
    if not url:
        raise HTTPException(status_code=502, detail="Payment provider error")
    logger.info(f"[BILLING] Checkout {session.get('id')} plan={plan} user={fresh['id']}")
    return {"url": url, "session_id": session.get("id")}


@router.post("/portal")
async def customer_portal(user: dict = Depends(get_current_user)):
    """Stripe Customer Portal. Cancellation is at period end, with no price changes."""
    db = _db()
    await _require_ready(db)
    fresh = await _load_user(db, user["id"])
    customer_id = fresh.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(status_code=400, detail="No billing account yet")
    client = _stripe()
    try:
        config_id = await _portal_configuration_id(client)
        session = await client.create_portal_session({
            "customer": customer_id,
            "return_url": f"{_client_base()}/settings",
            "configuration": config_id,
        })
    except HTTPException:
        raise
    except StripeError as exc:
        raise _provider_error(exc) from exc
    url = session.get("url")
    if not url:
        raise HTTPException(status_code=502, detail="Payment provider error")
    return {"url": url}


@router.get("/status")
async def billing_status(user: dict = Depends(get_current_user)):
    """Plan, Stripe status, period end, cancel flag, and founding seats left."""
    db = _db()
    await _require_ready(db)
    fresh = await _load_user(db, user["id"])
    row = await db.get_stripe_subscription_row(fresh["id"])
    taken = await db.count_founding_seats()
    return _status_body(fresh, row, taken)


@router.get("/founding")
async def founding_seats():
    """Public count of founding seats still open. No authentication."""
    db = _db()
    await _require_ready(db)
    taken = await db.count_founding_seats()
    return {"founding_seats_left": seats_left(taken), "limit": FOUNDING_LIMIT}


@router.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    """Verify Stripe's signature, then apply subscription state once per event id."""
    if not _keys_configured():
        raise _not_configured()
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    try:
        stripe.Webhook.construct_event(payload, signature, _setting("stripe_webhook_secret"))
    except SignatureVerificationError as exc:
        logger.warning("[BILLING] Rejected Stripe webhook signature")
        raise HTTPException(status_code=400, detail="Invalid signature") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid payload") from exc
    try:
        event = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid payload") from exc
    if not isinstance(event, dict) or not event.get("id") or not event.get("type"):
        raise HTTPException(status_code=400, detail="Invalid payload")

    event_type = event["type"]
    if event_type not in HANDLED_EVENTS:
        return {"received": True}

    db = _db()
    try:
        await _require_ready(db)
        if await db.get_stripe_event(event["id"]):
            return {"received": True, "duplicate": True}
        await apply_stripe_event(db, event, _stripe())
        await db.insert_stripe_event(event["id"], event_type)
    except HTTPException:
        raise
    except BillingStorageUnavailable as exc:
        logger.error(f"[BILLING] Storage missing while applying {event_type}: {exc}")
        raise _not_configured() from exc
    except StripeError as exc:
        raise _provider_error(exc) from exc
    except Exception as exc:
        logger.exception(f"[BILLING] Failed to apply {event_type} {event.get('id')}")
        raise HTTPException(status_code=500, detail="Could not record billing event") from exc
    return {"received": True}
