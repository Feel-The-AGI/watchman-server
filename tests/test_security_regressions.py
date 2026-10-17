"""Security regression coverage for the Watchman server.

Copy to tests/test_security_regressions.py, then run pytest -q.
These are independent route/unit tests with mocked persistence; they do not
validate production Paystack configuration or a complete checkout journey.
"""
import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routes import calendar as calendar_routes
from app.routes import cycles, payments, settings as settings_routes
from app.database import Database


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    "update_cycle", "delete_cycle", "update_constraint", "delete_constraint",
    "delete_leave_block",
])
@pytest.mark.parametrize("owner", ["me", "other-user"])
async def test_database_mutations_isolate_users(method, owner):
    """Exercise real database methods with a two-user in-memory query adapter."""
    rows = [{"id": "record", "user_id": owner, "name": "Original"}]

    class Query:
        def __init__(self):
            self.filters = {}
            self.changes = None

        def table(self, name):
            return self

        def update(self, changes):
            self.changes = changes
            return self

        def delete(self):
            return self

        def eq(self, key, value):
            self.filters[key] = value
            return self

        def execute(self):
            matched = [row for row in rows if all(
                row.get(key) == value for key, value in self.filters.items()
            )]
            for row in matched:
                if self.changes is None:
                    rows.remove(row)
                else:
                    row.update(self.changes)
            return SimpleNamespace(data=matched)

    db = object.__new__(Database)
    db.client = Query()
    args = ("record", {"name": "Changed"}) if method.startswith("update") else ("record",)
    result = await getattr(db, method)(*args, user_id="me")
    if owner == "other-user":
        assert not result
        assert rows == [{"id": "record", "user_id": owner, "name": "Original"}]
    else:
        assert result
        if method.startswith("update"):
            assert rows[0]["name"] == "Changed"
        else:
            assert rows == []


@pytest.mark.asyncio
async def test_update_foreign_cycle_is_rejected_before_mutation(monkeypatch):
    db = MagicMock()
    db.get_cycles = AsyncMock(return_value=[{"id": "someone-elses-cycle"}])
    db.update_cycle = AsyncMock()
    monkeypatch.setattr(cycles, "Database", lambda **kwargs: db)
    monkeypatch.setattr(cycles, "create_calendar_engine", lambda user_id: MagicMock())
    with pytest.raises(HTTPException) as exc:
        await cycles.update_cycle("foreign", cycles.UpdateCycleRequest(name="Tampered"), {"id": "me"})
    assert exc.value.status_code == 404
    db.update_cycle.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_foreign_cycle_is_rejected(monkeypatch):
    db = MagicMock()
    db.delete_cycle = AsyncMock(return_value=False)
    monkeypatch.setattr(cycles, "Database", lambda **kwargs: db)
    with pytest.raises(HTTPException) as exc:
        await cycles.delete_cycle("foreign", {"id": "me"})
    assert exc.value.status_code == 404
    db.delete_cycle.assert_awaited_once_with("foreign", user_id="me")


@pytest.mark.asyncio
async def test_update_foreign_constraint_is_rejected(monkeypatch):
    db = MagicMock()
    db.get_constraints = AsyncMock(return_value=[])
    db.update_constraint = AsyncMock()
    monkeypatch.setattr(settings_routes, "Database", lambda **kwargs: db)
    data = SimpleNamespace(name="x", description="x", rule={}, weight=1, is_active=True)
    with pytest.raises(HTTPException) as exc:
        await settings_routes.update_constraint("foreign", data, {"id": "me"})
    assert exc.value.status_code == 404
    db.update_constraint.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_foreign_constraint_is_rejected(monkeypatch):
    db = MagicMock()
    db.get_constraints = AsyncMock(return_value=[])
    db.delete_constraint = AsyncMock()
    monkeypatch.setattr(settings_routes, "Database", lambda **kwargs: db)
    with pytest.raises(HTTPException) as exc:
        await settings_routes.delete_constraint("foreign", {"id": "me"})
    assert exc.value.status_code == 404
    db.delete_constraint.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_foreign_leave_is_rejected(monkeypatch):
    db = MagicMock()
    db.get_leave_blocks = AsyncMock(return_value=[])
    db.delete_leave_block = AsyncMock()
    monkeypatch.setattr(calendar_routes, "Database", lambda **kwargs: db)
    with pytest.raises(HTTPException) as exc:
        await calendar_routes.delete_leave_block("foreign", {"id": "me"})
    assert exc.value.status_code == 404
    db.delete_leave_block.assert_not_awaited()


def make_request(payload: bytes, signature: str | None) -> Request:
    headers = [(b"content-type", b"application/json")]
    if signature is not None:
        headers.append((b"x-paystack-signature", signature.encode("latin-1")))
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/api/payments/webhook", "headers": headers}, receive)


@pytest.mark.asyncio
async def test_webhook_rejects_missing_signature(monkeypatch):
    monkeypatch.setattr(payments.settings, "paystack_secret_key", "sk_test_example")
    with pytest.raises(HTTPException) as exc:
        await payments.paystack_webhook(make_request(b'{"event":"unknown"}', None))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("signature", ["0" * 128, "invalid", "é"])
async def test_webhook_rejects_bad_signature(monkeypatch, signature):
    monkeypatch.setattr(payments.settings, "paystack_secret_key", "sk_test_example")
    with pytest.raises(HTTPException) as exc:
        await payments.paystack_webhook(make_request(b'{"event":"unknown"}', signature))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_webhook_rejects_missing_server_key(monkeypatch):
    monkeypatch.setattr(payments.settings, "paystack_secret_key", "")
    with pytest.raises(HTTPException) as exc:
        await payments.paystack_webhook(make_request(b'{"event":"unknown"}', "0" * 128))
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_webhook_accepts_correct_signed_unknown_event(monkeypatch):
    secret = "sk_test_example"
    payload = b'{"event":"unknown","data":{}}'
    signature = hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()
    monkeypatch.setattr(payments.settings, "paystack_secret_key", secret)
    monkeypatch.setattr(payments, "Database", lambda **kwargs: MagicMock())
    monkeypatch.setattr(payments, "get_email_service", lambda: MagicMock())
    response = await payments.paystack_webhook(make_request(payload, signature))
    assert response == {"status": "success"}
