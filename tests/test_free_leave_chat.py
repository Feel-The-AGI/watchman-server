"""Free accounts cannot plan leave from chat. Sick days and other edits stay."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.commitment_limit import COMMITMENT_LIMIT_REPLY
from app.engines.chat_service import ChatService
from app.engines.command_executor import CommandExecutor
from app.engines import chat_service
from app.leave_access import LEAVE_PRO_REPLY
from app.prompts.registry import version_for


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
