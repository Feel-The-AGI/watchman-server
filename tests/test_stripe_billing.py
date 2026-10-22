"""Stripe checkout, webhooks, founding cap, and the untouched Paystack routes."""

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.middleware.auth import get_current_user, get_effective_tier
from app.routes import billing, payments
from app.stripe_billing import (
    ACCESS_GRACE,
    access_for,
    is_stale,
    seats_left,
    subscription_period_end,
)
from tests.stripe_memory import MemoryBillingDB

USER_ID = "11111111-1111-1111-1111-111111111111"
SECRET = "whsec_test_secret"
PRICE_MONTH = "price_month"
PRICE_YEAR = "price_year"
PRICE_FOUNDING = "price_founding"


def settings(**overrides):
    base = dict(
        stripe_secret_key="sk_test_watchman",
        stripe_webhook_secret=SECRET,
        stripe_price_monthly=PRICE_MONTH,
        stripe_price_yearly=PRICE_YEAR,
        stripe_price_founding=PRICE_FOUNDING,
        stripe_api_base="",
        client_base_url="https://trywatchman.app",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeStripe:
    def __init__(self, api_key, api_base=""):
        self.api_key = api_key
        self.api_base = api_base
        self.calls = []
        self.subscription = {
            "id": "sub_retrieved",
            "object": "subscription",
            "customer": "cus_test",
            "status": "active",
            "current_period_end": int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp()),
            "cancel_at_period_end": False,
            "metadata": {"plan": "monthly", "watchman_user_id": USER_ID},
            "items": {"data": [{"price": {"id": PRICE_MONTH}}]},
        }

    async def create_customer(self, params):
        self.calls.append(("customer", params))
        return {"id": "cus_test", "email": params.get("email")}

    async def create_checkout_session(self, params):
        self.calls.append(("checkout", params))
        return {"id": "cs_test", "url": "https://checkout.stripe.com/c/pay/cs_test"}

    async def create_portal_configuration(self, params):
        self.calls.append(("portal_config", params))
        return {"id": "bpc_test"}

    async def create_portal_session(self, params):
        self.calls.append(("portal", params))
        return {"id": "bps_test", "url": "https://billing.stripe.com/p/session/test"}

    async def retrieve_subscription(self, subscription_id):
        self.calls.append(("retrieve", subscription_id))
        payload = dict(self.subscription)
        payload["id"] = subscription_id
        return payload


def user_row(**extra):
    row = {
        "id": USER_ID,
        "email": "ada@example.com",
        "name": "Ada",
        "tier": "free",
        "role": "user",
    }
    row.update(extra)
    return row


def sign(payload: bytes, secret: str = SECRET) -> str:
    timestamp = int(time.time())
    signed = f"{timestamp}.{payload.decode()}"
    digest = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def event(event_id, event_type, obj):
    return {"id": event_id, "object": "event", "type": event_type, "data": {"object": obj}}


def subscription(period_end, status="active", plan="monthly", cancel=False, price=PRICE_MONTH, sub_id="sub_1"):
    return {
        "id": sub_id,
        "object": "subscription",
        "customer": "cus_test",
        "status": status,
        "current_period_end": int(period_end.timestamp()),
        "cancel_at_period_end": cancel,
        "metadata": {"plan": plan, "watchman_user_id": USER_ID},
        "items": {"data": [{"price": {"id": price}, "current_period_end": int(period_end.timestamp())}]},
    }


def invoice(period_end, price=PRICE_MONTH, plan="monthly"):
    return {
        "id": "in_1",
        "object": "invoice",
        "customer": "cus_test",
        "subscription": "sub_1",
        "status": "paid",
        "metadata": {"plan": plan, "watchman_user_id": USER_ID},
        "lines": {"data": [{
            "period": {"end": int(period_end.timestamp())},
            "price": {"id": price},
        }]},
    }


@pytest.fixture
def world(monkeypatch):
    memory = MemoryBillingDB()
    memory.users[USER_ID] = user_row()
    fake = FakeStripe("sk_test_watchman")
    billing._portal_config_ids.clear()
    monkeypatch.setattr(billing, "get_settings", lambda: settings())
    monkeypatch.setattr(billing, "Database", lambda use_admin=False: memory)
    monkeypatch.setattr(billing, "StripeBillingClient", lambda *args, **kwargs: fake)
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user_row()
    client = TestClient(app)
    return SimpleNamespace(memory=memory, fake=fake, client=client, app=app)


