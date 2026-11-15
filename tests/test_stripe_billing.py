"""Stripe billing: signatures, form encoding, checkout, webhooks, cancel."""

import json
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.billing import RENEWAL_GRACE
from app.routes import payments
from app.stripe_client import (
    STRIPE_VERSION,
    StripeError,
    encode_form,
    invoice_period_end,
    invoice_subscription_id,
    sign_payload,
    stripe_request,
    subscription_period_end,
    verify_signature,
)
from tests.test_security_regressions import make_request

SECRET = "whsec_test_secret"
NOW = int(time.time())
PERIOD_END = NOW + 30 * 86400


class MemoryDB:
    def __init__(self):
        self.users = {}
        self.payments = []
        self.subs = {}

    async def get_user_by_id(self, user_id):
        row = self.users.get(user_id)
        return dict(row) if row else None

    async def update_user(self, user_id, data):
        self.users[user_id].update(data)
        return dict(self.users[user_id])

    async def get_user_by_stripe_customer(self, customer):
        for user in self.users.values():
            if user.get("stripe_customer_id") == customer:
                return dict(user)
        return None

    async def get_subscription(self, user_id):
        row = self.subs.get(user_id)
        return dict(row) if row else None

    async def create_subscription(self, data):
        row = {"id": str(uuid4()), **data}
        self.subs[data["user_id"]] = row
        return dict(row)

    async def update_subscription(self, subscription_id, data):
        for row in self.subs.values():
            if row["id"] == subscription_id:
                row.update(data)
                return dict(row)
        return None

    async def get_payment_by_invoice(self, invoice_id):
        for row in self.payments:
            if row.get("stripe_invoice_id") == invoice_id:
                return dict(row)
        return None

    async def create_payment_record(self, data):
        self.payments.append(dict(data))
        return dict(data)

    async def get_payment_history(self, user_id):
        return [row for row in self.payments if row["user_id"] == user_id]


class FakeStripe:
    """Records calls and answers like the Stripe API for the paths we use."""

    def __init__(self):
        self.calls = []
        self.subscriptions = {}
        self.prices = []
        self.sessions = {}

    async def __call__(self, secret_key, method, path, data=None, **kwargs):
        self.calls.append((method, path, data or {}, kwargs))
        if path == "/customers":
            return {"id": "cus_1"}
        if path == "/prices" and method == "GET":
            return {"data": list(self.prices)}
        if path == "/products":
            return {"id": "prod_1"}
        if path == "/prices":
            price = {
                "id": f"price_{len(self.prices) + 1}",
                "unit_amount": data["unit_amount"],
                "currency": "usd",
                "recurring": {"interval": data["recurring"]["interval"]},
                "product": data["product"],
            }
            self.prices.append(price)
            return price
        if path == "/checkout/sessions":
            return {"id": "cs_test_1", "url": "https://checkout.stripe.test/c/pay/cs_test_1"}
        if path.startswith("/checkout/sessions/"):
            return self.sessions[path.rsplit("/", 1)[1]]
        if path.startswith("/subscriptions/") and method == "GET":
            return self.subscriptions[path.rsplit("/", 1)[1]]
        if path.startswith("/subscriptions/") and method == "POST":
            sub = self.subscriptions[path.rsplit("/", 1)[1]]
            sub["cancel_at_period_end"] = data["cancel_at_period_end"]
            return sub
        if path == "/billing_portal/sessions":
            return {"url": "https://billing.stripe.test/p/session"}
        raise AssertionError(f"unexpected Stripe call {method} {path}")


def subscription(status="active", *, end=PERIOD_END, interval="month", new_layout=False, cancel=False):
    item = {"price": {"id": "price_1", "recurring": {"interval": interval}}}
    sub = {
        "id": "sub_1",
        "object": "subscription",
        "customer": "cus_1",
        "status": status,
        "cancel_at_period_end": cancel,
        "metadata": {"watchman_user_id": "alice"},
        "items": {"data": [item]},
    }
    if new_layout:
        item["current_period_end"] = end
    else:
        sub["current_period_end"] = end
    return sub


