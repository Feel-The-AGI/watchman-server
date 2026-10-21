"""Entitlements: paid periods, checkout pricing, and Pro-only exports."""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.billing import (
    PRO_PERIOD_DAYS,
    access_expired,
    charge_matches,
    extend_expiry,
    ghs_pesewas,
    monthly_recurring_revenue,
    paying_user_count,
)
from app.middleware.auth import get_current_user, get_effective_tier, require_pro_tier
from app.routes import chat, daily_logs, incidents, payments
from tests.test_security_regressions import make_request


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def test_charge_conversion_and_match():
    ghs, pesewas = ghs_pesewas(12, 15)
    assert ghs == ghs
    assert float(ghs) == 180
    assert pesewas == 18000
    assert charge_matches(180, 18000, "GHS")
    assert charge_matches("180.00", 18000, "ghs")
    assert not charge_matches(180, 18001, "GHS")
    assert not charge_matches(180, 18000, "USD")
    assert not charge_matches(None, 18000, "GHS")


def test_period_extends_once_from_the_later_boundary():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    first = extend_expiry(None, now)
    assert first - now == timedelta(days=PRO_PERIOD_DAYS)
    second = extend_expiry(first, now)
    assert second - first == timedelta(days=PRO_PERIOD_DAYS)
    # A replay that starts from the same current expiry grants the same end.
    assert extend_expiry(None, now) == first


def test_expiry_leaves_complimentary_pro_and_admins_alone():
    assert not access_expired({"tier": "pro"})
    assert not access_expired({"tier": "admin", "pro_expires_at": "2000-01-01T00:00:00Z"})
    assert access_expired({"tier": "pro", "pro_expires_at": "2000-01-01T00:00:00Z"})
    assert not access_expired({
        "tier": "pro",
        "pro_expires_at": iso(datetime.now(timezone.utc) + timedelta(days=1)),
    })


def test_effective_tier_and_pro_gate():
    future = iso(datetime.now(timezone.utc) + timedelta(days=3))
    past = iso(datetime.now(timezone.utc) - timedelta(days=1))
    assert get_effective_tier({"tier": "pro", "pro_expires_at": future}) == "pro"
    assert get_effective_tier({"tier": "pro"}) == "pro"
    assert get_effective_tier({"tier": "pro", "pro_expires_at": past}) == "free"
    assert get_effective_tier({"tier": "admin"}) == "admin"


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [
    {"id": "pro", "tier": "pro", "pro_expires_at": iso(datetime.now(timezone.utc) + timedelta(days=2))},
    {"id": "comp", "tier": "pro"},
    {"id": "admin", "tier": "admin"},
])
async def test_require_pro_allows_current_access(user):
    assert await require_pro_tier(user) == user


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [
    {"id": "free", "tier": "free"},
    {"id": "lapsed", "tier": "pro", "pro_expires_at": iso(datetime.now(timezone.utc) - timedelta(hours=1))},
    {"id": "trial", "tier": "free", "created_at": datetime.now(timezone.utc).isoformat()},
])
async def test_require_pro_blocks_free_trial_and_lapsed(user):
    with pytest.raises(HTTPException) as exc:
        await require_pro_tier(user)
    assert exc.value.status_code == 403


def test_mrr_excludes_admins_and_lapsed_pro():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    users = [
        {"tier": "pro", "pro_expires_at": iso(now + timedelta(days=1))},
        {"tier": "pro"},  # complimentary, still counted until a paid expiry is recorded
        {"tier": "pro", "pro_expires_at": iso(now - timedelta(days=1))},
        {"tier": "admin"},
        {"tier": "free"},
    ]
    assert paying_user_count(users, now) == 2
    assert monthly_recurring_revenue(users, 12, now) == 24


def test_published_free_chat_allowance():
    """The settings screen offers free users 100 messages a month and 50 history rows."""
    assert chat.FREE_MESSAGE_LIMIT == 100
    assert chat.FREE_HISTORY_LIMIT == 50


class MemoryDB:
    def __init__(self):
        self.users = {}
        self.payments = {}
        self.subs = {}

    async def get_payment_by_reference(self, reference):
        row = self.payments.get(reference)
        return dict(row) if row else None

    async def update_payment_by_reference(self, reference, data, status=None):
        row = self.payments.get(reference)
        if not row or (status is not None and row.get("status") != status):
            return None
        row.update(data)
        return dict(row)

    async def get_user_by_id(self, user_id):
        row = self.users.get(user_id)
        return dict(row) if row else None

    async def update_user(self, user_id, data):
        self.users[user_id].update(data)
        return dict(self.users[user_id])

    async def get_user_by_paystack_customer(self, code):
        for user in self.users.values():
            if user.get("paystack_customer_code") == code:
                return dict(user)
        return None

    async def get_user_by_email(self, email):
        for user in self.users.values():
            if user.get("email") == email:
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
        for user_id, row in self.subs.items():
            if row.get("id") == subscription_id:
                row.update(data)
                return dict(row)
        return None

    async def create_payment_record(self, data):
        self.payments[data["paystack_reference"]] = dict(data)
        return dict(data)


