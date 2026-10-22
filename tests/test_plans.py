"""Fixed GHS plans. The client does not choose the amount."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app.billing import (
    PLANS,
    charge_matches,
    checkout_plan,
    extend_expiry,
    period_days_for_payment,
    to_pesewas,
)
from app.database import Database
from app.middleware.auth import get_current_user
from app.routes import payments
from tests.test_entitlements import MemoryDB


def _checkout_ready(monkeypatch, reference="ref_1"):
    monkeypatch.setattr(payments.settings, "paystack_secret_key", "sk_test_example")
    monkeypatch.setattr(payments, "paystack_request", AsyncMock(return_value={"data": {
        "authorization_url": "https://paystack.test/pay",
        "access_code": "ac",
        "reference": reference,
    }}))
    db = MemoryDB()
    monkeypatch.setattr(payments, "Database", lambda **kwargs: db)
    monkeypatch.setattr(payments, "get_email_service", lambda: SimpleNamespace(
        send_pro_upgrade_email=AsyncMock(),
        send_admin_new_subscriber_notification=AsyncMock(),
    ))
    return db


def test_each_plan_has_a_fixed_ghs_amount_and_day_count():
    assert [(plan.id, plan.amount_ghs, plan.display_usd, plan.days, plan.offered) for plan in PLANS.values()] == [
        ("monthly", payments.PLANS["monthly"].amount_ghs, payments.PLANS["monthly"].display_usd, 30, True),
        ("yearly", payments.PLANS["yearly"].amount_ghs, payments.PLANS["yearly"].display_usd, 365, True),
        ("founding_yearly", payments.PLANS["founding_yearly"].amount_ghs, payments.PLANS["founding_yearly"].display_usd, 365, False),
    ]
    assert PLANS["monthly"].amount_ghs == payments.PLANS["monthly"].amount_ghs
    assert str(PLANS["monthly"].amount_ghs) == "70.00"
    assert str(PLANS["yearly"].amount_ghs) == "560.00"
    assert str(PLANS["founding_yearly"].amount_ghs) == "420.00"
    assert int(PLANS["monthly"].display_usd) == 6
    assert int(PLANS["yearly"].display_usd) == 48
    assert int(PLANS["founding_yearly"].display_usd) == 36
    assert to_pesewas(PLANS["monthly"].amount_ghs) == 7000
    assert to_pesewas(PLANS["yearly"].amount_ghs) == 56000
    assert to_pesewas(PLANS["founding_yearly"].amount_ghs) == 42000
    assert checkout_plan(None).id == "monthly"
    assert checkout_plan("yearly").days == 365
    with pytest.raises(payments.PlanNotOffered):
        checkout_plan("founding_yearly")


def test_pricing_offers_monthly_and_yearly_only():
    body = payments._pricing_body(claimed=100)
    assert body["founding_available"] is False
    assert body["founding_seats_left"] == 0
    assert body["seats_left"] == 0
    assert [plan["id"] for plan in body["plans"]] == ["monthly", "yearly"]
    assert body["plans"][0]["amount_ghs"] == 70
    assert body["plans"][0]["display_usd"] == 6
    assert body["plans"][0]["days"] == 30
    assert body["plans"][1]["amount_ghs"] == 560
    assert body["plans"][1]["display_usd"] == 48
    assert body["plans"][1]["days"] == 365
    assert body["ghs_amount"] == 70
    assert body["usd_price"] == 6
    assert "exchange_rate" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_id,ghs,pesewas,days,display", [
    ("monthly", 70, 7000, 30, 6),
    ("yearly", 560, 56000, 365, 48),
])
async def test_checkout_charges_the_plan_and_ignores_a_client_amount(monkeypatch, plan_id, ghs, pesewas, days, display):
    db = _checkout_ready(monkeypatch, reference=f"ref_{plan_id}")
    result = await payments.create_checkout_session(
        {"id": "alice", "email": "alice@example.com"},
        payments.CheckoutRequest(plan=plan_id, amount=1, amount_ghs=1),
    )
    assert result["plan"] == plan_id
    assert result["amount_ghs"] == ghs
    assert result["display_usd"] == display
    assert result["period_days"] == days
    sent = payments.paystack_request.await_args.args[2]
    assert sent["amount"] == pesewas
    assert sent["currency"] == "GHS"
    assert "plan" not in sent
    stored = db.payments[f"ref_{plan_id}"]
    assert stored["amount_local"] == ghs
    assert stored["plan"] == plan_id
    assert stored["amount"] == display
    assert stored["description"].startswith(f"plan={plan_id}")


@pytest.mark.asyncio
async def test_founding_checkout_is_refused_when_the_cap_is_full(monkeypatch):
    db = _checkout_ready(monkeypatch)
    db.founding_claimed = 100
    with pytest.raises(HTTPException) as exc:
        await payments.create_checkout_session(
            {"id": "alice", "email": "alice@example.com"},
            payments.CheckoutRequest(plan="founding_yearly", amount=420),
        )
    assert exc.value.status_code == 400
    payments.paystack_request.assert_not_awaited()


def test_http_checkout_without_a_body_stays_monthly(monkeypatch):
    db = _checkout_ready(monkeypatch, reference="ref_http")
    app = FastAPI()
    app.include_router(payments.router, prefix="/api/payments")
    app.dependency_overrides[get_current_user] = lambda: {"id": "alice", "email": "alice@example.com"}
    response = TestClient(app).post("/api/payments/create-checkout-session")
    assert response.status_code == 200
    assert response.json()["plan"] == "monthly"
    assert response.json()["amount_ghs"] == 70
    assert db.payments["ref_http"]["amount_local"] == 70
    tampered = TestClient(app).post(
        "/api/payments/create-checkout-session",
        json={"plan": "yearly", "amount": 1, "amount_ghs": "1"},
    )
    assert tampered.status_code == 200
    assert tampered.json()["amount_ghs"] == 560
    assert payments.paystack_request.await_args.args[2]["amount"] == 56000


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_id,ghs,pesewas,days", [
    ("monthly", 70, 7000, 30),
    ("yearly", 560, 56000, 365),
    ("founding_yearly", 420, 42000, 365),
])
async def test_webhook_matches_the_stored_amount_for_each_plan(monkeypatch, plan_id, ghs, pesewas, days):
    db = MemoryDB()
    db.users["alice"] = {"id": "alice", "email": "alice@example.com", "name": "Alice", "tier": "free"}
    db.payments["ref_1"] = {
        "user_id": "alice",
        "status": "pending",
        "plan": plan_id,
        "amount_local": ghs,
        "amount": float(PLANS[plan_id].display_usd),
        "description": f"plan={plan_id}; Watchman Pro",
    }
    monkeypatch.setattr(payments, "get_email_service", lambda: SimpleNamespace(
        send_pro_upgrade_email=AsyncMock(),
        send_admin_new_subscriber_notification=AsyncMock(),
    ))
    assert charge_matches(ghs, pesewas, "GHS")
    assert not charge_matches(ghs, pesewas + 1, "GHS")
    before = datetime.now(timezone.utc)
    short = await payments.apply_successful_charge(db, "ref_1", {
        "status": "success", "amount": pesewas + 1, "currency": "GHS", "customer": {},
    })
    assert short["status"] == "ignored"
    assert db.users["alice"]["tier"] == "free"
    db.payments["ref_1"]["status"] = "pending"
    granted = await payments.apply_successful_charge(db, "ref_1", {
        "status": "success",
        "amount": pesewas,
        "currency": "GHS",
        "customer": {"email": "alice@example.com"},
    })
    assert granted["status"] == "success"
    expiry = datetime.fromisoformat(db.users["alice"]["pro_expires_at"])
    assert timedelta(days=days) - timedelta(minutes=1) < expiry - before < timedelta(days=days, minutes=1)


@pytest.mark.asyncio
async def test_a_second_period_stacks_on_the_current_expiry(monkeypatch):
    db = MemoryDB()
    current = datetime.now(timezone.utc) + timedelta(days=10)
    db.users["alice"] = {
        "id": "alice",
        "email": "alice@example.com",
        "tier": "pro",
        "pro_expires_at": current.isoformat(),
    }
    db.payments["ref_1"] = {
        "user_id": "alice",
        "status": "pending",
        "plan": "yearly",
        "amount_local": 560,
        "amount": 48,
    }
    monkeypatch.setattr(payments, "get_email_service", lambda: SimpleNamespace(
        send_pro_upgrade_email=AsyncMock(),
        send_admin_new_subscriber_notification=AsyncMock(),
    ))
    await payments.apply_successful_charge(db, "ref_1", {
        "status": "success", "amount": 56000, "currency": "GHS", "customer": {},
    })
    expiry = datetime.fromisoformat(db.users["alice"]["pro_expires_at"])
    assert abs((expiry - current) - timedelta(days=365)) < timedelta(minutes=1)
    assert extend_expiry(current, datetime.now(timezone.utc), days=365) - current == timedelta(days=365)


@pytest.mark.asyncio
async def test_legacy_pending_charge_of_139_86_still_grants_thirty_days(monkeypatch):
    db = MemoryDB()
    db.users["alice"] = {"id": "alice", "email": "alice@example.com", "tier": "free"}
    db.payments["ref_old"] = {
        "user_id": "alice",
        "status": "pending",
        "amount_local": 139.86,
        "amount": 12,
        "description": "Watchman Pro - $12 for 30 days",
    }
    monkeypatch.setattr(payments, "get_email_service", lambda: SimpleNamespace(
        send_pro_upgrade_email=AsyncMock(),
        send_admin_new_subscriber_notification=AsyncMock(),
    ))
    assert charge_matches(139.86, 13986, "GHS")
    assert period_days_for_payment(db.payments["ref_old"]) == 30
    before = datetime.now(timezone.utc)
    result = await payments.apply_successful_charge(db, "ref_old", {
        "status": "success",
        "amount": 13986,
        "currency": "GHS",
        "customer": {"email": "alice@example.com"},
    })
    assert result["status"] == "success"
    expiry = datetime.fromisoformat(db.users["alice"]["pro_expires_at"])
    assert timedelta(days=29, hours=23) < expiry - before < timedelta(days=30, minutes=1)


@pytest.mark.asyncio
async def test_missing_plan_column_still_stores_the_checkout(monkeypatch):
    class Insert:
        def __init__(self, client, data):
            self.client = client
            self.data = data

        def execute(self):
            self.client.attempts.append(dict(self.data))
            if "plan" in self.data:
                raise APIError({
                    "message": "Could not find the 'plan' column of 'payments' in the schema cache",
                    "code": "PGRST204",
                    "hint": None,
                    "details": None,
                })
            return SimpleNamespace(data=[dict(self.data)])

    class Client:
        def __init__(self):
            self.attempts = []

        def table(self, name):
            assert name == "payments"
            return self

        def insert(self, data):
            return Insert(self, data)

    client = Client()
    db = Database.__new__(Database)
    db.client = client
    saved = await db.create_payment_record({
        "user_id": "alice",
        "paystack_reference": "ref_1",
        "plan": "yearly",
        "amount": 48,
        "amount_local": 560,
        "description": "plan=yearly; Watchman Pro yearly for 365 days",
        "status": "pending",
    })
    assert [list(row) for row in client.attempts] == [
        ["user_id", "paystack_reference", "plan", "amount", "amount_local", "description", "status"],
        ["user_id", "paystack_reference", "amount", "amount_local", "description", "status"],
    ]
    assert "plan" not in saved
    assert period_days_for_payment(saved) == 365


@pytest.mark.asyncio
async def test_prepaid_copy_names_the_plan_and_does_not_promise_renewal(monkeypatch):
    from app.services.email_service import EmailService

    service = EmailService()
    captured = {}

    async def capture(to, subject, html, text=None):
        captured["html"] = html
        return True

    service.send_email = capture
    ends = datetime(2027, 1, 22, tzinfo=timezone.utc)
    await service.send_pro_upgrade_email(
        "ada@example.com", "Ada", plan_name="yearly", ends_on=ends,
    )
    html = captured["html"]
    assert "Yearly, through 22 Jan 2027." in html
    for feature in (
        "Unlimited rotations and commitments",
        "Planning beyond 3 months",
        "Leave planning",
        "Unlimited chat and full history",
        "Calendar sharing",
        "CSV and PDF exports",
    ):
        assert feature in html
    assert "Weighted Constraints" not in html
    assert "Priority Support" not in html
    assert "Manage your subscription" not in html

    user = {
        "id": "alice",
        "tier": "pro",
        "pro_expires_at": ends.isoformat(),
        "paystack_subscription_code": None,
    }
    db = MemoryDB()
    db.users["alice"] = dict(user)
    monkeypatch.setattr(payments, "Database", lambda **kwargs: db)
    cancelled = await payments.cancel_subscription(user)
    assert cancelled["message"] == "Pro is prepaid and ends on 22 Jan 2027. Nothing will be charged again."
    assert "renew" not in cancelled["message"].lower()
    managed = await payments.get_manage_subscription_link(user)
    assert managed["message"] == cancelled["message"]
    from app.middleware.auth import require_pro_tier
    with pytest.raises(HTTPException) as exc:
        await require_pro_tier({"id": "free", "tier": "free"})
    assert exc.value.detail == "This is a Pro feature. Upgrade to Pro to use it."
