"""
Watchman Payments Routes
Stripe integration for Pro subscriptions
"""

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from app.config import get_settings
from app.database import Database
from app.middleware.auth import get_current_user
from app.services.email_service import get_email_service


router = APIRouter()
settings = get_settings()

# Initialize Stripe
stripe.api_key = settings.stripe_secret_key


@router.post("/create-checkout-session")
async def create_checkout_session(user: dict = Depends(get_current_user)):
    """Create a Stripe checkout session for Pro subscription"""
    if not settings.stripe_secret_key or not settings.stripe_price_id_pro:
        raise HTTPException(status_code=503, detail="Payment service not configured")

    db = Database(use_admin=True)
    user_email = user.get("email")
    user_id = user.get("id")

    try:
        # Check if user already has a Stripe customer ID
        stripe_customer_id = user.get("stripe_customer_id")

        if not stripe_customer_id:
            # Create a new Stripe customer
            customer = stripe.Customer.create(
                email=user_email,
                metadata={"watchman_user_id": user_id}
            )
            stripe_customer_id = customer.id
            # Save customer ID to user
            await db.update_user(user_id, {"stripe_customer_id": stripe_customer_id})
            logger.info(f"[PAYMENTS] Created Stripe customer {stripe_customer_id} for user {user_id}")

        # Create checkout session
        session = stripe.checkout.Session.create(
            customer=stripe_customer_id,
            payment_method_types=["card"],
            line_items=[{
                "price": settings.stripe_price_id_pro,
                "quantity": 1,
            }],
            mode="subscription",
            success_url="https://trywatchman.app/dashboard/settings?success=true",
            cancel_url="https://trywatchman.app/dashboard/settings?canceled=true",
            metadata={"watchman_user_id": user_id},
        )

        logger.info(f"[PAYMENTS] Created checkout session {session.id} for user {user_id}")
        return {"checkout_url": session.url, "session_id": session.id}

    except stripe.error.StripeError as e:
        logger.error(f"[PAYMENTS] Stripe error: {e}")
        raise HTTPException(status_code=500, detail="Failed to create checkout session")


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events"""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not settings.stripe_webhook_secret:
        logger.error("[PAYMENTS] Webhook secret not configured")
        raise HTTPException(status_code=500, detail="Webhook not configured")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except ValueError:
        logger.error("[PAYMENTS] Invalid webhook payload")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        logger.error("[PAYMENTS] Invalid webhook signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    logger.info(f"[PAYMENTS] Webhook event: {event['type']}")

    db = Database(use_admin=True)
    email_service = get_email_service()

    # Handle subscription events
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_id = session.get("customer")
        subscription_id = session.get("subscription")
        user_id = session.get("metadata", {}).get("watchman_user_id")

        logger.info(f"[PAYMENTS] Checkout completed - customer: {customer_id}, user: {user_id}")

        if user_id:
            # Update user to Pro
            await db.update_user(user_id, {
                "tier": "pro",
                "stripe_subscription_id": subscription_id,
            })

            # Get user details for email
            user = await db.get_user_by_id(user_id)
            if user:
                user_email = user.get("email")
                user_name = user.get("name") or user_email.split("@")[0] if user_email else "there"

                # Send Pro upgrade email
                try:
                    await email_service.send_pro_upgrade_email(
                        to=user_email,
                        user_name=user_name,
                    )
                    logger.info(f"[PAYMENTS] Pro upgrade email sent to {user_email}")
                except Exception as e:
                    logger.warning(f"[PAYMENTS] Failed to send Pro upgrade email: {e}")

            logger.info(f"[PAYMENTS] User {user_id} upgraded to Pro")

    elif event["type"] == "customer.subscription.updated":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer")
        status = subscription.get("status")

        logger.info(f"[PAYMENTS] Subscription updated - customer: {customer_id}, status: {status}")

        # Find user by stripe customer ID and update subscription status
        user = await db.get_user_by_stripe_customer(customer_id)
        if user:
            if status == "active":
                await db.update_user(user["id"], {"tier": "pro"})
            elif status in ["canceled", "unpaid", "past_due"]:
                await db.update_user(user["id"], {"tier": "free"})
            logger.info(f"[PAYMENTS] Updated user {user['id']} tier based on status: {status}")

    elif event["type"] == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer")

        logger.info(f"[PAYMENTS] Subscription deleted - customer: {customer_id}")

        # Find user and downgrade to free
        user = await db.get_user_by_stripe_customer(customer_id)
        if user:
            await db.update_user(user["id"], {
                "tier": "free",
                "stripe_subscription_id": None,
            })
            logger.info(f"[PAYMENTS] User {user['id']} downgraded to free")

    elif event["type"] == "invoice.paid":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        amount = invoice.get("amount_paid", 0) / 100  # Convert from cents

        logger.info(f"[PAYMENTS] Invoice paid - customer: {customer_id}, amount: ${amount}")

        # Store payment record
        user = await db.get_user_by_stripe_customer(customer_id)
        if user:
            await db.create_payment_record({
                "user_id": user["id"],
                "stripe_invoice_id": invoice.get("id"),
                "amount": amount,
                "currency": invoice.get("currency", "usd"),
                "status": "paid",
                "description": "Watchman Pro Subscription",
            })
            logger.info(f"[PAYMENTS] Payment record created for user {user['id']}")

    return {"status": "success"}


@router.get("/billing-portal")
async def get_billing_portal(user: dict = Depends(get_current_user)):
    """Get Stripe billing portal URL for managing subscription"""
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Payment service not configured")

    stripe_customer_id = user.get("stripe_customer_id")
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="No billing account found")

    try:
        session = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url="https://trywatchman.app/dashboard/settings",
        )
        return {"url": session.url}
    except stripe.error.StripeError as e:
        logger.error(f"[PAYMENTS] Billing portal error: {e}")
        raise HTTPException(status_code=500, detail="Failed to access billing portal")


@router.get("/payment-history")
async def get_payment_history(user: dict = Depends(get_current_user)):
    """Get user's payment history"""
    db = Database(use_admin=True)
    payments = await db.get_payment_history(user["id"])
    return {"payments": payments}
