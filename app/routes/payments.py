"""
Watchman Payments Routes
Paystack charges for Pro access at a fixed GHS plan price.

A verified charge grants that plan's days. It does not create a Paystack
subscription and it does not grant access without a server-created checkout.
USD amounts are display only and are never sent to Paystack.
"""

import hashlib
import hmac
import time
import httpx
from datetime import datetime, timezone
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from app.billing import (
    PLANS,
    PlanNotOffered,
    UnknownPlan,
    access_expired,
    charge_matches,
    checkout_description,
    checkout_plan,
    extend_expiry,
    founding_available,
    offered_plans,
    parse_timestamp,
    period_days_for_payment,
    to_pesewas,
)
from app.config import get_settings
from app.database import Database
from app.middleware.auth import get_current_user, get_effective_tier
from app.services.email_service import get_email_service, ADMIN_EMAIL


router = APIRouter()
settings = get_settings()

# Paystack API configuration
PAYSTACK_BASE_URL = "https://api.paystack.co"

class ExchangeRateUnavailable(Exception):
    """The live USD→GHS rate could not be fetched. Do not charge a guessed rate."""


# Cache exchange rate for 1 hour to avoid too many API calls
_exchange_rate_cache = {"rate": None, "timestamp": 0}
EXCHANGE_RATE_CACHE_SECONDS = 3600  # 1 hour


async def get_usd_to_ghs_rate() -> float:
    """
    Fetch current USD to GHS exchange rate.
    Uses exchangerate-api.com. A missing key or a failed response raises
    ExchangeRateUnavailable so checkout cannot charge a hardcoded rate.
    """
    current_time = time.time()

    if (_exchange_rate_cache["rate"] is not None and
        current_time - _exchange_rate_cache["timestamp"] < EXCHANGE_RATE_CACHE_SECONDS):
        logger.debug(f"[EXCHANGE] Using cached rate: {_exchange_rate_cache['rate']}")
        return _exchange_rate_cache["rate"]

    api_key = settings.exchange_rate_api_key
    if not api_key:
        logger.error("[EXCHANGE] No API key configured")
        raise ExchangeRateUnavailable("Exchange rate API key is not configured")

    try:
        async with httpx.AsyncClient() as client:
            url = f"https://v6.exchangerate-api.com/v6/{api_key}/pair/USD/GHS"
            response = await client.get(url, timeout=10.0)

        if response.status_code == 200:
            data = response.json()
            rate = data.get("conversion_rate") if data.get("result") == "success" else None
            if isinstance(rate, (int, float)) and rate > 0:
                _exchange_rate_cache["rate"] = float(rate)
                _exchange_rate_cache["timestamp"] = current_time
                logger.info(f"[EXCHANGE] Fetched fresh USD→GHS rate: {rate}")
                return float(rate)
        logger.error(f"[EXCHANGE] API error status={response.status_code}")
    except ExchangeRateUnavailable:
        raise
    except Exception as e:
        logger.error(f"[EXCHANGE] Error fetching rate: {e}")
    raise ExchangeRateUnavailable("Exchange rate is unavailable")


async def paystack_request(method: str, endpoint: str, data: dict = None) -> dict:
    """Make a request to Paystack API"""
    headers = {
        "Authorization": f"Bearer {settings.paystack_secret_key}",
        "Content-Type": "application/json",
    }
    
    async with httpx.AsyncClient() as client:
        url = f"{PAYSTACK_BASE_URL}{endpoint}"
        
        if method == "GET":
            response = await client.get(url, headers=headers, timeout=30.0)
        elif method == "POST":
            response = await client.post(url, headers=headers, json=data, timeout=30.0)
        else:
            raise ValueError(f"Unsupported method: {method}")
        
        result = response.json()
        
        if not result.get("status"):
            logger.error(f"[PAYSTACK] API error: {result.get('message')}")
            raise HTTPException(status_code=400, detail=result.get("message", "Paystack error"))
        
        return result


class CheckoutRequest(BaseModel):
    """A plan id selects the price. Amount fields are never charged."""

    plan: Optional[str] = None
    plan_id: Optional[str] = None
    amount: Optional[Any] = None
    amount_ghs: Optional[Any] = None
    amount_usd: Optional[Any] = None

    def requested_plan(self) -> Optional[str]:
        chosen = self.plan if self.plan not in (None, "") else self.plan_id
        if chosen in (None, ""):
            return None
        return str(chosen)