@pytest.fixture
def env(monkeypatch):
    db = MemoryDB()
    db.users["alice"] = {"id": "alice", "email": "alice@example.com", "name": "Alice", "tier": "free"}
    fake = FakeStripe()
    sent = []

    class Emails:
        async def send_pro_upgrade_email(self, **kwargs):
            sent.append(("user", kwargs))

        async def send_admin_new_subscriber_notification(self, **kwargs):
            sent.append(("admin", kwargs))

    monkeypatch.setattr(payments, "Database", lambda **kwargs: db)
    monkeypatch.setattr(payments, "stripe_request", fake)
    monkeypatch.setattr(payments, "get_email_service", lambda: Emails())
    monkeypatch.setattr(payments.settings, "stripe_secret_key", "sk_test_123")
    monkeypatch.setattr(payments.settings, "stripe_webhook_secret", SECRET)
    monkeypatch.setattr(payments.settings, "pro_price_usd", 12.0)
    monkeypatch.setattr(payments.settings, "pro_annual_price_usd", 96.0)
    payments._price_cache.clear()
    return db, fake, sent


def event(kind, obj):
    return json.dumps({"id": "evt_1", "type": kind, "data": {"object": obj}}).encode()


async def deliver(kind, obj):
    body = event(kind, obj)
    return await payments.stripe_webhook(make_request(body, sign_payload(body, SECRET)))


# --- client helpers ---------------------------------------------------------

def test_form_encoding_matches_stripe_nesting():
    pairs = encode_form({
        "mode": "subscription",
        "line_items": [{"price": "price_1", "quantity": 1}],
        "subscription_data": {"metadata": {"watchman_user_id": "u1"}},
        "allow_promotion_codes": True,
        "skip": None,
    })
    assert pairs == [
        ("mode", "subscription"),
        ("line_items[0][price]", "price_1"),
        ("line_items[0][quantity]", "1"),
        ("subscription_data[metadata][watchman_user_id]", "u1"),
        ("allow_promotion_codes", "true"),
    ]


def test_signature_accepts_only_fresh_matching_signatures():
    body = b'{"id":"evt"}'
    header = sign_payload(body, SECRET, NOW)
    assert verify_signature(body, header, SECRET, now=NOW)
    assert verify_signature(body, header + ",v1=deadbeef", SECRET, now=NOW)
    assert not verify_signature(body + b" ", header, SECRET, now=NOW)
    assert not verify_signature(body, header, "whsec_other", now=NOW)
    assert not verify_signature(body, header, SECRET, now=NOW + 301)
    assert not verify_signature(body, None, SECRET, now=NOW)
    assert not verify_signature(body, "t=abc,v1=00", SECRET, now=NOW)
    assert not verify_signature(body, header, "", now=NOW)


def test_period_and_subscription_readers_handle_both_api_layouts():
    old = subscription()
    new = subscription(new_layout=True)
    assert subscription_period_end(old) == subscription_period_end(new)
    assert subscription_period_end({}) is None
    old_invoice = {"subscription": "sub_1", "lines": {"data": [{"period": {"end": PERIOD_END}}]}}
    new_invoice = {
        "parent": {"subscription_details": {"subscription": "sub_1"}},
        "lines": {"data": [{"period": {"end": PERIOD_END}}]},
    }
    assert invoice_subscription_id(old_invoice) == invoice_subscription_id(new_invoice) == "sub_1"
    assert invoice_period_end(old_invoice) == invoice_period_end(new_invoice)
    assert invoice_subscription_id({"lines": {"data": []}}) is None


