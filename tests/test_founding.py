"""Founding yearly: 100 seats, then the locked price only for people who have it."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from loguru import logger

from app.billing import FOUNDING_CAP, resolve_checkout_plan
from app.middleware.auth import get_current_user, get_optional_user
from app.routes import payments
from tests.test_entitlements import MemoryDB


def _ready(monkeypatch, reference="ref_founding"):
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


def test_cap_boundary_is_99_open_and_100_closed():
    assert FOUNDING_CAP == 100
    open_plan = resolve_checkout_plan("founding_yearly", founding_member=False, claimed=99)
    assert open_plan.id == "founding_yearly"
    assert str(open_plan.amount_ghs) == "420.00"
    with pytest.raises(payments.PlanNotOffered):
        resolve_checkout_plan("founding_yearly", founding_member=False, claimed=100)
    locked = resolve_checkout_plan("yearly", founding_member=True, claimed=100)
    assert locked.id == "founding_yearly"
    assert str(locked.amount_ghs) == "420.00"
    ordinary = resolve_checkout_plan("yearly", founding_member=False, claimed=100)
    assert ordinary.id == "yearly"
    assert str(ordinary.amount_ghs) == "560.00"


def test_price_endpoint_shows_seats_and_the_locked_price():
    at_99 = payments._pricing_body(claimed=99, founding_member=False)
    assert at_99["founding_available"] is True
    assert at_99["seats_left"] == 1
    assert any(plan["id"] == "founding_yearly" and plan["amount_ghs"] == 420 for plan in at_99["plans"])

    at_100 = payments._pricing_body(claimed=100, founding_member=False)
    assert at_100["founding_available"] is False
    assert at_100["seats_left"] == 0
    assert at_100["yearly_amount_ghs"] == 560
    assert "founding_yearly" not in {plan["id"] for plan in at_100["plans"]}

    member = payments._pricing_body(claimed=100, founding_member=True)
    assert member["founding_member"] is True
    assert member["yearly_amount_ghs"] == 420
    yearly = next(plan for plan in member["plans"] if plan["id"] == "yearly")
    assert yearly["amount_ghs"] == 420
    assert yearly["display_usd"] == 36


@pytest.mark.asyncio
async def test_seat_99_can_start_founding_and_seat_100_cannot(monkeypatch):
    db = _ready(monkeypatch, reference="ref_99")
    db.founding_claimed = 99
    allowed = await payments.create_checkout_session(
        {"id": "alice", "email": "alice@example.com"},
        payments.CheckoutRequest(plan="founding_yearly", amount=1),
    )
    assert allowed["amount_ghs"] == 420
    assert allowed["plan"] == "founding_yearly"
    assert payments.paystack_request.await_args.args[2]["amount"] == 42000
    assert db.payments["ref_99"]["amount_local"] == 420

    db.founding_claimed = 100
    with pytest.raises(HTTPException) as exc:
        await payments.create_checkout_session(
            {"id": "bob", "email": "bob@example.com"},
            payments.CheckoutRequest(plan="founding_yearly"),
        )
    assert exc.value.status_code == 400
    yearly = await payments.create_checkout_session(
        {"id": "bob", "email": "bob@example.com"},
        payments.CheckoutRequest(plan="yearly"),
    )
    assert yearly["plan"] == "yearly"
    assert yearly["amount_ghs"] == 560


@pytest.mark.asyncio
async def test_a_flagged_user_pays_420_after_the_cap(monkeypatch):
    db = _ready(monkeypatch, reference="ref_locked")
    db.founding_claimed = 100
    result = await payments.create_checkout_session(
        {"id": "alice", "email": "alice@example.com", "founding_member": True},
        payments.CheckoutRequest(plan="yearly", amount=560),
    )
    assert result["plan"] == "founding_yearly"
    assert result["amount_ghs"] == 420
    assert result["display_usd"] == 36
    assert payments.paystack_request.await_args.args[2]["amount"] == 42000
    with pytest.raises(HTTPException) as exc:
        await payments.create_checkout_session(
            {"id": "bob", "email": "bob@example.com"},
            payments.CheckoutRequest(plan="founding_yearly"),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_successful_founding_payment_sets_the_flag(monkeypatch):
    db = MemoryDB()
    db.users["alice"] = {"id": "alice", "email": "alice@example.com", "tier": "free"}
    db.payments["ref_1"] = {
        "user_id": "alice",
        "paystack_reference": "ref_1",
        "status": "pending",
        "plan": "founding_yearly",
        "amount_local": 420,
        "amount": 36,
        "description": "plan=founding_yearly; Watchman Pro",
    }
    monkeypatch.setattr(payments, "get_email_service", lambda: SimpleNamespace(
        send_pro_upgrade_email=AsyncMock(),
        send_admin_new_subscriber_notification=AsyncMock(),
    ))
    result = await payments.apply_successful_charge(db, "ref_1", {
        "status": "success",
        "amount": 42000,
        "currency": "GHS",
        "customer": {"email": "alice@example.com"},
    })
    assert result["status"] == "success"
    assert db.users["alice"]["tier"] == "pro"
    assert db.users["alice"]["founding_member"] is True
    assert db.founding_claimed == 1
    expiry = datetime.fromisoformat(db.users["alice"]["pro_expires_at"])
    assert expiry - datetime.now(timezone.utc) > timedelta(days=364)


@pytest.mark.asyncio
async def test_the_101st_founding_payment_still_grants_pro(monkeypatch):
    db = MemoryDB()
    db.force_over_cap = True
    db.founding_claimed = 100
    db.users["alice"] = {"id": "alice", "email": "alice@example.com", "tier": "free"}
    db.payments["ref_1"] = {
        "user_id": "alice",
        "paystack_reference": "ref_1",
        "status": "pending",
        "plan": "founding_yearly",
        "amount_local": 420,
        "amount": 36,
    }
    monkeypatch.setattr(payments, "get_email_service", lambda: SimpleNamespace(
        send_pro_upgrade_email=AsyncMock(),
        send_admin_new_subscriber_notification=AsyncMock(),
    ))
    lines = []
    handle = logger.add(lambda message: lines.append(str(message)), level="ERROR")
    try:
        result = await payments.apply_successful_charge(db, "ref_1", {
            "status": "success",
            "amount": 42000,
            "currency": "GHS",
            "customer": {"email": "alice@example.com"},
        })
    finally:
        logger.remove(handle)
    assert result["status"] == "success"
    assert db.payments["ref_1"]["status"] == "paid"
    assert db.users["alice"]["tier"] == "pro"
    assert db.users["alice"]["founding_member"] is True
    assert db.founding_claimed == 100
    assert any("Founding cap exceeded" in line for line in lines)


def test_pricing_http_reports_seats(monkeypatch):
    db = MemoryDB()
    db.founding_claimed = 99
    monkeypatch.setattr(payments, "Database", lambda **kwargs: db)
    app = FastAPI()
    app.include_router(payments.router, prefix="/api/payments")
    response = TestClient(app).get("/api/payments/pricing")
    assert response.status_code == 200
    body = response.json()
    assert body["seats_left"] == 1
    assert body["founding_available"] is True
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "alice", "founding_member": True,
    }
    app.dependency_overrides[get_optional_user] = lambda: {
        "id": "alice", "founding_member": True,
    }
    db.founding_claimed = 100
    locked = TestClient(app).get("/api/payments/pricing")
    assert locked.status_code == 200
    assert locked.json()["yearly_amount_ghs"] == 420
    assert locked.json()["seats_left"] == 0
