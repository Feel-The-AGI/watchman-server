"""Entitlements: paid periods and Pro-only exports. Stripe flows are in test_stripe_billing.py."""

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
    extend_expiry,
    monthly_recurring_revenue,
    paying_user_count,
)
from app.middleware.auth import get_current_user, get_effective_tier, require_pro_tier
from app.routes import chat, daily_logs, incidents, payments
from tests.test_security_regressions import make_request


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


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