def post_event(world, body: dict, secret: str = SECRET):
    raw = json.dumps(body, separators=(",", ":")).encode()
    return world.client.post(
        "/api/billing/stripe-webhook",
        content=raw,
        headers={"Stripe-Signature": sign(raw, secret), "Content-Type": "application/json"},
    )


def test_paystack_routes_stay():
    paths = {route.path for route in payments.router.routes}
    assert "/create-checkout-session" in paths
    assert "/webhook" in paths
    billing_paths = {route.path for route in billing.router.routes}
    assert "/checkout" in billing_paths
    assert "/portal" in billing_paths
    assert "/status" in billing_paths
    assert "/founding" in billing_paths
    assert "/stripe-webhook" in billing_paths


def test_missing_keys_are_503_and_do_not_call_stripe(world, monkeypatch):
    monkeypatch.setattr(billing, "get_settings", lambda: settings(
        stripe_secret_key="", stripe_webhook_secret=""
    ))
    assert world.client.post("/api/billing/checkout", json={"plan": "monthly"}).status_code == 503
    assert world.client.post("/api/billing/portal").status_code == 503
    assert world.client.get("/api/billing/status").status_code == 503
    assert world.client.get("/api/billing/founding").status_code == 503
    webhook = world.client.post("/api/billing/stripe-webhook", content=b"{}")
    assert webhook.status_code == 503
    assert webhook.json()["detail"] == "Payments not configured"
    assert world.fake.calls == []


def test_missing_storage_is_503(world):
    world.memory.ready = False
    response = world.client.post("/api/billing/checkout", json={"plan": "monthly"})
    assert response.status_code == 503
    assert response.json()["detail"] == "Payments not configured"
    assert world.fake.calls == []


def test_checkout_requires_auth_and_rejects_an_unknown_plan(world):
    bare = TestClient(create_app())
    assert bare.post("/api/billing/checkout", json={"plan": "monthly"}).status_code == 401
    assert world.client.post("/api/billing/checkout", json={"plan": "weekly"}).status_code == 400
    assert world.client.post("/api/billing/checkout", content=b"not-json").status_code == 400


def test_checkout_creates_a_card_subscription_and_reuses_the_customer(world):
    first = world.client.post("/api/billing/checkout", json={"plan": "monthly"})
    assert first.status_code == 200
    assert first.json()["url"].startswith("https://checkout.stripe.com/")
    checkout = world.fake.calls[1][1]
    assert world.fake.calls[0][0] == "customer"
    assert checkout["mode"] == "subscription"
    assert checkout["client_reference_id"] == USER_ID
    assert checkout["line_items"] == [{"price": PRICE_MONTH, "quantity": 1}]
    assert checkout["allowed_payment_method_types"] == ["card"]
    assert checkout["success_url"] == "https://trywatchman.app/settings?billing=success"
    assert checkout["cancel_url"] == "https://trywatchman.app/settings?billing=cancel"
    assert "trial_period_days" not in checkout
    assert checkout["subscription_data"]["metadata"]["plan"] == "monthly"
    assert world.memory.users[USER_ID]["stripe_customer_id"] == "cus_test"

    second = world.client.post("/api/billing/checkout", json={"plan": "yearly"})
    assert second.status_code == 200
    customers = [call for call in world.fake.calls if call[0] == "customer"]
    assert len(customers) == 1
    assert world.fake.calls[-1][1]["line_items"][0]["price"] == PRICE_YEAR
    assert world.fake.calls[-1][1]["customer"] == "cus_test"


def test_portal_cancels_at_period_end_and_blocks_price_changes(world):
    world.memory.users[USER_ID]["stripe_customer_id"] = "cus_test"
    first = world.client.post("/api/billing/portal")
    assert first.status_code == 200
    assert first.json()["url"].startswith("https://billing.stripe.com/")
    config = world.fake.calls[0][1]["features"]
    assert config["subscription_cancel"] == {"enabled": True, "mode": "at_period_end"}
    assert config["subscription_update"]["enabled"] is False
    portal = world.fake.calls[1][1]
    assert portal["customer"] == "cus_test"
    assert portal["return_url"] == "https://trywatchman.app/settings"
    assert portal["configuration"] == "bpc_test"
    world.client.post("/api/billing/portal")
    assert [call[0] for call in world.fake.calls].count("portal_config") == 1