def verified_charge(amount=18000):
    return {
        "status": "success",
        "amount": amount,
        "currency": "GHS",
        "reference": "ref_1",
        "customer": {"email": "alice@example.com", "customer_code": "CUS_1"},
    }


@pytest.mark.asyncio
async def test_verified_checkout_grants_thirty_days_once(monkeypatch):
    db = MemoryDB()
    db.users["alice"] = {"id": "alice", "email": "alice@example.com", "name": "Alice", "tier": "free"}
    db.payments["ref_1"] = {
        "user_id": "alice",
        "paystack_reference": "ref_1",
        "amount": 12,
        "amount_local": 180,
        "status": "pending",
    }
    emails = SimpleNamespace(
        send_pro_upgrade_email=AsyncMock(),
        send_admin_new_subscriber_notification=AsyncMock(),
    )
    monkeypatch.setattr(payments, "get_email_service", lambda: emails)
    before = datetime.now(timezone.utc)
    first = await payments.apply_successful_charge(db, "ref_1", verified_charge())
    expiry = datetime.fromisoformat(db.users["alice"]["pro_expires_at"])
    assert first["status"] == "success"
    assert timedelta(days=29, hours=23) < expiry - before < timedelta(days=30, minutes=1)
    assert db.payments["ref_1"]["status"] == "paid"
    assert db.subs["alice"]["current_period_end"] == db.users["alice"]["pro_expires_at"]
    emails.send_pro_upgrade_email.assert_awaited()

    second = await payments.apply_successful_charge(db, "ref_1", verified_charge())
    assert second["status"] == "success"
    assert db.users["alice"]["pro_expires_at"] == expiry.isoformat()
    assert emails.send_pro_upgrade_email.await_count == 1


@pytest.mark.asyncio
async def test_retry_after_paid_claim_does_not_add_another_period(monkeypatch):
    db = MemoryDB()
    stored = iso(datetime(2026, 11, 20, tzinfo=timezone.utc))
    db.users["alice"] = {"id": "alice", "email": "alice@example.com", "tier": "free"}
    db.payments["ref_1"] = {
        "user_id": "alice",
        "status": "paid",
        "amount_local": 180,
        "access_until": stored,
    }
    monkeypatch.setattr(payments, "get_email_service", lambda: SimpleNamespace(
        send_pro_upgrade_email=AsyncMock(),
        send_admin_new_subscriber_notification=AsyncMock(),
    ))
    await payments.apply_successful_charge(db, "ref_1", verified_charge())
    assert db.users["alice"]["tier"] == "pro"
    assert db.users["alice"]["pro_expires_at"] == stored


@pytest.mark.asyncio
async def test_unknown_or_underpaid_charge_does_not_grant_pro():
    db = MemoryDB()
    db.users["alice"] = {"id": "alice", "tier": "free", "email": "alice@example.com"}
    assert (await payments.apply_successful_charge(db, "missing", verified_charge()))["status"] == "ignored"
    assert db.users["alice"]["tier"] == "free"

    db.payments["ref_1"] = {"user_id": "alice", "status": "pending", "amount_local": 180, "amount": 12}
    result = await payments.apply_successful_charge(db, "ref_1", verified_charge(amount=100))
    assert result["status"] == "ignored"
    assert db.payments["ref_1"]["status"] == "rejected"
    assert db.users["alice"]["tier"] == "free"


@pytest.mark.asyncio
async def test_metadata_user_cannot_redirect_a_stored_checkout(monkeypatch):
    """The signed event may name another user. The stored checkout owner is granted access."""
    db = MemoryDB()
    db.users["alice"] = {"id": "alice", "tier": "free", "email": "alice@example.com", "name": "Alice"}
    db.users["bob"] = {"id": "bob", "tier": "free", "email": "bob@example.com", "name": "Bob"}
    db.payments["ref_1"] = {"user_id": "alice", "status": "pending", "amount_local": 180, "amount": 12}
    monkeypatch.setattr(payments, "Database", lambda **kwargs: db)
    monkeypatch.setattr(payments, "get_email_service", lambda: SimpleNamespace(
        send_pro_upgrade_email=AsyncMock(),
        send_admin_new_subscriber_notification=AsyncMock(),
    ))
    monkeypatch.setattr(payments, "paystack_request", AsyncMock(return_value={"data": verified_charge()}))
    monkeypatch.setattr(payments.settings, "paystack_secret_key", "sk_test_example")
    payload = b'{"event":"charge.success","data":{"reference":"ref_1","metadata":{"watchman_user_id":"bob"}}}'
    signature = hmac.new(b"sk_test_example", payload, hashlib.sha512).hexdigest()
    response = await payments.paystack_webhook(make_request(payload, signature))
    assert response["status"] == "success"
    assert db.users["alice"]["tier"] == "pro"
    assert db.users["bob"]["tier"] == "free"


