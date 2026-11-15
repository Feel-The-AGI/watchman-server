"""
Watchman Payments Routes
Pro is a Stripe subscription, billed monthly or yearly in USD.

Access comes from Stripe, never from the browser: a paid invoice (webhook)
or a completed checkout the server looks up itself sets pro_expires_at to
the end of the paid period. Cancelling stops the next renewal and keeps the
days already paid for.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from app.billing import access_expired, access_until, parse_timestamp
from app.config import get_settings
from app.database import Database
from app.middleware.auth import get_current_user, get_effective_tier
from app.services.email_service import ADMIN_EMAIL, get_email_service
from app.stripe_client import (
    StripeError,
    customer_id,
    invoice_period_end,
    invoice_subscription_id,
    stripe_request,
    subscription_interval,
    subscription_period_end,
    verify_signature,
)

router = APIRouter()
settings = get_settings()

PRODUCT_NAME = "Watchman Pro"
LOOKUP_KEYS = {"monthly": "watchman_pro_monthly", "annual": "watchman_pro_annual"}
INTERVALS = {"monthly": "month", "annual": "year"}
# Stripe subscription states that mean the customer is paying.
LIVE_STATES = {"active", "trialing", "past_due"}
NOT_CONFIGURED = "Payments are not set up yet. Nothing was charged."

# Price ids found or created this process, keyed by plan and amount in cents.
_price_cache: dict = {}


class CheckoutRequest(BaseModel):
    plan: Literal["monthly", "annual"] = "monthly"


def plan_amount(plan: str) -> float:
    return settings.pro_annual_price_usd if plan == "annual" else settings.pro_price_usd


def _cents(amount: float) -> int:
    return int(round(float(amount) * 100))


def _require_stripe() -> str:
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED)
    return settings.stripe_secret_key


async def _stripe(method: str, path: str, data: Optional[dict] = None, **kwargs) -> dict:
    try:
        return await stripe_request(_require_stripe(), method, path, data, **kwargs)
    except StripeError as exc:
        logger.error(f"[PAYMENTS] Stripe {method} {path} failed ({exc.status}): {exc.message}")
        raise HTTPException(
            status_code=502 if exc.status != 503 else 503,
            detail="The payment provider did not respond. Nothing was charged. Try again in a moment.",
        ) from exc


async def price_id_for(plan: str) -> str:
    """The Stripe price for a plan. Found by lookup key, created when missing.

    A changed amount in settings makes a new price and moves the lookup key
    to it, so existing subscribers keep their price and new ones get the new
    amount.
    """
    cents = _cents(plan_amount(plan))
    cached = _price_cache.get((plan, cents))
    if cached:
        return cached
    lookup = LOOKUP_KEYS[plan]
    found = await _stripe("GET", "/prices", {"lookup_keys": [lookup], "active": "true", "limit": 1})
    for price in found.get("data") or []:
        recurring = price.get("recurring") or {}
        if (
            price.get("unit_amount") == cents
            and str(price.get("currency")).lower() == "usd"
            and recurring.get("interval") == INTERVALS[plan]
        ):
            _price_cache[(plan, cents)] = price["id"]
            return price["id"]
    product_id = (found.get("data") or [{}])[0].get("product") if found.get("data") else None
    if isinstance(product_id, dict):
        product_id = product_id.get("id")
    if not product_id:
        product = await _stripe("POST", "/products", {
            "name": PRODUCT_NAME,
            "description": "Unlimited planning, commitments, chat, exports and sharing.",
        }, idempotency_key="watchman-pro-product")
        product_id = product["id"]
    price = await _stripe("POST", "/prices", {
        "product": product_id,
        "currency": "usd",
        "unit_amount": cents,
        "recurring": {"interval": INTERVALS[plan]},
        "lookup_key": lookup,
        "transfer_lookup_key": True,
        "nickname": f"Watchman Pro {plan}",
    }, idempotency_key=f"watchman-price-{plan}-{cents}")
    _price_cache[(plan, cents)] = price["id"]
    return price["id"]


async def _customer_for(db: Database, user: dict) -> str:
    """The user's Stripe customer, created on first checkout."""
    existing = user.get("stripe_customer_id")
    if existing:
        return existing
    customer = await _stripe("POST", "/customers", {
        "email": user.get("email"),
        "name": user.get("name") or None,
        "metadata": {"watchman_user_id": user["id"]},
    }, idempotency_key=f"watchman-customer-{user['id']}")
    saved = await db.update_user(user["id"], {"stripe_customer_id": customer["id"]})
    if not saved:
        raise HTTPException(status_code=500, detail="Could not start checkout. Nothing was charged.")
    user["stripe_customer_id"] = customer["id"]
    return customer["id"]