@pytest.mark.asyncio
async def test_stripe_request_sends_version_auth_and_raises_on_errors():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["version"] = request.headers["stripe-version"]
        seen["idem"] = request.headers.get("idempotency-key")
        seen["body"] = request.content.decode()
        if request.url.path.endswith("/bad"):
            return httpx.Response(400, json={"error": {"message": "No such price", "code": "resource_missing"}})
        return httpx.Response(200, json={"id": "ok"})

    transport = httpx.MockTransport(handler)
    result = await stripe_request("sk_test_x", "POST", "/ok", {"a": {"b": 1}}, idempotency_key="k1", transport=transport)
    assert result == {"id": "ok"}
    assert seen["auth"] == "Bearer sk_test_x"
    assert seen["version"] == STRIPE_VERSION
    assert seen["idem"] == "k1"
    assert seen["body"] == "a%5Bb%5D=1"
    with pytest.raises(StripeError) as exc:
        await stripe_request("sk_test_x", "POST", "/bad", {}, transport=transport)
    assert exc.value.status == 400 and exc.value.code == "resource_missing"
    with pytest.raises(StripeError) as exc:
        await stripe_request("", "GET", "/ok")
    assert exc.value.status == 503


# --- pricing and checkout ---------------------------------------------------

@pytest.mark.asyncio
async def test_pricing_lists_monthly_and_annual(env):
    body = await payments.get_pricing()
    assert body["monthly"]["amount"] == 12.0
    assert body["annual"]["amount"] == 96.0
    assert body["annual"]["per_month"] == 8.0
    assert body["annual"]["saving_percent"] == 33
    assert body["configured"] is True


@pytest.mark.asyncio
async def test_checkout_creates_customer_price_and_subscription_session(env):
    db, fake, _ = env
    result = await payments.create_checkout_session(payments.CheckoutRequest(plan="annual"), dict(db.users["alice"]))
    assert result["checkout_url"].startswith("https://checkout.stripe.test/")
    assert db.users["alice"]["stripe_customer_id"] == "cus_1"
    session = next(call for call in fake.calls if call[1] == "/checkout/sessions")[2]
    assert session["mode"] == "subscription"
    assert session["customer"] == "cus_1"
    assert session["client_reference_id"] == "alice"
    assert session["line_items"] == [{"price": "price_1", "quantity": 1}]
    assert "{CHECKOUT_SESSION_ID}" in session["success_url"]
    price = next(call for call in fake.calls if call[1] == "/prices" and call[0] == "POST")[2]
    assert price["unit_amount"] == 9600 and price["recurring"] == {"interval": "year"}
    assert price["lookup_key"] == "watchman_pro_annual"


@pytest.mark.asyncio
async def test_existing_price_is_reused_and_changed_amount_gets_a_new_one(env, monkeypatch):
    db, fake, _ = env
    fake.prices.append({"id": "price_old", "unit_amount": 1200, "currency": "usd",
                        "recurring": {"interval": "month"}, "product": "prod_1"})
    assert await payments.price_id_for("monthly") == "price_old"
    assert not any(call[1] == "/products" for call in fake.calls)
    monkeypatch.setattr(payments.settings, "pro_price_usd", 15.0)
    new_price = await payments.price_id_for("monthly")
    assert new_price != "price_old"
    created = [call for call in fake.calls if call[1] == "/prices" and call[0] == "POST"]
    assert created[-1][2]["product"] == "prod_1" and created[-1][2]["transfer_lookup_key"] is True


@pytest.mark.asyncio
async def test_checkout_is_refused_when_stripe_is_not_configured(env, monkeypatch):
    db, fake, _ = env
    monkeypatch.setattr(payments.settings, "stripe_secret_key", "")
    with pytest.raises(HTTPException) as exc:
        await payments.create_checkout_session(None, dict(db.users["alice"]))
    assert exc.value.status_code == 503
    assert fake.calls == []