def _pricing_body() -> dict:
    """Offered plans, plus the monthly fields the current pricing page reads."""
    monthly = PLANS["monthly"]
    return {
        "currency": "GHS",
        "founding_available": founding_available(),
        "plans": [plan.as_public() for plan in offered_plans()],
        "usd_price": float(monthly.display_usd),
        "ghs_amount": float(monthly.amount_ghs),
        "display_price": f"${int(monthly.display_usd)}",
        "display_ghs": f"GHS {monthly.amount_ghs:.2f}",
        "period_days": monthly.days,
        "note": "Prices are fixed in GHS. USD amounts are for display only and are not charged.",
    }


@router.post("/create-checkout-session")
async def create_checkout_session(
    user: dict = Depends(get_current_user),
    body: Optional[CheckoutRequest] = None,
):
    """
    Initialize a Paystack transaction for one prepaid Pro period.

    The GHS amount comes from the plan table. A missing plan id is monthly,
    which is what the current client sends by omitting a body. The returned
    reference is stored before the checkout URL is given to the client.
    """
    if not settings.paystack_secret_key:
        raise HTTPException(status_code=503, detail="Payment service not configured")

    user_email = user.get("email")
    user_id = user.get("id")

    if not user_email:
        raise HTTPException(status_code=400, detail="User email required for payment")

    try:
        try:
            plan = checkout_plan(body.requested_plan() if body else None)
        except UnknownPlan as exc:
            raise HTTPException(status_code=400, detail=f"Unknown plan: {exc.plan_id}")
        except PlanNotOffered as exc:
            raise HTTPException(status_code=400, detail=f"Plan is not available: {exc.plan_id}")

        if body and body.amount not in (None, "") and str(body.amount) not in {
            str(plan.display_usd), str(float(plan.display_usd)), str(int(plan.display_usd)),
        }:
            logger.info(
                f"[PAYMENTS] Ignoring client amount for user {user_id}; charging plan {plan.id}"
            )

        ghs_amount = float(plan.amount_ghs)
        display_usd = float(plan.display_usd)
        amount_in_pesewas = to_pesewas(plan.amount_ghs)
        logger.info(
            f"[PAYMENTS] Checkout plan={plan.id} GHS {ghs_amount} for {plan.days} days "
            f"(display ${display_usd})"
        )

        # Do not send `plan` or `channels` at the top level. `plan` would start
        # a Paystack subscription. This Ghana Starter Business account also
        # rejects card / apple_pay with "No active channel to process transaction".
        # Omitting channels lets Paystack offer every channel enabled on the
        # account, including mobile money and bank transfer.
        transaction_data = {
            "email": user_email,
            "amount": amount_in_pesewas,
            "currency": "GHS",
            "callback_url": "https://trywatchman.app/dashboard/settings?success=true",
            "metadata": {
                "watchman_user_id": user_id,
                "plan": plan.id,
                "display_usd": display_usd,
                "ghs_amount": ghs_amount,
                "period_days": plan.days,
                "custom_fields": [
                    {
                        "display_name": "Plan",
                        "variable_name": "plan",
                        "value": f"Watchman Pro {plan.id} ({plan.days} days)"
                    },
                    {
                        "display_name": "USD display",
                        "variable_name": "usd_display",
                        "value": f"${display_usd:g} display only"
                    }
                ]
            },
        }

        result = await paystack_request("POST", "/transaction/initialize", transaction_data)

        data = result.get("data", {})
        authorization_url = data.get("authorization_url")
        access_code = data.get("access_code")
        reference = data.get("reference")
        if not authorization_url or not reference:
            raise HTTPException(status_code=502, detail="Payment provider did not return a checkout")

        db = Database(use_admin=True)
        recorded = await db.create_payment_record({
            "user_id": user_id,
            "paystack_reference": reference,
            "plan": plan.id,
            "amount": display_usd,
            "amount_local": ghs_amount,
            "currency": "usd",
            "currency_local": "ghs",
            "status": "pending",
            "description": checkout_description(plan),
        })
        if not recorded:
            logger.error(f"[PAYMENTS] Checkout {reference} was not stored; withholding the payment URL")
            raise HTTPException(status_code=500, detail="Could not start checkout")

        logger.info(
            f"[PAYMENTS] Paystack transaction initialized for user {user_id}, "
            f"ref: {reference}, plan {plan.id}, GHS {ghs_amount}"
        )

        return {
            "checkout_url": authorization_url,
            "access_code": access_code,
            "reference": reference,
            "plan": plan.id,
            "amount_ghs": ghs_amount,
            "amount_usd": display_usd,
            "display_usd": display_usd,
            "period_days": plan.days,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PAYMENTS] Error initializing Paystack transaction: {e}")
        raise HTTPException(status_code=500, detail="Failed to create checkout session")


