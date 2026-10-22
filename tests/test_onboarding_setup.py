"""Onboarding: no leaked reasoning, a typed anchor, no default rules, no stray blank."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database import Database
from app.engines import chat_service
from app.engines.chat_context import (
    anchor_acknowledgement,
    parse_typed_anchor,
    strip_model_reasoning,
)
from app.engines.chat_service import SYSTEM_PROMPT, WATCHMAN_TOOLS, ChatService
from app.routes.auth import complete_onboarding


TODAY = date(2026, 10, 22)
ONBOARDING_PROMPT = (
    'The user is describing their work shift rotation pattern: "14 on, 14 off"\n'
    "Please understand this and respond in this EXACT JSON format:\n"
    '{"understood": true, "pattern": [], "description": "summary", "message": "reply"}'
)


class _Today(date):
    @classmethod
    def today(cls):
        return TODAY


def _service():
    service = ChatService.__new__(ChatService)
    service.user_id = "user-1"
    service.db = SimpleNamespace()
    service.settings_service = SimpleNamespace(get_snapshot=AsyncMock(return_value={}))
    service.command_executor = SimpleNamespace(
        create_proposal=AsyncMock(return_value={"id": "proposal-1", "status": "pending"}),
        execute=AsyncMock(),
    )

    async def save(role, content, command_id=None):
        return {"id": "message", "role": role, "content": content}

    service._save_message = save
    service._get_calendar_snapshot = AsyncMock(return_value={
        "text": "No calendar days are stored yet.",
        "count": 0,
        "start": None,
        "end": None,
        "counts": {},
    })
    service._get_logs_and_incidents = AsyncMock(return_value="none")
    service._recent_proposals = AsyncMock(return_value=[])
    service.get_history = AsyncMock(return_value=[])
    return service


def _blank_tool():
    return (
        "I'll clear the days before Oct 22.",
        {
            "name": "override_days",
            "input": {
                "start_date": "2026-01-01",
                "end_date": "2026-10-21",
                "work_type": "blank",
            },
        },
    )


def test_model_reasoning_is_removed_before_it_is_shown():
    leaked = 'The user says they work 14 on and 14 off. Got it — 14 days on, 14 off.'
    cleaned = strip_model_reasoning(leaked)
    assert "The user says" not in cleaned
    assert "the user says" not in cleaned.lower()
    assert "14 days on, 14 off" in cleaned

    raw = (
        '{"understood": false, "pattern": [], "description": "The user says 14 on", '
        '"message": "The user says this is unclear. What pattern is it?"}'
    )
    hidden = strip_model_reasoning(raw)
    assert "the user says" not in hidden.lower()
    assert "What pattern is it?" in hidden


@pytest.mark.asyncio
async def test_a_model_reply_starting_the_user_says_never_reaches_the_user(monkeypatch):
    service = _service()

    async def fake_complete(**kwargs):
        return (
            "The user says today is day 1. Oct 22 is day 1 of your cycle.",
            None,
        )

    monkeypatch.setattr(chat_service, "complete_chat", fake_complete)
    monkeypatch.setattr(chat_service, "projection_for_user", AsyncMock(return_value={}))
    result = await service.send_message("hello", auto_execute=False)
    blob = f"{result['response']} {(result.get('assistant_message') or {}).get('content')}"
    assert "the user says" not in blob.lower()
    assert "Oct 22 is day 1" in result["response"]

    async def parse(**kwargs):
        return (
            'The user says "14 on, 14 off". {"understood": false, "pattern": [], '
            '"description": "summary", "message": "The user says this is unclear. Try 14 on, 14 off."}',
            None,
        )

    monkeypatch.setattr(chat_service, "complete_chat", parse)
    parsed = await service.send_message(ONBOARDING_PROMPT, auto_execute=False)
    assert "the user says" not in parsed["response"].lower()
    assert "Try 14 on, 14 off." in parsed["response"]


FIVE_FIVE_FIVE = [
    {"label": "work_day", "duration": 5},
    {"label": "work_night", "duration": 5},
    {"label": "off", "duration": 5},
]
LIVE_ANCHOR = "Today, Oct 22 2026, is day 1 of the cycle."
LIVE_WRONG_REPLY = (
    "I'll set up the anchor so that today, 2026-10-22, is cycle day 1. "
    "But I need the rotation pattern first: the calendar has no cycle stored yet, "
    "so I can't set an anchor without knowing the shift blocks. "
    "Could you tell me your full rotation pattern?"
)


@pytest.mark.parametrize("text, expected", [
    ("Today Oct 22 is day 1", {"date": "2026-10-22", "cycle_day": 1}),
    ("day 1 was Oct 20", {"date": "2026-10-20", "cycle_day": 1}),
    (LIVE_ANCHOR, {"date": "2026-10-22", "cycle_day": 1}),
])
def test_a_typed_anchor_matches_the_slider(text, expected):
    assert parse_typed_anchor(text, TODAY) == expected
    reply = anchor_acknowledgement(expected)
    assert "pattern" not in reply.lower()
    assert str(expected["cycle_day"]) in reply


@pytest.mark.asyncio
@pytest.mark.parametrize("text, expected_date", [
    ("Today Oct 22 is day 1", "2026-10-22"),
    ("day 1 was Oct 20", "2026-10-20"),
])
async def test_a_typed_anchor_does_not_ask_for_the_pattern(monkeypatch, text, expected_date):
    service = _service()
    monkeypatch.setattr(chat_service, "date", _Today)
    called = {"n": 0}

    async def fake_complete(**kwargs):
        called["n"] += 1
        return _blank_tool()

    monkeypatch.setattr(chat_service, "complete_chat", fake_complete)
    result = await service.send_message(text, auto_execute=False)

    assert called["n"] == 0
    assert result["proposal"] is None
    assert result["command"] is None
    assert result["anchor"] == {"date": expected_date, "cycle_day": 1}
    assert "pattern" not in result["response"].lower()
    assert "clear the days" not in result["response"].lower()
    service.command_executor.create_proposal.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_picked_preset_is_known_when_the_anchor_is_typed(monkeypatch):
    """Live signup: 5-5-5 was chosen, then the anchor was typed before it was saved."""
    service = _service()
    monkeypatch.setattr(chat_service, "date", _Today)
    monkeypatch.setattr(chat_service, "projection_for_user", AsyncMock(return_value={}))
    seen = {}

    async def fake_complete(**kwargs):
        seen["system"] = kwargs.get("system") or ""
        return (LIVE_WRONG_REPLY, None)

    monkeypatch.setattr(chat_service, "complete_chat", fake_complete)
    result = await service.send_message(LIVE_ANCHOR, pattern=FIVE_FIVE_FIVE)
    reply = result["response"]
    lowered = reply.lower()
    assert result["anchor"] == {"date": "2026-10-22", "cycle_day": 1}
    assert "5 day shifts" in reply
    assert "5 night shifts" in reply
    assert "5 days off" in reply
    assert "no cycle" not in lowered
    assert "i'll set up" not in lowered
    assert "rotation pattern" not in lowered
    assert "shift blocks" not in lowered
    assert seen == {}

    follow = await service.send_message("What should I do next?", pattern=FIVE_FIVE_FIVE)
    system = seen["system"]
    assert "CHOSEN." in system
    assert "5 work day" in system
    assert "5 work night" in system
    assert "5 off" in system
    assert "No rotation is stored" not in system
    assert "no cycle" not in follow["response"].lower()
    assert "i'll set up the anchor" not in follow["response"].lower()


def test_the_prompt_does_not_tell_the_model_to_blank_days_before_the_anchor():
    tool = next(item for item in WATCHMAN_TOOLS if item["name"] == "update_cycle")
    instructions = f"{SYSTEM_PROMPT}\n{tool['description']}".lower()
    assert "mark those days as 'blank'" not in instructions
    assert "before rotation became stable" not in instructions
    assert "as 'blank' using override_days" not in instructions
    assert "can be marked as 'blank'" not in instructions
    assert "stay filled from the pattern" in instructions
    assert "unless the user explicitly asks" in instructions


@pytest.mark.asyncio
async def test_a_setup_message_stores_no_override_days_proposal(monkeypatch):
    """The model used to answer an anchor with 'clear the days before Oct 22'."""
    service = _service()
    monkeypatch.setattr(chat_service, "date", _Today)
    monkeypatch.setattr(chat_service, "projection_for_user", AsyncMock(return_value={}))

    async def fake_complete(**kwargs):
        return _blank_tool()

    monkeypatch.setattr(chat_service, "complete_chat", fake_complete)
    result = await service.send_message("Today Oct 22 is day 1", auto_execute=False)
    assert result["proposal"] is None
    assert (result.get("command") or {}).get("action") != "override_days"
    service.command_executor.create_proposal.assert_not_awaited()

    # A message that is not itself an anchor still must not store that card.
    other = await service.send_message("The calendar looks good", auto_execute=False)
    assert other["proposal"] is None
    assert other["response"] == "Days before the anchor stay filled from your pattern."
    assert "clear the days before" not in other["response"].lower()
    service.command_executor.create_proposal.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_explicit_request_to_blank_days_can_still_be_proposed(monkeypatch):
    service = _service()
    monkeypatch.setattr(chat_service, "projection_for_user", AsyncMock(return_value={}))
    monkeypatch.setattr(chat_service, "complete_chat", AsyncMock(return_value=_blank_tool()))
    result = await service.send_message(
        "Please blank the days before Oct 22",
        auto_execute=False,
    )
    assert result["proposal"]["id"] == "proposal-1"
    assert result["command"]["action"] == "override_days"
    service.command_executor.create_proposal.assert_awaited()


@pytest.mark.asyncio
async def test_completing_onboarding_saves_no_default_rules(monkeypatch):
    inserted = []

    class DB:
        def __init__(self, use_admin=False):
            pass

        async def get_active_constraints(self, user_id):
            return []

        async def create_constraint(self, data):
            inserted.append(data["name"])
            return data

        async def create_default_constraints(self, user_id):
            inserted.append("defaults")
            return [
                {"name": "No study on night shifts"},
                {"name": "Maximum 2 concurrent education commitments"},
                {"name": "Work is immutable"},
            ]

        async def complete_onboarding(self, user_id):
            inserted.append(user_id)
            return {"onboarding_completed": True}

    monkeypatch.setattr("app.routes.auth.Database", DB)
    result = await complete_onboarding({"id": "user-1", "onboarding_completed": False})
    assert result["message"] == "Onboarding completed."
    assert inserted == ["user-1"]
    assert "No study on night shifts" not in inserted
    assert "Work is immutable" not in inserted


@pytest.mark.asyncio
async def test_create_default_constraints_writes_nothing():
    database = Database.__new__(Database)
    assert await database.create_default_constraints("user-1") == []