async def _save_subscription_row(
    db: Database,
    user_id: str,
    *,
    status: str,
    period_end: Optional[datetime],
    cancel_at_period_end: bool,
    customer: Optional[str],
    subscription_id: Optional[str],
) -> None:
    fields = {
        "tier": "pro",
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "stripe_customer_id": customer,
        "stripe_subscription_id": subscription_id,
    }
    if period_end:
        fields["current_period_end"] = period_end.isoformat()
    row = await db.get_subscription(user_id)
    if row and row.get("id"):
        saved = await db.update_subscription(row["id"], fields)
    else:
        saved = await db.create_subscription({
            "user_id": user_id,
            "current_period_start": datetime.now(timezone.utc).isoformat(),
            **fields,
        })
    if not saved:
        logger.error(f"[PAYMENTS] Could not record subscription row for {user_id}")


def _row_status(stripe_status: str) -> str:
    if stripe_status in ("active", "trialing"):
        return "active"
    if stripe_status in ("past_due", "unpaid", "incomplete"):
        return "past_due"
    if stripe_status == "canceled":
        return "cancelled"
    return "expired"


async def _notify_new_subscriber(user: dict, amount: float) -> None:
    email_service = get_email_service()
    recipient = user.get("email")
    name = user.get("name") or (recipient.split("@")[0] if recipient else "there")
    if recipient:
        try:
            await email_service.send_pro_upgrade_email(to=recipient, user_name=name)
        except Exception as exc:
            logger.warning(f"[PAYMENTS] Pro upgrade email failed: {exc}")
    try:
        await email_service.send_admin_new_subscriber_notification(
            admin_email=ADMIN_EMAIL,
            subscriber_email=recipient,
            subscriber_name=name,
            amount_usd=amount,
        )
    except Exception as exc:
        logger.warning(f"[PAYMENTS] Admin subscriber email failed: {exc}")


async def apply_subscription(db: Database, user: dict, subscription: dict, *, notify: bool = True) -> dict:
    """Bring the user's access in line with a Stripe subscription. Safe to repeat.

    Access only ever moves forward here. A cancelled or unpaid subscription
    does not take away days already paid for; they run out on their own.
    """
    status = str(subscription.get("status") or "")
    period_end = subscription_period_end(subscription)
    sub_id = subscription.get("id")
    customer = customer_id(subscription) or user.get("stripe_customer_id")
    cancelling = bool(subscription.get("cancel_at_period_end")) or bool(subscription.get("cancel_at"))
    now = datetime.now(timezone.utc)

    updates: dict = {}
    if customer and customer != user.get("stripe_customer_id"):
        updates["stripe_customer_id"] = customer
    live = status in LIVE_STATES
    if live and sub_id != user.get("stripe_subscription_id"):
        updates["stripe_subscription_id"] = sub_id
    if status == "canceled" and user.get("stripe_subscription_id") == sub_id:
        updates["stripe_subscription_id"] = None

    granted = None
    was_active = user.get("tier") == "pro" and not access_expired(user, now)
    if status in ("active", "trialing") and period_end:
        until = access_until(period_end)
        current = parse_timestamp(user.get("pro_expires_at"))
        if user.get("tier") != "admin" and (user.get("tier") != "pro" or current is None or current < until):
            updates["tier"] = "pro"
            updates["pro_expires_at"] = until.isoformat()
            granted = until
    if updates:
        saved = await db.update_user(user["id"], updates)
        if not saved:
            raise HTTPException(status_code=500, detail="Could not record the subscription")
        user.update(saved if isinstance(saved, dict) else updates)

    await _save_subscription_row(
        db,
        user["id"],
        status=_row_status(status),
        period_end=period_end,
        cancel_at_period_end=cancelling,
        customer=customer,
        subscription_id=sub_id if status != "canceled" else None,
    )
    if granted and notify and not was_active:
        interval = subscription_interval(subscription)
        await _notify_new_subscriber(user, plan_amount("annual" if interval == "year" else "monthly"))
    return {
        "status": status,
        "access_until": user.get("pro_expires_at"),
        "cancel_at_period_end": cancelling,
    }