@router.get("/pricing")
async def get_pricing():
    """
    Public plan list for the pricing page.
    Amounts are fixed GHS. founding_yearly is listed only when it is offered.
    """
    return _pricing_body()


async def _save_subscription_period(db: Database, user_id: str, until: datetime, *, cancelled: bool = False) -> None:
    fields = {
        "tier": "pro",
        "status": "active",
        "current_period_end": until.isoformat(),
        "cancel_at_period_end": cancelled,
    }
    subscription = await db.get_subscription(user_id)
    if subscription and subscription.get("id"):
        saved = await db.update_subscription(subscription["id"], fields)
    else:
        saved = await db.create_subscription({
            "user_id": user_id,
            "current_period_start": datetime.now(timezone.utc).isoformat(),
            **fields,
        })
    if not saved:
        raise HTTPException(status_code=500, detail="Could not record subscription period")


async def _grant_access(
    db: Database,
    user: dict,
    until: datetime,
    *,
    customer_code: str = None,
    notify: bool = False,
    email: str = None,
    usd_amount: float = None,
) -> None:
    updates = {"tier": "pro", "pro_expires_at": until.isoformat()}
    if customer_code:
        updates["paystack_customer_code"] = customer_code
    updated = await db.update_user(user["id"], updates)
    if not updated:
        raise HTTPException(status_code=500, detail="Could not grant Pro access")
    user.update(updated)
    await _save_subscription_period(db, user["id"], until)
    if not notify:
        return
    email_service = get_email_service()
    recipient = email or user.get("email")
    user_name = user.get("name") or (recipient.split("@")[0] if recipient else "there")
    if recipient:
        try:
            await email_service.send_pro_upgrade_email(to=recipient, user_name=user_name)
        except Exception as e:
            logger.warning(f"[PAYMENTS] Failed to send Pro upgrade email: {e}")
    try:
        await email_service.send_admin_new_subscriber_notification(
            admin_email=ADMIN_EMAIL,
            subscriber_email=recipient,
            subscriber_name=user_name,
            amount_usd=usd_amount if usd_amount is not None else float(PLANS["monthly"].display_usd),
        )
    except Exception as e:
        logger.warning(f"[PAYMENTS] Failed to send admin notification: {e}")


async def _ensure_access(db: Database, payment: dict) -> None:
    """Make sure a paid checkout's stored period is on the user. Safe to repeat."""
    until = parse_timestamp(payment.get("access_until"))
    if until is None:
        return
    user = await db.get_user_by_id(payment["user_id"])
    if not user:
        return
    current = parse_timestamp(user.get("pro_expires_at"))
    if user.get("tier") == "pro" and current and current >= until:
        return
    await _grant_access(
        db,
        user,
        until,
        customer_code=user.get("paystack_customer_code"),
        notify=False,
    )