@pytest.mark.asyncio
async def test_checkout_refuses_to_charge_without_a_live_rate(monkeypatch):
    monkeypatch.setattr(payments.settings, "paystack_secret_key", "sk_test_example")

    async def unavailable():
        raise payments.ExchangeRateUnavailable("rate down")

    paystack = AsyncMock()
    monkeypatch.setattr(payments, "get_usd_to_ghs_rate", unavailable)
    monkeypatch.setattr(payments, "paystack_request", paystack)
    with pytest.raises(HTTPException) as exc:
        await payments.create_checkout_session({"id": "alice", "email": "alice@example.com"})
    assert exc.value.status_code == 503
    paystack.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkout_stores_server_price_before_returning_url(monkeypatch):
    monkeypatch.setattr(payments.settings, "paystack_secret_key", "sk_test_example")
    monkeypatch.setattr(payments.settings, "pro_price_usd", 12.0)
    monkeypatch.setattr(payments, "get_usd_to_ghs_rate", AsyncMock(return_value=15))
    monkeypatch.setattr(payments, "paystack_request", AsyncMock(return_value={"data": {
        "authorization_url": "https://paystack.test/pay",
        "access_code": "ac",
        "reference": "ref_1",
    }}))
    db = MemoryDB()
    monkeypatch.setattr(payments, "Database", lambda **kwargs: db)
    result = await payments.create_checkout_session({"id": "alice", "email": "alice@example.com"})
    assert result["checkout_url"] == "https://paystack.test/pay"
    assert result["amount_ghs"] == 180
    sent = payments.paystack_request.await_args.args[2]
    assert sent["amount"] == 18000
    assert sent["currency"] == "GHS"
    stored = db.payments["ref_1"]
    assert stored["status"] == "pending"
    assert stored["user_id"] == "alice"
    assert stored["amount"] == 12
    assert stored["amount_local"] == 180


def test_payment_routes_do_not_keep_a_hardcoded_fx_fallback():
    source = open("app/routes/payments.py", encoding="utf-8").read()
    assert "14.5" not in source


@pytest.mark.asyncio
async def test_cancel_keeps_remaining_paid_period(monkeypatch):
    expires = datetime.now(timezone.utc) + timedelta(days=10)
    user = {
        "id": "alice",
        "tier": "pro",
        "pro_expires_at": iso(expires),
        "paystack_subscription_code": None,
    }
    db = MemoryDB()
    db.users["alice"] = dict(user)
    monkeypatch.setattr(payments, "Database", lambda **kwargs: db)
    paystack = AsyncMock()
    monkeypatch.setattr(payments, "paystack_request", paystack)
    result = await payments.cancel_subscription(user)
    assert result["tier"] == "pro"
    assert db.users["alice"]["tier"] == "pro"
    assert db.subs["alice"]["cancel_at_period_end"] is True
    paystack.assert_not_awaited()


@pytest.mark.asyncio
async def test_subscription_disable_does_not_end_paid_access():
    expires = iso(datetime.now(timezone.utc) + timedelta(days=5))
    db = MemoryDB()
    db.users["alice"] = {
        "id": "alice",
        "tier": "pro",
        "email": "alice@example.com",
        "paystack_customer_code": "CUS_1",
        "pro_expires_at": expires,
    }
    db.subs["alice"] = {"id": "sub", "user_id": "alice", "status": "active", "cancel_at_period_end": False}
    await payments._mark_subscription_cancelled(db, {"customer": {"customer_code": "CUS_1"}})
    assert db.users["alice"]["tier"] == "pro"
    assert db.users["alice"]["pro_expires_at"] == expires
    assert db.subs["alice"]["cancel_at_period_end"] is True


def _export_client(monkeypatch, module, user):
    monkeypatch.setattr(module, "Database", lambda **kwargs: (_ for _ in ()).throw(AssertionError("db used")))
    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


@pytest.mark.parametrize("module,path", [
    (daily_logs, "/api/daily-logs/export"),
    (incidents, "/api/incidents/export"),
])
def test_exports_require_current_pro(monkeypatch, module, path):
    free = _export_client(monkeypatch, module, {"id": "free", "tier": "free"})
    assert free.get(f"{path}?start_date=2026-01-01&end_date=2026-01-31").status_code == 403

    lapsed = _export_client(monkeypatch, module, {
        "id": "lapsed",
        "tier": "pro",
        "pro_expires_at": iso(datetime.now(timezone.utc) - timedelta(days=1)),
    })
    assert lapsed.get(f"{path}?start_date=2026-01-01&end_date=2026-01-31").status_code == 403

    db = MagicMock()
    if module is daily_logs:
        db.get_daily_logs = AsyncMock(return_value=[])
    else:
        db.get_incidents = AsyncMock(return_value=[])
    monkeypatch.setattr(module, "Database", lambda **kwargs: db)
    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "pro",
        "tier": "pro",
        "pro_expires_at": iso(datetime.now(timezone.utc) + timedelta(days=1)),
    }
    response = TestClient(app).get(f"{path}?start_date=2026-01-01&end_date=2026-01-31&format=csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