async def _user_for_customer(db: Database, customer: Optional[str], fallback_user_id: Optional[str] = None) -> Optional[dict]:
    user = await db.get_user_by_stripe_customer(customer) if customer else None
    if not user and fallback_user_id:
        user = await db.get_user_by_id(fallback_user_id)
    return user


async def _record_invoice(db: Database, user: dict, invoice: dict) -> None:
    """One payments row per paid invoice."""
    invoice_id = invoice.get("id")
    if not invoice_id or await db.get_payment_by_invoice(invoice_id):
        return
    end = invoice_period_end(invoice)
    await db.create_payment_record({
        "user_id": user["id"],
        "stripe_invoice_id": invoice_id,
        "amount": round((invoice.get("amount_paid") or 0) / 100, 2),
        "currency": str(invoice.get("currency") or "usd").lower(),
        "status": "paid",
        "description": "Watchman Pro",
        "access_until": access_until(end).isoformat() if end else None,
    })


@router.get("/pricing")
async def get_pricing():
    """Public prices for the pricing page."""
    monthly = settings.pro_price_usd
    annual = settings.pro_annual_price_usd
    saving = round(100 - (annual / (monthly * 12)) * 100) if monthly else 0
    return {
        "currency": "usd",
        "configured": bool(settings.stripe_secret_key),
        "monthly": {"amount": monthly, "display": f"${monthly:g}", "interval": "month"},
        "annual": {
            "amount": annual,
            "display": f"${annual:g}",
            "interval": "year",
            "per_month": round(annual / 12, 2),
            "saving_percent": max(saving, 0),
        },
    }


@router.post("/create-checkout-session")
async def create_checkout_session(
    request: Optional[CheckoutRequest] = None,
    user: dict = Depends(get_current_user),
):
    """Open Stripe Checkout for a Pro subscription."""
    _require_stripe()
    plan = (request or CheckoutRequest()).plan
    if user.get("tier") == "admin":
        raise HTTPException(status_code=400, detail="Admin accounts already have every feature.")
    if not user.get("email"):
        raise HTTPException(status_code=400, detail="Your account needs an email address before checkout.")
    if user.get("stripe_subscription_id") and user.get("tier") == "pro" and not access_expired(user):
        raise HTTPException(
            status_code=409,
            detail="You already have Pro. Use Manage billing to change plan or payment method.",
        )
    db = Database(use_admin=True)
    customer = await _customer_for(db, user)
    price = await price_id_for(plan)
    base = settings.app_url.rstrip("/")
    session = await _stripe("POST", "/checkout/sessions", {
        "mode": "subscription",
        "customer": customer,
        "client_reference_id": user["id"],
        "line_items": [{"price": price, "quantity": 1}],
        "allow_promotion_codes": True,
        "billing_address_collection": "auto",
        "subscription_data": {"metadata": {"watchman_user_id": user["id"], "plan": plan}},
        "metadata": {"watchman_user_id": user["id"], "plan": plan},
        "success_url": f"{base}/dashboard/settings?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/pricing?checkout=cancelled",
    })
    if not session.get("url"):
        raise HTTPException(status_code=502, detail="Checkout did not open. Nothing was charged.")
    logger.info(f"[PAYMENTS] Checkout {session.get('id')} opened for {user['id']} ({plan})")
    return {"checkout_url": session["url"], "session_id": session.get("id"), "plan": plan}