async def apply_successful_charge(db: Database, reference: str, verified: dict) -> dict:
    """Grant one Pro period for a server-created checkout, at most once."""
    if verified.get("status") != "success":
        logger.info(f"[PAYMENTS] Charge {reference} is not successful ({verified.get('status')})")
        return {"status": "ignored"}

    payment = await db.get_payment_by_reference(reference)
    if not payment:
        logger.warning(f"[PAYMENTS] Ignoring charge {reference}; it was not created by checkout")
        return {"status": "ignored"}
    if payment.get("status") == "paid":
        await _ensure_access(db, payment)
        return {"status": "success", "access_until": payment.get("access_until")}
    if payment.get("status") != "pending":
        logger.info(f"[PAYMENTS] Charge {reference} is {payment.get('status')}; not granting access")
        return {"status": "ignored"}
    if not charge_matches(payment.get("amount_local"), verified.get("amount"), verified.get("currency")):
        await db.update_payment_by_reference(reference, {"status": "rejected"}, status="pending")
        logger.error(
            f"[PAYMENTS] Rejecting {reference}: charged {verified.get('amount')} {verified.get('currency')} "
            f"against stored GHS {payment.get('amount_local')}"
        )
        return {"status": "ignored"}

    user = await db.get_user_by_id(payment["user_id"])
    if not user:
        logger.error(f"[PAYMENTS] Checkout {reference} has no user {payment.get('user_id')}")
        return {"status": "ignored"}

    now = datetime.now(timezone.utc)
    until = extend_expiry(
        user.get("pro_expires_at"), now, days=period_days_for_payment(payment)
    )
    claimed = await db.update_payment_by_reference(
        reference,
        {"status": "paid", "access_until": until.isoformat()},
        status="pending",
    )
    if not claimed:
        current = await db.get_payment_by_reference(reference)
        if current and current.get("status") == "paid":
            await _ensure_access(db, current)
            return {"status": "success", "access_until": current.get("access_until")}
        if current and current.get("status") == "pending":
            raise HTTPException(status_code=500, detail="Could not record payment")
        return {"status": "ignored"}

    customer = verified.get("customer") or {}
    was_active = user.get("tier") == "pro" and not access_expired(user, now)
    await _grant_access(
        db,
        user,
        until,
        customer_code=customer.get("customer_code"),
        notify=not was_active,
        email=customer.get("email") or user.get("email"),
        usd_amount=payment.get("amount"),
    )
    logger.info(f"[PAYMENTS] User {user['id']} Pro access until {until.isoformat()}")
    return {"status": "success", "access_until": until.isoformat()}


async def _find_paystack_user(db: Database, data: dict) -> dict:
    customer = data.get("customer") or {}
    customer_code = customer.get("customer_code")
    user = None
    if customer_code:
        user = await db.get_user_by_paystack_customer(customer_code)
    if not user and customer.get("email"):
        user = await db.get_user_by_email(customer.get("email"))
    return user


async def _store_paystack_subscription(db: Database, data: dict, *, grant_access: bool) -> None:
    customer = data.get("customer") or {}
    user = await _find_paystack_user(db, data)
    if not user:
        return
    updates = {}
    if customer.get("customer_code"):
        updates["paystack_customer_code"] = customer.get("customer_code")
    if data.get("subscription_code"):
        updates["paystack_subscription_code"] = data.get("subscription_code")
    if updates and not grant_access:
        await db.update_user(user["id"], updates)


async def _mark_subscription_cancelled(db: Database, data: dict) -> None:
    """Stop renewal. Access already paid for lasts until pro_expires_at."""
    user = await _find_paystack_user(db, data)
    if not user:
        return
    await db.update_user(user["id"], {"paystack_subscription_code": None})
    subscription = await db.get_subscription(user["id"])
    if subscription and subscription.get("id"):
        await db.update_subscription(subscription["id"], {
            "cancel_at_period_end": True,
            "status": "active" if not access_expired(user) else "cancelled",
        })
    logger.info(f"[PAYMENTS] Renewal stopped for user {user['id']}; paid access is unchanged")


async def _mark_renewal_failed(db: Database, data: dict) -> None:
    user = await _find_paystack_user(db, data)
    if not user:
        return
    reference = data.get("reference") or (data.get("transaction") or {}).get("reference")
    if reference:
        await db.update_payment_by_reference(reference, {"status": "failed"}, status="pending")
    subscription = await db.get_subscription(user["id"])
    if subscription and subscription.get("id"):
        await db.update_subscription(subscription["id"], {"status": "past_due"})
    logger.warning(f"[PAYMENTS] Renewal payment failed for user {user['id']}; current period stays")


def _current_paid_expiry(user: dict):
    """Return a future paid-period end, or None for complimentary Pro and other tiers."""
    if user.get("tier") != "pro":
        return None
    expires = parse_timestamp(user.get("pro_expires_at"))
    if expires and expires > datetime.now(timezone.utc):
        return expires
    return None


