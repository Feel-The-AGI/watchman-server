"""
Watchman Payments Routes
Paystack integration for Pro subscriptions ($12/month, charged in GHS)
Uses dynamic USD→GHS conversion via exchange rate API
"""

import hashlib
import hmac
import time
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from app.config import get_settings
from app.database import Database
from app.middleware.auth import get_current_user
from app.services.email_service import get_email_service


router = APIRouter()
settings = get_settings()

# Paystack API configuration
PAYSTACK_BASE_URL = "https://api.paystack.co"

# Cache exchange rate for 1 hour to avoid too many API calls
_exchange_rate_cache = {"rate": None, "timestamp": 0}
EXCHANGE_RATE_CACHE_SECONDS = 3600  # 1 hour


async def get_usd_to_ghs_rate() -> float:
    """
    Fetch current USD to GHS exchange rate.
    Uses exchangerate-api.com service.
    Caches result for 1 hour.
    """
    current_time = time.time()
    
    # Check cache
    if (_exchange_rate_cache["rate"] is not None and 
        current_time - _exchange_rate_cache["timestamp"] < EXCHANGE_RATE_CACHE_SECONDS):
        logger.debug(f"[EXCHANGE] Using cached rate: {_exchange_rate_cache['rate']}")
        return _exchange_rate_cache["rate"]
    
    # Fetch fresh rate
    try:
        async with httpx.AsyncClient() as client:
            api_key = settings.exchange_rate_api_key
            if not api_key:
                # Fallback to a reasonable default if no API key
                logger.warning("[EXCHANGE] No API key, using fallback rate of 14.5")
                return 14.5
            
            # ExchangeRate-API format
            url = f"https://v6.exchangerate-api.com/v6/{api_key}/pair/USD/GHS"
            response = await client.get(url, timeout=10.0)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("result") == "success":
                    rate = data.get("conversion_rate", 14.5)
                    _exchange_rate_cache["rate"] = rate
                    _exchange_rate_cache["timestamp"] = current_time
                    logger.info(f"[EXCHANGE] Fetched fresh USD→GHS rate: {rate}")
                    return rate
            
            logger.warning(f"[EXCHANGE] API error, using fallback rate")
            return 14.5
            
    except Exception as e:
        logger.error(f"[EXCHANGE] Error fetching rate: {e}, using fallback")
        return 14.5


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


@router.post("/create-checkout-session")
async def create_checkout_session(user: dict = Depends(get_current_user)):
    """
    Initialize a Paystack transaction for Pro subscription ($12/month).
    Converts USD to GHS dynamically using current exchange rate.
    Returns a checkout URL for the user to complete payment.
    """
    if not settings.paystack_secret_key:
        raise HTTPException(status_code=503, detail="Payment service not configured")

    user_email = user.get("email")
    user_id = user.get("id")

    if not user_email:
        raise HTTPException(status_code=400, detail="User email required for payment")

    try:
        # Get current exchange rate
        exchange_rate = await get_usd_to_ghs_rate()
        
        # Convert $12 USD to GHS
        usd_price = settings.pro_price_usd  # $12
        ghs_amount = usd_price * exchange_rate
        
        # Round to 2 decimal places for display, convert to pesewas for Paystack
        ghs_amount_rounded = round(ghs_amount, 2)
        amount_in_pesewas = int(ghs_amount_rounded * 100)  # Paystack uses smallest unit
        
        logger.info(f"[PAYMENTS] Converting ${usd_price} USD → GHS {ghs_amount_rounded} (rate: {exchange_rate})")
        
        # Initialize transaction in GHS
        transaction_data = {
            "email": user_email,
            "amount": amount_in_pesewas,  # Amount in pesewas
            "currency": "GHS",
            "callback_url": "https://trywatchman.app/dashboard/settings?success=true",
            "metadata": {
                "watchman_user_id": user_id,
                "plan": "pro",
                "usd_price": usd_price,
                "exchange_rate": exchange_rate,
                "ghs_amount": ghs_amount_rounded,
                "custom_fields": [
                    {
                        "display_name": "Plan",
                        "variable_name": "plan",
                        "value": "Watchman Pro"
                    },
                    {
                        "display_name": "USD Equivalent",
                        "variable_name": "usd_equivalent",
                        "value": f"${usd_price}"
                    }
                ]
            },
            "channels": ["card", "apple_pay"],  # Card and Apple Pay only
        }

        result = await paystack_request("POST", "/transaction/initialize", transaction_data)
        
        data = result.get("data", {})
        authorization_url = data.get("authorization_url")
        access_code = data.get("access_code")
        reference = data.get("reference")

        logger.info(f"[PAYMENTS] Paystack transaction initialized for user {user_id}, ref: {reference}, GHS {ghs_amount_rounded}")
        
        return {
            "checkout_url": authorization_url,
            "access_code": access_code,
            "reference": reference,
            "amount_ghs": ghs_amount_rounded,
            "amount_usd": usd_price,
            "exchange_rate": exchange_rate,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PAYMENTS] Error initializing Paystack transaction: {e}")
        raise HTTPException(status_code=500, detail="Failed to create checkout session")