@pytest.mark.asyncio
async def test_checkout_refuses_a_second_subscription(env):
    db, fake, _ = env
    db.users["alice"].update({
        "tier": "pro", "stripe_subscription_id": "sub_1",
        "pro_expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
    })
    with pytest.raises(HTTPException) as exc:
        await payments.create_checkout_session(None, dict(db.users["alice"]))
    assert exc.value.status_code == 409
    assert fake.calls == []


# --- granting access ----------------------------------------------------------

@pytest.mark.asyncio
async def test_paid_invoice_grants_pro_until_period_end_once(env):
    db, fake, sent = env
    db.users["alice"]["stripe_customer_id"] = "cus_1"
    fake.subscriptions["sub_1"] = subscription(new_layout=True)
    invoice = {
        "id": "in_1", "customer": "cus_1", "amount_paid": 1200, "currency": "usd",
        "parent": {"subscription_details": {"subscription": "sub_1"}},
        "lines": {"data": [{"period": {"end": PERIOD_END}}]},
    }
    assert await deliver("invoice.paid", invoice) == {"status": "success"}
    assert await deliver("invoice.paid", invoice) == {"status": "success"}
    user = db.users["alice"]
    expected = datetime.fromtimestamp(PERIOD_END, tz=timezone.utc) + RENEWAL_GRACE
    assert user["tier"] == "pro"
    assert user["pro_expires_at"] == expected.isoformat()
    assert user["stripe_subscription_id"] == "sub_1"
    assert len(db.payments) == 1 and db.payments[0]["amount"] == 12.0
    assert [kind for kind, _ in sent] == ["user", "admin"]
    assert db.subs["alice"]["status"] == "active"


@pytest.mark.asyncio
async def test_renewal_moves_access_forward(env):
    db, fake, _ = env
    db.users["alice"]["stripe_customer_id"] = "cus_1"
    fake.subscriptions["sub_1"] = subscription()
    await deliver("customer.subscription.updated", subscription())
    first = db.users["alice"]["pro_expires_at"]
    later = PERIOD_END + 30 * 86400
    await deliver("customer.subscription.updated", subscription(end=later))
    assert db.users["alice"]["pro_expires_at"] > first
    # An older event arriving late never shortens access.
    await deliver("customer.subscription.updated", subscription())
    assert db.users["alice"]["pro_expires_at"] > first


@pytest.mark.asyncio
async def test_checkout_completed_uses_client_reference_when_customer_is_new(env):
    db, fake, _ = env
    fake.subscriptions["sub_1"] = subscription()
    session = {"id": "cs_1", "mode": "subscription", "subscription": "sub_1",
               "customer": "cus_9", "client_reference_id": "alice"}
    assert await deliver("checkout.session.completed", session) == {"status": "success"}
    assert db.users["alice"]["tier"] == "pro"
    assert db.users["alice"]["stripe_customer_id"] == "cus_1"


@pytest.mark.asyncio
async def test_unknown_customer_and_payment_mode_sessions_are_ignored(env):
    db, fake, _ = env
    assert await deliver("checkout.session.completed", {"mode": "payment"}) == {"status": "ignored"}
    invoice = {"id": "in_2", "customer": "cus_unknown", "subscription": "sub_1", "lines": {"data": []}}
    assert await deliver("invoice.paid", invoice) == {"status": "ignored"}
    assert db.users["alice"]["tier"] == "free"


@pytest.mark.asyncio
async def test_incomplete_subscription_does_not_grant_pro(env):
    db, fake, _ = env
    db.users["alice"]["stripe_customer_id"] = "cus_1"
    await deliver("customer.subscription.created", subscription("incomplete"))
    assert db.users["alice"]["tier"] == "free"