def _period_is_active(user: dict) -> bool:
    if user.get("tier") == "admin":
        return True
    if user.get("tier") == "pro" and not access_expired(user):
        return True
    return False


@router.post("/webhook")
async def paystack_webhook(request: Request):
    """
    Handle Paystack webhook events.
    Events include: charge.success, subscription.create, subscription.disable, invoice.update, etc.
    """
    payload = await request.body()
    sig_header = request.headers.get("x-paystack-signature")
    
    # Paystack signs the raw payload with the API secret key (not a separate
    # webhook secret). Fail closed if the secret or signature is unavailable.
    # See https://paystack.com/docs/payments/webhooks/
    if not settings.paystack_secret_key:
        logger.error("[PAYMENTS] Cannot validate webhook without Paystack secret key")
        raise HTTPException(status_code=503, detail="Payment service not configured")
    computed_signature = hmac.new(
        settings.paystack_secret_key.encode("utf-8"), payload, hashlib.sha512
    ).hexdigest()
    if not sig_header or not hmac.compare_digest(
        sig_header.encode("utf-8"), computed_signature.encode("ascii")
    ):
        logger.warning("[PAYMENTS] Invalid Paystack webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        event = await request.json()
    except Exception:
        logger.error("[PAYMENTS] Invalid webhook payload")
        raise HTTPException(status_code=400, detail="Invalid payload")

    event_type = event.get("event")
    data = event.get("data", {})
    
    logger.info(f"[PAYMENTS] Paystack webhook event: {event_type}")

    db = Database(use_admin=True)

    if event_type == "charge.success":
        reference = data.get("reference")
        if not reference:
            logger.warning("[PAYMENTS] charge.success without a reference")
            return {"status": "ignored"}
        # Confirm the charge with Paystack. The signed body is not enough:
        # a real but cheaper transaction created outside our checkout must not grant Pro.
        verified = await paystack_request("GET", f"/transaction/verify/{reference}")
        verified_data = verified.get("data") or {}
        if verified_data.get("reference") not in (None, reference):
            logger.error(f"[PAYMENTS] Verification reference mismatch for {reference}")
            return {"status": "ignored"}
        return await apply_successful_charge(db, reference, verified_data)

    if event_type == "charge.failed":
        reference = data.get("reference")
        if reference:
            await db.update_payment_by_reference(reference, {"status": "failed"}, status="pending")
        return {"status": "success"}

    if event_type == "subscription.create":
        # Store the provider ids only. Access still comes from a verified charge.
        await _store_paystack_subscription(db, data, grant_access=False)
        return {"status": "success"}

    if event_type in ["subscription.disable", "subscription.not_renew"]:
        await _mark_subscription_cancelled(db, data)
        return {"status": "success"}

    if event_type == "invoice.payment_failed":
        await _mark_renewal_failed(db, data)
        return {"status": "success"}

    if event_type == "invoice.update" and data.get("paid"):
        # A renewal only extends access when it matches a server-created checkout.
        # Unsolicited invoices are logged and do not grant Pro.
        reference = data.get("reference") or (data.get("transaction") or {}).get("reference")
        logger.info(f"[PAYMENTS] Invoice paid reference={reference}; access changes only via a stored checkout")
        if reference:
            payment = await db.get_payment_by_reference(reference)
            if payment and payment.get("status") == "pending":
                verified = await paystack_request("GET", f"/transaction/verify/{reference}")
                return await apply_successful_charge(db, reference, verified.get("data") or {})
        return {"status": "ignored"}

    return {"status": "success"}


@router.get("/verify/{reference}")
async def verify_transaction(reference: str, user: dict = Depends(get_current_user)):
    """Verify the current user's checkout and grant its period if Paystack has been paid."""
    if not settings.paystack_secret_key:
        raise HTTPException(status_code=503, detail="Payment service not configured")

    db = Database(use_admin=True)
    payment = await db.get_payment_by_reference(reference)
    if not payment or payment.get("user_id") != user.get("id"):
        raise HTTPException(status_code=404, detail="Transaction not found")

    try:
        if payment.get("status") == "pending":
            result = await paystack_request("GET", f"/transaction/verify/{reference}")
            await apply_successful_charge(db, reference, result.get("data") or {})
            payment = await db.get_payment_by_reference(reference) or payment
        return {
            "status": payment.get("status"),
            "amount": payment.get("amount"),
            "currency": payment.get("currency"),
            "access_until": payment.get("access_until"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PAYMENTS] Error verifying transaction: {e}")
        raise HTTPException(status_code=500, detail="Failed to verify transaction")


@router.get("/subscription-status")
async def get_subscription_status(user: dict = Depends(get_current_user)):
    """Get the current user's paid-access status from the local period."""
    expires = parse_timestamp(user.get("pro_expires_at"))
    active = _period_is_active(user)
    db = Database(use_admin=True)
    subscription = await db.get_subscription(user["id"])
    return {
        "active": active,
        "tier": get_effective_tier(user),
        "current_period_end": expires.isoformat() if expires else None,
        "cancel_at_period_end": bool(subscription and subscription.get("cancel_at_period_end")),
        "message": "Pro access is active" if active else "No active Pro period",
    }


@router.post("/cancel-subscription")
async def cancel_subscription(user: dict = Depends(get_current_user)):
    """Stop renewal. Access already paid for lasts until the period ends."""
    if user.get("tier") == "admin":
        raise HTTPException(status_code=400, detail="Admin access is not a Paystack subscription")
    expires = _current_paid_expiry(user)
    subscription_code = user.get("paystack_subscription_code")
    if expires is None and not subscription_code:
        raise HTTPException(status_code=400, detail="No active Pro period to cancel")

    if subscription_code:
        if not settings.paystack_secret_key:
            raise HTTPException(status_code=503, detail="Payment service not configured")
        try:
            result = await paystack_request("GET", f"/subscription/{subscription_code}")
            email_token = (result.get("data") or {}).get("email_token")
            if not email_token:
                raise HTTPException(status_code=400, detail="Could not retrieve subscription details")
            await paystack_request("POST", "/subscription/disable", {
                "code": subscription_code,
                "token": email_token,
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[PAYMENTS] Error cancelling Paystack subscription: {e}")
            raise HTTPException(status_code=500, detail="Failed to cancel subscription")

    db = Database(use_admin=True)
    if expires is not None:
        await db.update_user(user["id"], {"paystack_subscription_code": None})
        await _save_subscription_period(db, user["id"], expires, cancelled=True)
        logger.info(f"[PAYMENTS] Renewal cancelled for user {user['id']}")
        return {
            "status": "cancelled",
            "tier": "pro",
            "access_until": expires.isoformat(),
            "message": "Renewal is cancelled. Pro access lasts until the end of the paid period.",
        }

    # A Paystack subscription with no recorded period was the old indefinite grant.
    now = datetime.now(timezone.utc)
    updated = await db.update_user(user["id"], {
        "tier": "free",
        "pro_expires_at": now.isoformat(),
        "paystack_subscription_code": None,
    })
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to cancel subscription")
    logger.info(f"[PAYMENTS] Indefinite subscription ended for user {user['id']}")
    return {
        "status": "cancelled",
        "tier": "free",
        "access_until": now.isoformat(),
        "message": "Subscription cancelled.",
    }


@router.get("/payment-history")
async def get_payment_history(user: dict = Depends(get_current_user)):
    """Get user's payment history"""
    db = Database(use_admin=True)
    payments = await db.get_payment_history(user["id"])
    return {"payments": payments}


@router.get("/manage-subscription")
async def get_manage_subscription_link(user: dict = Depends(get_current_user)):
    """
    Get a link for the user to manage their subscription.
    Paystack sends users to their hosted portal via email.
    """
    expires = parse_timestamp(user.get("pro_expires_at"))
    if not _period_is_active(user):
        return {
            "has_subscription": False,
            "message": "No active Pro period. Subscribe to Pro to manage billing."
        }

    # One-off charges have no Paystack billing portal. Cancellation keeps the
    # paid period and stops a future renewal if a Paystack subscription exists.
    return {
        "has_subscription": True,
        "tier": get_effective_tier(user),
        "current_period_end": expires.isoformat() if expires else None,
        "message": "Pro access lasts until the end of the paid period. Cancel to stop renewal.",
        "cancel_url": "/api/payments/cancel-subscription",
    }