@router.get("/pricing")
async def get_pricing():
    """
    Get current pricing information including exchange rate.
    This endpoint is public (no auth required) so pricing page can use it.
    """
    try:
        exchange_rate = await get_usd_to_ghs_rate()
        usd_price = settings.pro_price_usd
        ghs_amount = round(usd_price * exchange_rate, 2)
        
        return {
            "usd_price": usd_price,
            "ghs_amount": ghs_amount,
            "exchange_rate": exchange_rate,
            "currency": "GHS",
            "display_price": f"${int(usd_price)}",
            "display_ghs": f"GHS {ghs_amount:.2f}",
            "note": f"Approximately ${int(usd_price)} USD at current exchange rate",
        }
    except Exception as e:
        logger.error(f"[PAYMENTS] Error getting pricing: {e}")
        # Return fallback pricing
        return {
            "usd_price": 12.0,
            "ghs_amount": 174.0,  # Fallback estimate
            "exchange_rate": 14.5,
            "currency": "GHS",
            "display_price": "$12",
            "display_ghs": "GHS 174.00",
            "note": "Approximately $12 USD",
        }


@router.post("/webhook")
async def paystack_webhook(request: Request):
    """
    Handle Paystack webhook events.
    Events include: charge.success, subscription.create, subscription.disable, invoice.update, etc.
    """
    payload = await request.body()
    sig_header = request.headers.get("x-paystack-signature")
    
    # Verify webhook signature if secret is configured
    if settings.paystack_webhook_secret:
        computed_signature = hmac.new(
            settings.paystack_webhook_secret.encode('utf-8'),
            payload,
            hashlib.sha512
        ).hexdigest()
        
        if sig_header != computed_signature:
            logger.error("[PAYMENTS] Invalid Paystack webhook signature")
            raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        event = await request.json()
    except Exception:
        logger.error("[PAYMENTS] Invalid webhook payload")
        raise HTTPException(status_code=400, detail="Invalid payload")

    event_type = event.get("event")
    data = event.get("data", {})
    
    logger.info(f"[PAYMENTS] Paystack webhook event: {event_type}")

    db = Database(use_admin=True)
    email_service = get_email_service()

    # Handle different event types
    if event_type == "charge.success":
        # Payment was successful
        customer = data.get("customer", {})
        customer_email = customer.get("email")
        customer_code = customer.get("customer_code")
        amount = data.get("amount", 0) / 100  # Convert from pesewas to GHS
        currency = data.get("currency", "GHS")
        reference = data.get("reference")
        metadata = data.get("metadata", {})
        user_id = metadata.get("watchman_user_id")
        usd_price = metadata.get("usd_price", 12.0)  # Original USD price
        exchange_rate = metadata.get("exchange_rate", 0)
        
        logger.info(f"[PAYMENTS] Charge success - email: {customer_email}, amount: {amount} {currency} (${usd_price} USD)")
        
        # Find user by ID from metadata, or by email
        user = None
        if user_id:
            user = await db.get_user_by_id(user_id)
        
        if not user and customer_email:
            user = await db.get_user_by_email(customer_email)
        
        if user:
            # Update user to Pro and store Paystack customer code
            await db.update_user(user["id"], {
                "tier": "pro",
                "paystack_customer_code": customer_code,
                "paystack_subscription_code": data.get("subscription_code"),
            })
            
            # Create payment record with both GHS and USD amounts
            await db.create_payment_record({
                "user_id": user["id"],
                "paystack_reference": reference,
                "amount": usd_price,  # Store USD amount for display
                "amount_local": amount,  # GHS amount actually charged
                "currency": "usd",  # Display currency
                "currency_local": currency.lower(),  # Actual charge currency
                "exchange_rate": exchange_rate,
                "status": "paid",
                "description": f"Watchman Pro - ${int(usd_price)}/month (GHS {amount:.2f})",
            })
            
            # Send Pro upgrade email
            user_name = user.get("name") or customer_email.split("@")[0] if customer_email else "there"
            try:
                await email_service.send_pro_upgrade_email(
                    to=customer_email,
                    user_name=user_name,
                )
                logger.info(f"[PAYMENTS] Pro upgrade email sent to {customer_email}")
            except Exception as e:
                logger.warning(f"[PAYMENTS] Failed to send Pro upgrade email: {e}")
            
            logger.info(f"[PAYMENTS] User {user['id']} upgraded to Pro")
        else:
            logger.warning(f"[PAYMENTS] Could not find user for charge: {customer_email}")

    elif event_type == "subscription.create":
        # New subscription created
        customer = data.get("customer", {})
        customer_email = customer.get("email")
        customer_code = customer.get("customer_code")
        subscription_code = data.get("subscription_code")
        plan = data.get("plan", {})
        
        logger.info(f"[PAYMENTS] Subscription created - email: {customer_email}, plan: {plan.get('name')}")
        
        user = await db.get_user_by_paystack_customer(customer_code)
        if not user and customer_email:
            user = await db.get_user_by_email(customer_email)
        
        if user:
            await db.update_user(user["id"], {
                "tier": "pro",
                "paystack_customer_code": customer_code,
                "paystack_subscription_code": subscription_code,
            })
            logger.info(f"[PAYMENTS] User {user['id']} subscription activated")

    elif event_type in ["subscription.disable", "subscription.not_renew"]:
        # Subscription cancelled or not renewed
        customer = data.get("customer", {})
        customer_code = customer.get("customer_code")
        customer_email = customer.get("email")
        
        logger.info(f"[PAYMENTS] Subscription disabled/not renewed - customer: {customer_code}")
        
        user = await db.get_user_by_paystack_customer(customer_code)
        if not user and customer_email:
            user = await db.get_user_by_email(customer_email)
        
        if user:
            await db.update_user(user["id"], {
                "tier": "free",
                "paystack_subscription_code": None,
            })
            logger.info(f"[PAYMENTS] User {user['id']} downgraded to free")

    elif event_type == "invoice.payment_failed":
        # Payment failed for subscription renewal
        customer = data.get("customer", {})
        customer_code = customer.get("customer_code")
        customer_email = customer.get("email")
        
        logger.warning(f"[PAYMENTS] Invoice payment failed - customer: {customer_code}")
        
        user = await db.get_user_by_paystack_customer(customer_code)
        if user:
            # Could send a payment failed email here
            logger.warning(f"[PAYMENTS] Payment failed for user {user['id']}")

    elif event_type == "invoice.update" and data.get("paid"):
        # Recurring payment successful
        customer = data.get("customer", {})
        customer_code = customer.get("customer_code")
        customer_email = customer.get("email")
        amount = data.get("amount", 0) / 100
        
        logger.info(f"[PAYMENTS] Invoice paid - customer: {customer_code}, amount: ${amount}")
        
        user = await db.get_user_by_paystack_customer(customer_code)
        if user:
            # Create payment record for recurring payment
            await db.create_payment_record({
                "user_id": user["id"],
                "paystack_reference": data.get("reference"),
                "amount": amount,
                "currency": data.get("currency", "usd").lower(),
                "status": "paid",
                "description": "Watchman Pro Subscription Renewal - $12/month",
            })
            logger.info(f"[PAYMENTS] Recurring payment recorded for user {user['id']}")

    return {"status": "success"}