@pytest.mark.asyncio
async def test_deleted_subscription_keeps_paid_days_and_clears_renewal(env):
    db, fake, _ = env
    db.users["alice"]["stripe_customer_id"] = "cus_1"
    await deliver("customer.subscription.updated", subscription())
    paid_until = db.users["alice"]["pro_expires_at"]
    await deliver("customer.subscription.deleted", subscription("canceled"))
    assert db.users["alice"]["tier"] == "pro"
    assert db.users["alice"]["pro_expires_at"] == paid_until
    assert db.users["alice"]["stripe_subscription_id"] is None
    assert db.subs["alice"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_admin_is_never_downgraded_by_a_subscription(env):
    db, fake, _ = env
    db.users["alice"].update({"tier": "admin", "stripe_customer_id": "cus_1"})
    await deliver("customer.subscription.updated", subscription())
    assert db.users["alice"]["tier"] == "admin"


@pytest.mark.asyncio
async def test_failed_renewal_marks_past_due(env):
    db, fake, _ = env
    db.users["alice"]["stripe_customer_id"] = "cus_1"
    await deliver("customer.subscription.updated", subscription())
    await deliver("invoice.payment_failed", {"id": "in_3", "customer": "cus_1"})
    assert db.subs["alice"]["status"] == "past_due"
    assert db.users["alice"]["tier"] == "pro"


# --- return from checkout -------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_session_grants_access_for_the_owner_only(env):
    db, fake, _ = env
    fake.sessions["cs_test_1"] = {"id": "cs_test_1", "status": "complete", "client_reference_id": "alice",
                                  "subscription": subscription()}
    db.users["bob"] = {"id": "bob", "tier": "free"}
    with pytest.raises(HTTPException) as exc:
        await payments.verify_checkout_session("cs_test_1", dict(db.users["bob"]))
    assert exc.value.status_code == 404
    result = await payments.verify_checkout_session("cs_test_1", dict(db.users["alice"]))
    assert result["status"] == "active"
    assert db.users["alice"]["tier"] == "pro"
    with pytest.raises(HTTPException):
        await payments.verify_checkout_session("not-a-session", dict(db.users["alice"]))


@pytest.mark.asyncio
async def test_verify_session_waits_for_an_unfinished_checkout(env):
    db, fake, _ = env
    fake.sessions["cs_test_2"] = {"id": "cs_test_2", "status": "open", "client_reference_id": "alice"}
    result = await payments.verify_checkout_session("cs_test_2", dict(db.users["alice"]))
    assert result["status"] == "pending"
    assert db.users["alice"]["tier"] == "free"


# --- cancel, resume, portal -------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_and_resume_keep_paid_access(env):
    db, fake, _ = env
    db.users["alice"]["stripe_customer_id"] = "cus_1"
    fake.subscriptions["sub_1"] = subscription()
    await deliver("customer.subscription.updated", subscription())
    result = await payments.cancel_subscription(dict(db.users["alice"]))
    assert result["tier"] == "pro"
    assert db.subs["alice"]["cancel_at_period_end"] is True
    status = await payments.get_subscription_status(dict(db.users["alice"]))
    assert status["cancel_at_period_end"] is True and status["renews"] is False
    await payments.resume_subscription(dict(db.users["alice"]))
    assert db.subs["alice"]["cancel_at_period_end"] is False


@pytest.mark.asyncio
async def test_cancel_without_subscription_is_a_clear_400(env):
    db, fake, _ = env
    with pytest.raises(HTTPException) as exc:
        await payments.cancel_subscription(dict(db.users["alice"]))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_billing_portal_needs_a_customer(env):
    db, fake, _ = env
    with pytest.raises(HTTPException) as exc:
        await payments.billing_portal(dict(db.users["alice"]))
    assert exc.value.status_code == 400
    db.users["alice"]["stripe_customer_id"] = "cus_1"
    result = await payments.billing_portal(dict(db.users["alice"]))
    assert result["url"].startswith("https://billing.stripe.test/")


def test_no_paystack_code_is_left():
    for path in ("app/routes/payments.py", "app/config.py", "app/billing.py", "app/database.py"):
        assert "paystack" not in open(path, encoding="utf-8").read().lower(), path