@router.get("/verify-session/{session_id}")
async def verify_checkout_session(session_id: str, user: dict = Depends(get_current_user)):
    """Confirm a returning checkout without waiting for the webhook."""
    if not session_id.startswith("cs_") or len(session_id) > 255:
        raise HTTPException(status_code=404, detail="Checkout not found")
    session = await _stripe("GET", f"/checkout/sessions/{session_id}", {"expand": ["subscription"]})
    owner = session.get("client_reference_id") or (session.get("metadata") or {}).get("watchman_user_id")
    if owner != user["id"]:
        raise HTTPException(status_code=404, detail="Checkout not found")
    if session.get("status") != "complete":
        return {"status": "pending", "message": "Checkout is not finished yet."}
    subscription = session.get("subscription")
    if isinstance(subscription, str):
        subscription = await _stripe("GET", f"/subscriptions/{subscription}")
    if not isinstance(subscription, dict):
        return {"status": "pending", "message": "The payment is still being confirmed."}
    db = Database(use_admin=True)
    result = await apply_subscription(db, user, subscription)
    return {"status": "active" if result["status"] in ("active", "trialing") else result["status"], **result}


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Stripe events. The signature is checked against the raw body first."""
    payload = await request.body()
    if not settings.stripe_webhook_secret:
        logger.error("[PAYMENTS] Webhook received but STRIPE_WEBHOOK_SECRET is not set")
        raise HTTPException(status_code=503, detail="Payments are not configured")
    if not verify_signature(payload, request.headers.get("stripe-signature"), settings.stripe_webhook_secret):
        logger.warning("[PAYMENTS] Invalid Stripe webhook signature")
        raise HTTPException(status_code=400, detail="Invalid signature")
    try:
        event = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid payload")
    kind = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    logger.info(f"[PAYMENTS] Stripe event {kind} {event.get('id')}")
    db = Database(use_admin=True)

    if kind == "checkout.session.completed":
        if obj.get("mode") != "subscription" or not obj.get("subscription"):
            return {"status": "ignored"}
        user = await _user_for_customer(db, customer_id(obj), obj.get("client_reference_id"))
        if not user:
            logger.error(f"[PAYMENTS] Checkout {obj.get('id')} has no matching user")
            return {"status": "ignored"}
        sub_id = obj["subscription"] if isinstance(obj["subscription"], str) else obj["subscription"].get("id")
        subscription = await _stripe("GET", f"/subscriptions/{sub_id}")
        await apply_subscription(db, user, subscription)
        return {"status": "success"}

    if kind in ("invoice.paid", "invoice.payment_succeeded"):
        sub_id = invoice_subscription_id(obj)
        if not sub_id:
            return {"status": "ignored"}
        user = await _user_for_customer(db, customer_id(obj))
        if not user:
            logger.error(f"[PAYMENTS] Invoice {obj.get('id')} has no matching user")
            return {"status": "ignored"}
        subscription = await _stripe("GET", f"/subscriptions/{sub_id}")
        await apply_subscription(db, user, subscription)
        await _record_invoice(db, user, obj)
        return {"status": "success"}

    if kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        user = await _user_for_customer(db, customer_id(obj), (obj.get("metadata") or {}).get("watchman_user_id"))
        if not user:
            return {"status": "ignored"}
        await apply_subscription(db, user, obj)
        return {"status": "success"}

    if kind == "invoice.payment_failed":
        user = await _user_for_customer(db, customer_id(obj))
        if user:
            row = await db.get_subscription(user["id"])
            if row and row.get("id"):
                await db.update_subscription(row["id"], {"status": "past_due"})
            logger.warning(f"[PAYMENTS] Renewal failed for {user['id']}; Stripe will retry")
        return {"status": "success"}

    return {"status": "ignored"}


def _period_is_active(user: dict) -> bool:
    if user.get("tier") == "admin":
        return True
    return user.get("tier") == "pro" and not access_expired(user)


@router.get("/subscription-status")
async def get_subscription_status(user: dict = Depends(get_current_user)):
    expires = parse_timestamp(user.get("pro_expires_at"))
    active = _period_is_active(user)
    db = Database(use_admin=True)
    row = await db.get_subscription(user["id"])
    return {
        "active": active,
        "tier": get_effective_tier(user),
        "current_period_end": expires.isoformat() if expires else None,
        "cancel_at_period_end": bool(row and row.get("cancel_at_period_end")),
        "renews": bool(user.get("stripe_subscription_id")) and not bool(row and row.get("cancel_at_period_end")),
        "has_billing": bool(user.get("stripe_customer_id")),
        "message": "Pro access is active" if active else "No active Pro period",
    }


async def _set_cancel(user: dict, cancel: bool) -> dict:
    sub_id = user.get("stripe_subscription_id")
    if not sub_id:
        raise HTTPException(status_code=400, detail="There is no Pro subscription to change.")
    subscription = await _stripe("POST", f"/subscriptions/{sub_id}", {"cancel_at_period_end": cancel})
    db = Database(use_admin=True)
    result = await apply_subscription(db, user, subscription, notify=False)
    return result


@router.post("/cancel-subscription")
async def cancel_subscription(user: dict = Depends(get_current_user)):
    """Stop renewal at the end of the paid period."""
    if user.get("tier") == "admin":
        raise HTTPException(status_code=400, detail="Admin access is not a subscription.")
    result = await _set_cancel(user, True)
    until = result.get("access_until")
    return {
        "status": "cancelled",
        "tier": "pro",
        "access_until": until,
        "message": "Renewal is cancelled. Pro stays until the end of the period you paid for.",
    }


@router.post("/resume-subscription")
async def resume_subscription(user: dict = Depends(get_current_user)):
    """Undo a cancellation before the period ends."""
    result = await _set_cancel(user, False)
    return {
        "status": "active",
        "access_until": result.get("access_until"),
        "message": "Pro will renew as normal.",
    }


@router.post("/billing-portal")
async def billing_portal(user: dict = Depends(get_current_user)):
    """A Stripe page to change card, switch plan, see invoices or cancel."""
    customer = user.get("stripe_customer_id")
    if not customer:
        raise HTTPException(status_code=400, detail="There is no billing account yet. Upgrade to Pro first.")
    session = await _stripe("POST", "/billing_portal/sessions", {
        "customer": customer,
        "return_url": f"{settings.app_url.rstrip('/')}/dashboard/settings?tab=subscription",
    })
    if not session.get("url"):
        raise HTTPException(status_code=502, detail="The billing page did not open. Try again in a moment.")
    return {"url": session["url"]}


@router.get("/payment-history")
async def get_payment_history(user: dict = Depends(get_current_user)):
    db = Database(use_admin=True)
    payments = await db.get_payment_history(user["id"])
    return {"payments": [row for row in payments if row.get("status") == "paid"]}


@router.get("/manage-subscription")
async def get_manage_subscription_link(user: dict = Depends(get_current_user)):
    expires = parse_timestamp(user.get("pro_expires_at"))
    if not _period_is_active(user):
        return {"has_subscription": False, "message": "No active Pro period. Upgrade to Pro to manage billing."}
    return {
        "has_subscription": True,
        "tier": get_effective_tier(user),
        "current_period_end": expires.isoformat() if expires else None,
        "has_billing": bool(user.get("stripe_customer_id")),
        "message": "Manage billing opens Stripe, where you can change card or plan, see invoices, or cancel.",
    }

