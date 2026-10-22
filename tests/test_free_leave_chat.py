"""Free accounts cannot plan leave from chat. Sick days and other edits stay."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from loguru import logger

from app.commitment_limit import COMMITMENT_LIMIT_REPLY
from app.engines.chat_service import ChatService
from app.engines.command_executor import CommandExecutor
from app.engines import chat_service
from app.leave_access import LEAVE_PRO_REPLY
from app.middleware.auth import get_current_user
from app.prompts.registry import version_for
from app.routes import commands


def _settings(**overrides):
    values = {
        "chat_prompt_version": "",
        "onboarding_prompt_version": "",
        "chat_prompt_allowlist": "",
        "onboarding_prompt_allowlist": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _service():
    service = ChatService.__new__(ChatService)
    service.user_id = "user-1"
    service.db = SimpleNamespace()
    service.settings_service = SimpleNamespace(get_snapshot=AsyncMock(return_value={}))
    created = AsyncMock(return_value={"id": "proposal-1"})
    executed = AsyncMock(return_value={"success": True})
    service.command_executor = SimpleNamespace(
        create_proposal=created,
        execute=executed,
    )
    service._get_calendar_snapshot = AsyncMock(return_value={
        "text": "none", "count": 0, "start": None, "end": None, "counts": {},
    })
    service._get_logs_and_incidents = AsyncMock(return_value="none")
    service.get_history = AsyncMock(return_value=[])
    service._recent_proposals = AsyncMock(return_value=[])
    service._save_message = AsyncMock(
        side_effect=lambda role, content, command_id=None: {
            "id": f"msg-{role}", "role": role, "content": content,
        }
    )
    return service, created, executed


def _install(monkeypatch, version, tool):
    monkeypatch.setattr(
        "app.prompts.registry.get_settings",
        lambda: _settings(chat_prompt_version=version),
    )
    monkeypatch.setattr(chat_service, "projection_for_user", AsyncMock(return_value={}))
    monkeypatch.setattr(chat_service, "schedule_question_date", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        chat_service,
        "complete_chat",
        AsyncMock(return_value=("I'll do that.", tool)),
    )


async def _send(service, tier):
    return await service.send_message(
        "Please add that to my calendar",
        auto_execute=True,
        effective_tier=tier,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["", "chat-v1"])
async def test_free_leave_via_chat_writes_nothing(monkeypatch, version):
    service, created, executed = _service()
    _install(monkeypatch, version, {
        "name": "add_leave",
        "input": {"name": "Annual Leave", "start_date": "2026-11-01", "end_date": "2026-11-05"},
    })

    result = await _send(service, "free")

    expected = "chat-v2" if version == "" else "chat-v1"
    assert version_for("chat").id == expected
    assert result["proposal"] is None
    assert result["is_command"] is False
    assert "execution" not in result
    assert LEAVE_PRO_REPLY in result["response"]
    assert "Upgrade to Pro" in result["response"]
    created.assert_not_awaited()
    executed.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["pro", "trial", "admin"])
async def test_paid_leave_via_chat_still_creates_a_proposal(monkeypatch, tier):
    service, created, executed = _service()
    _install(monkeypatch, "", {
        "name": "add_leave",
        "input": {
            "name": "Annual Leave",
            "type": "annual",
            "start_date": "2026-11-01",
            "end_date": "2026-11-05",
        },
    })

    result = await service.send_message("Add annual leave", effective_tier=tier)

    assert result["proposal"]["id"] == "proposal-1"
    created.assert_awaited()
    executed.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [
    {"name": "add_leave", "input": {"name": "Sick day", "type": "sick", "start_date": "2026-11-02", "end_date": "2026-11-02"}},
    {"name": "override_days", "input": {"start_date": "2026-11-03", "end_date": "2026-11-03", "work_type": "work_day"}},
    {"name": "create_daily_log", "input": {"date": "2026-11-04", "note": "2 hours overtime", "actual_hours": 10}},
    {"name": "add_commitment", "input": {"name": "Swap cover", "type": "work"}},
])
async def test_free_non_leave_chat_edits_still_proceed(monkeypatch, tool):
    service, created, executed = _service()
    _install(monkeypatch, "chat-v1", tool)

    result = await service.send_message("Log that change", effective_tier="free")

    assert LEAVE_PRO_REPLY not in (result.get("response") or "")
    assert result["proposal"]["id"] == "proposal-1"
    created.assert_awaited()
    executed.assert_not_awaited()


def _limit_executor(rows, tier="free"):
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.user_id = "user-1"
    executor.effective_tier = tier
    executor.db = SimpleNamespace(
        get_user_by_id=AsyncMock(return_value={"id": "user-1", "tier": tier}),
        read_commitments=AsyncMock(return_value=rows),
        create_commitment=AsyncMock(),
    )
    return executor


@pytest.mark.asyncio
async def test_free_chat_cannot_add_a_third_commitment(monkeypatch):
    service, created, executed = _service()
    rows = [
        {"id": "one", "user_id": "user-1", "name": "Gym", "type": "personal", "start_date": "2026-11-01"},
        {"id": "two", "user_id": "user-1", "name": "Course", "type": "education", "start_date": "2026-11-02"},
    ]
    limiter = _limit_executor(rows)
    service.command_executor.commitment_limit_message = limiter.commitment_limit_message
    _install(monkeypatch, "", {
        "name": "add_commitment",
        "input": {"name": "Dentist", "type": "personal", "start_date": "2026-12-01"},
    })

    result = await service.send_message("Add another commitment", effective_tier="free")

    assert result["response"] == COMMITMENT_LIMIT_REPLY
    assert result["proposal"] is None
    assert result["is_command"] is False
    created.assert_not_awaited()
    executed.assert_not_awaited()
    limiter.db.create_commitment.assert_not_awaited()

    blocked = await limiter.execute({
        "action": "add_commitment",
        "payload": {"name": "Dentist", "type": "personal", "start_date": "2026-12-01"},
    })
    assert blocked["success"] is False
    assert blocked["code"] == "commitment_limit"
    assert blocked["error"] == COMMITMENT_LIMIT_REPLY
    limiter.db.create_commitment.assert_not_awaited()

    assert await limiter.commitment_limit_message({
        "id": "one", "name": "Renamed", "type": "work",
    }) is None
    paid = _limit_executor(rows, tier="pro")
    assert await paid.commitment_limit_message({"name": "Dentist", "type": "personal"}) is None


@pytest.mark.asyncio
async def test_commitment_count_failure_lets_the_chat_turn_through(monkeypatch):
    service, created, executed = _service()

    async def broken(payload):
        raise RuntimeError("commitments read failed")

    service.command_executor.commitment_limit_message = broken
    _install(monkeypatch, "", {
        "name": "add_commitment",
        "input": {"name": "Dentist", "type": "personal", "start_date": "2026-12-01"},
    })
    records = []
    handle = logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        result = await service.send_message("Add a dentist visit", effective_tier="free")
    finally:
        logger.remove(handle)

    assert result["proposal"]["id"] == "proposal-1"
    assert result["is_command"] is True
    assert COMMITMENT_LIMIT_REPLY not in (result.get("response") or "")
    created.assert_awaited()
    executed.assert_not_awaited()
    assert any("commitments read failed" in line for line in records)


def _review_client(monkeypatch, user, command, commitments=None):
    proposal_id = str(uuid4())
    rows = [{
        "id": proposal_id,
        "user_id": user["id"],
        "status": "pending",
        "command": command,
        "message_id": None,
        "expires_at": None,
    }]

    class Query:
        def __init__(self):
            self.filters, self.values = {}, None

        def update(self, values):
            self.values = values
            return self

        def eq(self, key, value):
            self.filters[key] = value
            return self

        def or_(self, expression):
            return self

        def execute(self):
            matched = [
                row for row in rows
                if all(row.get(key) == value for key, value in self.filters.items())
            ]
            for row in matched:
                if self.values:
                    row.update(self.values)
            return SimpleNamespace(data=deepcopy(matched))

    db = SimpleNamespace(
        client=SimpleNamespace(table=lambda name: Query()),
        get_user_by_id=AsyncMock(return_value=user),
        read_commitments=AsyncMock(return_value=list(commitments or [])),
        create_commitment=AsyncMock(),
    )

    def make_executor(database, user_id):
        executor = CommandExecutor.__new__(CommandExecutor)
        executor.db = database
        executor.user_id = user_id
        return executor

    monkeypatch.setattr(commands, "Database", lambda **kwargs: db)
    monkeypatch.setattr(commands, "create_command_executor", make_executor)
    app = FastAPI()
    app.include_router(commands.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), rows, proposal_id, db


def _past():
    return (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


@pytest.mark.parametrize("user", [
    {"id": "alice", "tier": "free"},
    {"id": "alice", "tier": "pro", "pro_expires_at": _past()},
])
def test_review_refuses_leave_for_free_and_lapsed_pro(monkeypatch, user):
    client, rows, proposal_id, db = _review_client(
        monkeypatch,
        user,
        {"action": "add_leave", "payload": {"name": "Holiday", "type": "annual"}},
    )
    response = client.post(
        f"/api/commands/proposals/{proposal_id}/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == LEAVE_PRO_REPLY
    assert rows[0]["status"] == "pending"
    assert rows[0]["reviewed_at"] is None
    db.create_commitment.assert_not_awaited()


@pytest.mark.parametrize("user", [
    {"id": "alice", "tier": "free"},
    {"id": "alice", "tier": "pro", "pro_expires_at": _past()},
])
def test_review_refuses_a_third_commitment_for_free_and_lapsed_pro(monkeypatch, user):
    existing = [
        {"id": "one", "user_id": "alice", "name": "Gym", "type": "personal", "start_date": "2026-11-01"},
        {"id": "two", "user_id": "alice", "name": "Course", "type": "education", "start_date": "2026-11-02"},
    ]
    client, rows, proposal_id, db = _review_client(
        monkeypatch,
        user,
        {"action": "add_commitment", "payload": {"name": "Dentist", "type": "personal", "start_date": "2026-12-01"}},
        existing,
    )
    response = client.post(
        f"/api/commands/proposals/{proposal_id}/review",
        json={"decision": "approve"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "commitment_limit",
        "message": COMMITMENT_LIMIT_REPLY,
    }
    assert rows[0]["status"] == "pending"
    assert rows[0]["reviewed_at"] is None
    db.create_commitment.assert_not_awaited()


@pytest.mark.asyncio
async def test_lapsed_pro_form_is_capped_at_two_commitments(monkeypatch):
    from app.routes.commitments import CreateCommitmentRequest, create_commitment

    class Store:
        def __init__(self, use_admin=False):
            self.created = False

        async def read_commitments(self, user_id):
            return [
                {"id": "one", "user_id": user_id, "name": "Gym", "type": "personal"},
                {"id": "two", "user_id": user_id, "name": "Course", "type": "education"},
            ]

        async def create_commitment(self, data):
            self.created = True
            return data

    store = Store()
    monkeypatch.setattr("app.routes.commitments.Database", lambda use_admin=False: store)
    with pytest.raises(HTTPException) as exc:
        await create_commitment(
            CreateCommitmentRequest(name="Dentist", type="personal"),
            {
                "id": "alice",
                "tier": "pro",
                "pro_expires_at": _past(),
                "settings": {},
            },
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == {
        "code": "commitment_limit",
        "message": COMMITMENT_LIMIT_REPLY,
    }
    assert store.created is False