@router.get("/verify/{reference}")
async def verify_transaction(reference: str, user: dict = Depends(get_current_user)):
    """Verify a Paystack transaction by reference"""
    if not settings.paystack_secret_key:
        raise HTTPException(status_code=503, detail="Payment service not configured")
    
    try:
        result = await paystack_request("GET", f"/transaction/verify/{reference}")
        data = result.get("data", {})
        
        return {
            "status": data.get("status"),
            "amount": data.get("amount", 0) / 100,
            "currency": data.get("currency"),
            "paid_at": data.get("paid_at"),
            "channel": data.get("channel"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PAYMENTS] Error verifying transaction: {e}")
        raise HTTPException(status_code=500, detail="Failed to verify transaction")


@router.get("/subscription-status")
async def get_subscription_status(user: dict = Depends(get_current_user)):
    """Get the current user's subscription status"""
    subscription_code = user.get("paystack_subscription_code")
    
    if not subscription_code:
        return {
            "active": False,
            "tier": user.get("tier", "free"),
            "message": "No active subscription"
        }
    
    if not settings.paystack_secret_key:
        return {
            "active": user.get("tier") == "pro",
            "tier": user.get("tier", "free"),
            "message": "Payment service not configured"
        }
    
    try:
        result = await paystack_request("GET", f"/subscription/{subscription_code}")
        data = result.get("data", {})
        
        return {
            "active": data.get("status") == "active",
            "tier": user.get("tier", "free"),
            "next_payment_date": data.get("next_payment_date"),
            "plan": data.get("plan", {}).get("name"),
            "amount": data.get("amount", 0) / 100,
            "email_token": data.get("email_token"),  # For managing subscription
        }
    except Exception as e:
        logger.error(f"[PAYMENTS] Error getting subscription status: {e}")
        return {
            "active": user.get("tier") == "pro",
            "tier": user.get("tier", "free"),
            "message": "Could not fetch subscription details"
        }


@router.post("/cancel-subscription")
async def cancel_subscription(user: dict = Depends(get_current_user)):
    """Cancel the user's subscription"""
    subscription_code = user.get("paystack_subscription_code")
    
    if not subscription_code:
        raise HTTPException(status_code=400, detail="No active subscription to cancel")
    
    if not settings.paystack_secret_key:
        raise HTTPException(status_code=503, detail="Payment service not configured")
    
    try:
        # Get subscription to get email_token
        result = await paystack_request("GET", f"/subscription/{subscription_code}")
        data = result.get("data", {})
        email_token = data.get("email_token")
        
        if not email_token:
            raise HTTPException(status_code=400, detail="Could not retrieve subscription details")
        
        # Disable subscription
        await paystack_request("POST", "/subscription/disable", {
            "code": subscription_code,
            "token": email_token,
        })
        
        # Update user
        db = Database(use_admin=True)
        await db.update_user(user["id"], {
            "tier": "free",
            "paystack_subscription_code": None,
        })
        
        logger.info(f"[PAYMENTS] Subscription cancelled for user {user['id']}")
        
        return {"status": "cancelled", "message": "Subscription has been cancelled"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PAYMENTS] Error cancelling subscription: {e}")
        raise HTTPException(status_code=500, detail="Failed to cancel subscription")


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
    subscription_code = user.get("paystack_subscription_code")
    customer_code = user.get("paystack_customer_code")
    
    if not subscription_code or not customer_code:
        return {
            "has_subscription": False,
            "message": "No active subscription. Subscribe to Pro to manage billing."
        }
    
    # Paystack doesn't have a direct billing portal like Stripe
    # Users manage subscriptions through emails from Paystack
    # We can provide the dashboard settings page as the management location
    return {
        "has_subscription": True,
        "tier": user.get("tier"),
        "message": "Subscription management is available through your email. Check for emails from Paystack to update payment methods or cancel.",
        "cancel_url": "/api/payments/cancel-subscription",
    }
