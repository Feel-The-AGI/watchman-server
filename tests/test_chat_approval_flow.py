"""Exercise authenticated chat/proposal HTTP routes with isolated persistence."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app.engines.command_executor import CommandExecutor
from app.middleware.auth import get_current_user
from app.routes import chat, commands
from app.routes.commands import unexpired_proposal_filter


@pytest.fixture
def flow(monkeypatch):
    proposal_id = str(uuid4())
    rows = [{
        "id": proposal_id, "user_id": "alice", "status": "pending",
        "command": {"action": "add_leave", "payload": {"name": "Annual leave"}},
        "message_id": None, "expires_at": None,
    }]

    seen_filters = []
    fail = {"rejected": False}

    class Query:
        def __init__(self):
            self.filters, self.values = {}, None
            self.expiration_filter = None

        def select(self, *args, **kwargs): return self
        def eq(self, key, value):
            self.filters[key] = value
            return self
        def or_(self, expression):
            self.expiration_filter = expression
            seen_filters.append(expression)
            return self
        def order(self, *args, **kwargs): return self
        def limit(self, *args): return self
        def update(self, values):
            self.values = values
            return self
        def execute(self):
            if fail["rejected"] and self.values and self.values.get("status") == "rejected":
                raise APIError({
                    "message": 'invalid input value for enum proposal_status: "rejected"',
                    "code": "22P02",
                    "hint": None,
                    "details": None,
                })
            matched = [row for row in rows if all(
                row.get(key) == value for key, value in self.filters.items()
            ) and (not self.expiration_filter or row.get("expires_at") is None
                   or datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc))]
            for row in matched:
                if self.values: row.update(self.values)
            return SimpleNamespace(data=deepcopy(matched))

    db = SimpleNamespace(client=SimpleNamespace(table=lambda name: Query()))
    executor = SimpleNamespace(execute=AsyncMock(return_value={"success": True}))
    service = SimpleNamespace(
        get_history=AsyncMock(return_value=[{"id": "message", "content": "Hello"}]),
        send_message=AsyncMock(return_value={"is_command": True, "proposal": {"id": proposal_id}}),
    )
    monkeypatch.setattr(commands, "Database", lambda **kwargs: db)
    monkeypatch.setattr(chat, "Database", lambda **kwargs: db)
    monkeypatch.setattr(commands, "create_command_executor", lambda *args: executor)
    monkeypatch.setattr(chat, "create_chat_service", lambda *args: service)
    app = FastAPI()
    app.include_router(chat.router, prefix="/api")
    app.include_router(commands.router, prefix="/api")

    async def user(authorization: str = Header("alice")):
        if authorization == "unauthenticated": raise HTTPException(401)
        return {"id": authorization, "tier": "pro"}

    app.dependency_overrides[get_current_user] = user
    return SimpleNamespace(client=TestClient(app), rows=rows, id=proposal_id,
                           executor=executor, service=service, filters=seen_filters, fail=fail)


def review(flow, user="alice", decision="approve"):
    return flow.client.post(f"/api/commands/proposals/{flow.id}/review",
                            json={"decision": decision}, headers={"authorization": user})


def test_chat_history_returns_effective_tier(flow):
    result = flow.client.get("/api/chat/history")
    assert result.status_code == 200
    assert result.json()["tier"] == "pro"
    assert result.json()["messages"][0]["content"] == "Hello"


@pytest.mark.parametrize("limit", [0, -1, 501])
def test_history_rejects_invalid_limits(flow, limit):
    assert flow.client.get(f"/api/chat/history?limit={limit}").status_code == 422


def test_chat_proposes_by_default_and_refuses_auto_execution(flow):
    assert flow.client.post("/api/chat/message", json={"content": "Add leave"}).status_code == 200
    flow.service.send_message.assert_awaited_once_with(content="Add leave", auto_execute=False)
    assert flow.client.post("/api/chat/message", json={
        "content": "Add leave", "auto_execute": True,
    }).status_code == 422
    flow.executor.execute.assert_not_awaited()


def test_approval_executes_stored_command_once(flow):
    original = deepcopy(flow.rows[0]["command"])
    assert review(flow).status_code == 200
    flow.executor.execute.assert_awaited_once_with(
        original, source="api", message_id=None, proposal_id=flow.id,
    )
    assert review(flow).status_code == 409
    assert flow.executor.execute.await_count == 1


def test_other_user_cannot_approve_or_read_pending(flow):
    assert review(flow, user="bob").status_code == 409
    assert flow.client.get("/api/commands/proposals/pending", headers={
        "authorization": "bob",
    }).json() == {"proposals": []}
    assert flow.rows[0]["status"] == "pending"
    flow.executor.execute.assert_not_awaited()


def test_rejection_is_persisted_and_cannot_be_approved(flow):
    assert review(flow, decision="reject").status_code == 200
    assert flow.rows[0]["status"] == "rejected"
    assert review(flow).status_code == 409
    flow.executor.execute.assert_not_awaited()


def test_expired_proposal_cannot_execute_or_restore(flow):
    flow.rows[0]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert review(flow).status_code == 409
    assert flow.client.get("/api/commands/proposals/pending").json() == {"proposals": []}
    flow.executor.execute.assert_not_awaited()


def test_arbitrary_command_body_cannot_bypass_proposal(flow):
    response = flow.client.post("/api/commands/execute", json={
        "action": "add_leave", "payload": {"name": "Unapproved"},
    })
    assert response.status_code == 409
    flow.executor.execute.assert_not_awaited()


def test_legacy_execute_uses_saved_command_not_tampered_payload(flow):
    assert flow.client.post("/api/commands/execute", json={
        "proposal_id": flow.id, "action": "remove_commitment", "payload": {},
    }).status_code == 200
    assert flow.executor.execute.await_args.args[0]["action"] == "add_leave"


def test_previous_client_can_approve_matching_saved_proposal(flow):
    assert flow.client.post("/api/commands/execute", json=flow.rows[0]["command"]).status_code == 200
    assert flow.executor.execute.await_count == 1
    assert flow.client.post("/api/commands/execute", json=flow.rows[0]["command"]).status_code == 409
    assert flow.executor.execute.await_count == 1


def test_unauthenticated_review_is_rejected(flow):
    assert review(flow, user="unauthenticated").status_code == 401
    flow.executor.execute.assert_not_awaited()


def test_unexpired_filter_quotes_timestamp():
    expression = unexpired_proposal_filter(datetime(2026, 10, 21, 17, 4, 5, 123456, timezone.utc))
    assert expression == 'expires_at.is.null,expires_at.gt."2026-10-21T17:04:05Z"'


@pytest.mark.asyncio
async def test_new_proposals_expire_in_seven_days():
    inserted = {}

    class Query:
        def insert(self, data):
            inserted.update(data)
            return self

        def execute(self):
            return SimpleNamespace(data=[dict(inserted)])

    executor = CommandExecutor.__new__(CommandExecutor)
    executor.user_id = "alice"
    executor.db = SimpleNamespace(client=SimpleNamespace(table=lambda name: Query()))
    executor.validate_command = AsyncMock(return_value={"valid": True, "violations": []})
    before = datetime.now(timezone.utc)
    proposal = await executor.create_proposal({"action": "add_leave", "payload": {}})
    expires = datetime.fromisoformat(proposal["expires_at"])
    assert timedelta(days=6, hours=23) < expires - before < timedelta(days=7, minutes=1)


def test_expiry_filter_quotes_timestamp_for_postgrest(flow):
    assert review(flow).status_code == 200
    assert flow.filters
    expression = flow.filters[0]
    assert 'expires_at.gt."' in expression
    assert expression.endswith('"')
    assert "+" not in expression


def test_failed_write_restores_pending_and_is_not_a_success(flow):
    flow.executor.execute.return_value = {
        "success": False,
        "error": "Commitment 'Dentist appointment' was not saved",
    }
    response = review(flow)
    assert response.status_code == 500
    assert response.json()["detail"] == "The change could not be saved. It is still waiting for review."
    assert "was not saved" not in response.text
    assert flow.rows[0]["status"] == "pending"
    flow.executor.execute.return_value = {"success": True}
    assert review(flow).status_code == 200
    assert flow.rows[0]["status"] == "approved"


def test_decline_database_error_stays_pending_without_leaking_the_enum(flow):
    flow.fail["rejected"] = True
    response = review(flow, decision="reject")
    assert response.status_code == 500
    body = response.json()["detail"]
    assert body == "The change could not be declined. It is still waiting for review."
    assert "proposal_status" not in response.text
    assert "22P02" not in response.text
    assert flow.rows[0]["status"] == "pending"
    flow.executor.execute.assert_not_awaited()


def test_failed_validation_restores_pending_proposal(flow):
    flow.executor.execute.return_value = {
        "success": False,
        "error": "Command has constraint violations",
        "validation": {"valid": False, "violations": [{"message": "blocked"}]},
    }
    response = review(flow)
    assert response.status_code == 422
    assert flow.rows[0]["status"] == "pending"
    flow.executor.execute.return_value = {"success": True}
    assert review(flow).status_code == 200
    assert flow.rows[0]["status"] == "approved"


def test_invalid_stored_command_is_not_executed(flow):
    flow.rows[0]["command"] = "add_leave"
    assert review(flow).status_code == 409
    assert flow.rows[0]["status"] == "pending"
    flow.executor.execute.assert_not_awaited()
