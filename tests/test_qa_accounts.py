"""The temporary QA route is off by default, secret-gated, and scoped to QA addresses."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import qa

SECRET = "s" * 40


def _settings(**overrides):
    values = {"qa_bypass_secret": SECRET, "qa_email_prefix": "owner+qa", "qa_email_domain": "gmail.com"}
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def client(monkeypatch):
    admin = MagicMock()
    admin.auth.admin.create_user.return_value = SimpleNamespace(user=SimpleNamespace(id="auth-1"))
    db = SimpleNamespace(
        get_user_by_email=AsyncMock(return_value={"id": "user-1", "auth_id": "auth-1"}),
        update_user=AsyncMock(),
        delete_all_user_data=AsyncMock(),
        delete_auth_user=AsyncMock(return_value=True),
    )
    state = {"settings": _settings()}
    monkeypatch.setattr(qa, "get_settings", lambda: state["settings"])
    monkeypatch.setattr(qa, "get_supabase_admin", lambda: admin)
    monkeypatch.setattr(qa, "Database", lambda **kwargs: db)
    app = FastAPI()
    app.include_router(qa.router, prefix="/api/qa")
    return SimpleNamespace(http=TestClient(app), admin=admin, db=db, state=state)


BODY = {"email": "owner+qa01@gmail.com", "password": "long-enough-pass"}


@pytest.mark.parametrize("overrides", [
    {"qa_bypass_secret": ""},
    {"qa_bypass_secret": "short"},
    {"qa_email_prefix": ""},
    {"qa_email_domain": ""},
])
def test_off_unless_fully_configured(client, overrides):
    client.state["settings"] = _settings(**overrides)
    response = client.http.post("/api/qa/accounts", json=BODY, headers={"X-QA-Secret": SECRET})
    assert response.status_code == 404
    assert client.http.request("DELETE", "/api/qa/accounts", json={"email": BODY["email"]},
                               headers={"X-QA-Secret": SECRET}).status_code == 404
    client.admin.auth.admin.create_user.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"X-QA-Secret": "wrong" * 10}])
def test_wrong_or_missing_secret_is_refused(client, headers):
    assert client.http.post("/api/qa/accounts", json=BODY, headers=headers).status_code == 401
    client.admin.auth.admin.create_user.assert_not_called()


@pytest.mark.parametrize("email", [
    "owner@gmail.com",
    "owner+qa@gmail.com",
    "owner+qa01@evil.com",
    "someone+qa01@gmail.com",
    "owner+qa01@gmail.com.evil.com",
    "owner+qa-01@gmail.com",
    "owner+qa01@gmail.com@x.com",
    "xowner+qa01@gmail.com",
    "owner+qa" + "a" * 25 + "@gmail.com",
])
def test_only_qa_addresses_are_accepted(client, email):
    headers = {"X-QA-Secret": SECRET}
    assert client.http.post("/api/qa/accounts", json={**BODY, "email": email}, headers=headers).status_code == 403
    assert client.http.request("DELETE", "/api/qa/accounts", json={"email": email}, headers=headers).status_code == 403
    client.admin.auth.admin.create_user.assert_not_called()
    client.db.delete_all_user_data.assert_not_awaited()


def test_creates_a_confirmed_account(client):
    response = client.http.post("/api/qa/accounts", json={**BODY, "email": " Owner+QA01@Gmail.com "},
                                headers={"X-QA-Secret": SECRET})
    assert response.status_code == 200
    sent = client.admin.auth.admin.create_user.call_args.args[0]
    assert sent["email"] == "owner+qa01@gmail.com"
    assert sent["email_confirm"] is True
    client.db.update_user.assert_not_awaited()


def test_pro_tier_is_set_on_the_qa_profile_only(client):
    response = client.http.post("/api/qa/accounts", json={**BODY, "tier": "pro"}, headers={"X-QA-Secret": SECRET})
    assert response.status_code == 200
    client.db.update_user.assert_awaited_once_with("user-1", {"tier": "pro"})


def test_short_password_and_duplicate(client):
    headers = {"X-QA-Secret": SECRET}
    assert client.http.post("/api/qa/accounts", json={**BODY, "password": "short"}, headers=headers).status_code == 422
    client.admin.auth.admin.create_user.side_effect = RuntimeError("already registered")
    assert client.http.post("/api/qa/accounts", json=BODY, headers=headers).status_code == 409


def test_delete_removes_data_and_auth_user(client):
    response = client.http.request("DELETE", "/api/qa/accounts", json={"email": BODY["email"]},
                                   headers={"X-QA-Secret": SECRET})
    assert response.status_code == 200
    client.db.delete_all_user_data.assert_awaited_once_with("user-1")
    client.db.delete_auth_user.assert_awaited_once_with("auth-1")


def test_the_route_is_not_in_the_public_schema():
    from app.main import create_app

    schema = create_app().openapi()
    assert not any(path.startswith("/api/qa") for path in schema["paths"])