def test_portal_without_a_customer_is_400(world):
    response = world.client.post("/api/billing/portal")
    assert response.status_code == 400
    assert response.json()["detail"] == "No billing account yet"


def test_founding_checkout_stops_at_100(world):
    for index in range(100):
        world.memory.seats[f"other-{index}"] = {"status": "active"}
    blocked = world.client.post("/api/billing/checkout", json={"plan": "founding"})
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "Founding 100 is full"
    assert world.fake.calls == []
    public = world.client.get("/api/billing/founding")
    assert public.status_code == 200
    assert public.json() == {"founding_seats_left": 0, "limit": 100}

    world.memory.seats[USER_ID] = {"status": "canceled"}
    allowed = world.client.post("/api/billing/checkout", json={"plan": "founding"})
    assert allowed.status_code == 200
    assert world.fake.calls[-1][1]["line_items"][0]["price"] == PRICE_FOUNDING


def test_invoice_webhook_grants_pro_replay_is_a_noop_and_delete_returns_free(world):
    period_end = datetime.now(timezone.utc) + timedelta(days=30)
    paid = event("evt_paid", "invoice.paid", invoice(period_end))
    first = post_event(world, paid)
    assert first.status_code == 200
    assert first.json() == {"received": True}
    stored = world.memory.users[USER_ID]
    assert stored["tier"] == "pro"
    assert get_effective_tier(stored) == "pro"
    expires = stored["pro_expires_at"]
    assert world.memory.subs[USER_ID]["plan"] == "monthly"
    assert world.memory.subs[USER_ID]["cancel_at_period_end"] is False

    status = world.client.get("/api/billing/status")
    assert status.status_code == 200
    body = status.json()
    assert body["plan"] == "monthly"
    assert body["status"] == "active"
    assert body["cancel_at_period_end"] is False
    assert body["tier"] == "pro"
    assert body["founding_seats_left"] == 100
    assert body["current_period_end"]

    replay = post_event(world, paid)
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert world.memory.users[USER_ID]["pro_expires_at"] == expires
    assert len(world.memory.events) == 1

    cancel_end = period_end
    updated = event("evt_cancel", "customer.subscription.updated", subscription(
        cancel_end, status="active", cancel=True
    ))
    cancel = post_event(world, updated)
    assert cancel.status_code == 200
    assert world.memory.users[USER_ID]["tier"] == "pro"
    assert get_effective_tier(world.memory.users[USER_ID]) == "pro"
    assert world.memory.subs[USER_ID]["cancel_at_period_end"] is True
    after_cancel = world.client.get("/api/billing/status").json()
    assert after_cancel["cancel_at_period_end"] is True
    assert after_cancel["tier"] == "pro"

    deleted = event("evt_deleted", "customer.subscription.deleted", subscription(
        datetime.now(timezone.utc) - timedelta(days=1),
        status="canceled",
    ))
    gone = post_event(world, deleted)
    assert gone.status_code == 200
    assert world.memory.users[USER_ID]["tier"] == "free"
    assert get_effective_tier(world.memory.users[USER_ID]) == "free"
    final = world.client.get("/api/billing/status").json()
    assert final["tier"] == "free"
    assert final["status"] == "canceled"


def test_bad_signature_is_400_even_when_storage_is_down(world):
    world.memory.ready = False
    raw = b'{"id":"evt_bad","type":"invoice.paid","data":{"object":{}}}'
    bad = world.client.post(
        "/api/billing/stripe-webhook",
        content=raw,
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
    )
    assert bad.status_code == 400
    assert bad.json()["detail"] == "Invalid signature"
    assert world.memory.events == {}
    missing = world.client.post("/api/billing/stripe-webhook", content=raw)
    assert missing.status_code == 400


def test_founding_invoice_takes_one_seat_and_a_replay_does_not_take_another(world):
    period_end = datetime.now(timezone.utc) + timedelta(days=365)
    body = event("evt_founding", "invoice.paid", invoice(period_end, PRICE_FOUNDING, "founding"))
    assert post_event(world, body).status_code == 200
    assert world.memory.users[USER_ID]["tier"] == "pro"
    assert USER_ID in world.memory.seats
    assert world.client.get("/api/billing/founding").json()["founding_seats_left"] == 99
    assert post_event(world, body).json()["duplicate"] is True
    assert len(world.memory.seats) == 1

    deleted = event("evt_founding_end", "customer.subscription.deleted", subscription(
        datetime.now(timezone.utc) - timedelta(hours=2),
        status="canceled",
        plan="founding",
        price=PRICE_FOUNDING,
    ))
    assert post_event(world, deleted).status_code == 200
    assert world.memory.users[USER_ID]["tier"] == "free"
    assert USER_ID in world.memory.seats
    assert world.client.get("/api/billing/founding").json()["founding_seats_left"] == 99


def test_checkout_completed_retrieves_the_subscription(world):
    body = event("evt_checkout", "checkout.session.completed", {
        "id": "cs_test",
        "object": "checkout.session",
        "mode": "subscription",
        "customer": "cus_test",
        "subscription": "sub_live",
        "client_reference_id": USER_ID,
        "metadata": {"plan": "monthly", "watchman_user_id": USER_ID},
    })
    assert post_event(world, body).status_code == 200
    assert ("retrieve", "sub_live") in world.fake.calls
    assert world.memory.users[USER_ID]["tier"] == "pro"
    assert world.memory.users[USER_ID]["stripe_customer_id"] == "cus_test"


def test_item_period_end_is_used_when_the_subscription_has_none():
    period_end = datetime(2027, 1, 1, tzinfo=timezone.utc)
    stamp = int(period_end.timestamp())
    found = subscription_period_end({
        "items": {"data": [{"current_period_end": stamp}]},
    })
    assert found == datetime.fromtimestamp(stamp, tz=timezone.utc)


def test_access_rules_keep_unpaid_until_period_end_and_skip_trials():
    future = datetime.now(timezone.utc) + timedelta(days=10)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    tier, until = access_for("active", future)
    assert tier == "pro"
    assert until - future == ACCESS_GRACE
    assert access_for("unpaid", future)[0] == "pro"
    assert access_for("unpaid", past)[0] == "free"
    assert access_for("trialing", future)[0] == "free"
    assert access_for("canceled", past)[0] == "free"
    assert access_for("canceled", future)[0] == "pro"
    assert seats_left(0) == 100
    assert seats_left(100) == 0
    assert is_stale(
        {"stripe_status": "canceled", "current_period_end": future.isoformat(), "stripe_subscription_id": "sub_1"},
        "sub_1",
        future,
        "active",
    )


def test_a_longer_paystack_period_survives_a_shorter_stripe_subscription(world):
    """Paystack remains live. Stripe must not cut off a period it did not grant."""
    longer = datetime.now(timezone.utc) + timedelta(days=60)
    world.memory.users[USER_ID]["tier"] = "pro"
    world.memory.users[USER_ID]["pro_expires_at"] = longer.isoformat()
    shorter = datetime.now(timezone.utc) + timedelta(days=30)
    paid = event("evt_overlap", "invoice.paid", invoice(shorter))
    assert post_event(world, paid).status_code == 200
    assert world.memory.users[USER_ID]["tier"] == "pro"
    assert world.memory.users[USER_ID]["pro_expires_at"] == longer.isoformat()

    deleted = event("evt_overlap_end", "customer.subscription.deleted", subscription(
        datetime.now(timezone.utc) - timedelta(days=1), status="canceled"
    ))
    assert post_event(world, deleted).status_code == 200
    assert world.memory.users[USER_ID]["tier"] == "pro"
    assert get_effective_tier(world.memory.users[USER_ID]) == "pro"
    assert world.memory.users[USER_ID]["pro_expires_at"] == longer.isoformat()


def test_a_later_invoice_does_not_resurrect_a_canceled_subscription(world):
    future = datetime.now(timezone.utc) + timedelta(days=20)
    deleted = event("evt_del", "customer.subscription.deleted", subscription(
        datetime.now(timezone.utc) - timedelta(minutes=5), status="canceled"
    ))
    # Establish the subscription id first so the delete matches it.
    post_event(world, event("evt_open", "customer.subscription.updated", subscription(future)))
    assert post_event(world, deleted).status_code == 200
    assert world.memory.users[USER_ID]["tier"] == "free"
    stale_invoice = event("evt_old_invoice", "invoice.paid", invoice(
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ))
    assert post_event(world, stale_invoice).status_code == 200
    assert world.memory.users[USER_ID]["tier"] == "free"
