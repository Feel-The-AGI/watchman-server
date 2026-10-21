"""A declined, approved, or expired proposal must not be offered again."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.engines import chat_service
from app.engines.chat_context import (
    annotate_closed_proposals,
    closed_proposal_note,
    declined_again_reply,
    repeats_declined_proposal,
)
from app.engines.chat_service import SYSTEM_PROMPT, ChatService
from app.engines.claude_chat import history_to_messages


LEAVE = {
    "id": "proposal-leave",
    "message_id": "m-leave",
    "status": "declined",
    "created_at": "2026-10-21T20:31:00+00:00",
    "command": {
        "action": "add_leave",
        "payload": {"type": "other", "date": "2026-10-31"},
        "explanation": "Mark Sat Oct 31 as leave",
    },
}

# Newest first, as get_history returns it. The dentist turn is the one just saved.
HISTORY = [
    {
        "id": "m-dentist",
        "role": "user",
        "content": "Add a dentist appointment next Tuesday Oct 27 from 3pm to 4pm",
    },
    {"id": "m-card", "role": "assistant", "content": "Mark Sat Oct 31 as leave"},
    {"id": "m-leave", "role": "user", "content": "Mark Saturday Oct 31 as leave"},
]


def test_prompt_proposes_only_the_latest_message():
    assert "Propose ONLY what the latest message asks." in SYSTEM_PROMPT
    assert "Do not bundle an earlier request" in SYSTEM_PROMPT


def test_declined_leave_is_marked_on_the_old_turns_only():
    note = closed_proposal_note(LEAVE)
    assert note == "The user declined this change (Mark Sat Oct 31 as leave). Do not re-propose it."
    annotated = annotate_closed_proposals(HISTORY, [LEAVE])
    messages = history_to_messages(annotated, HISTORY[0]["content"])
    earlier = "\n".join(message["content"] for message in messages[:-1])
    assert "The user declined this change (Mark Sat Oct 31 as leave). Do not re-propose it." in earlier
    assert messages[-1] == {
        "role": "user",
        "content": "Add a dentist appointment next Tuesday Oct 27 from 3pm to 4pm",
    }
    assert "declined" not in messages[-1]["content"].lower()
    assert "Oct 31" not in messages[-1]["content"]


def test_approved_and_expired_are_closed_and_pending_is_not():
    approved = closed_proposal_note({
        "status": "approved",
        "command": {
            "action": "add_commitment",
            "payload": {
                "name": "Dentist appointment",
                "type": "personal",
                "start_date": "2026-10-27",
                "end_date": "2026-10-27",
                "time_start": "15:00",
                "time_end": "16:00",
            },
            "explanation": "Executing add_commitment",
        },
    })
    assert approved.startswith("The user approved this change (")
    assert "Do not propose it again." in approved
    assert "Executing" not in approved

    expired = closed_proposal_note({
        "status": "expired",
        "command": {"action": "add_leave", "payload": {"date": "2026-10-31"}, "explanation": ""},
    })
    assert expired.startswith("This proposal expired (")
    assert "Do not re-propose it." in expired

    now = datetime(2026, 10, 21, 21, 0, tzinfo=timezone.utc)
    still_open = {"status": "pending", "expires_at": (now + timedelta(days=1)).isoformat()}
    assert closed_proposal_note(still_open, now) is None
    lapsed = {"status": "pending", "expires_at": (now - timedelta(minutes=1)).isoformat()}
    assert closed_proposal_note(lapsed, now).startswith("This proposal expired")
    assert annotate_closed_proposals(HISTORY, [still_open], now) == HISTORY


@pytest.mark.asyncio
async def test_later_turn_is_sent_with_the_declined_note(monkeypatch):
    service = ChatService.__new__(ChatService)
    service.user_id = "user-1"
    service.settings_service = SimpleNamespace(get_snapshot=AsyncMock(return_value={"constraints": []}))
    service.command_executor = SimpleNamespace(create_proposal=AsyncMock(), execute=AsyncMock())

    class Proposals:
        def select(self, *args, **kwargs):
            return self

        def eq(self, *args, **kwargs):
            return self

        def order(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def execute(self):
            return SimpleNamespace(data=[LEAVE])

    service.db = SimpleNamespace(client=SimpleNamespace(table=lambda name: Proposals()))

    async def save(role, content, command_id=None):
        return {"id": "m-dentist", "role": role, "content": content}

    service._save_message = save
    service._get_calendar_snapshot = AsyncMock(return_value={
        "text": "calendar", "count": 0, "start": None, "end": None, "counts": {},
    })
    service._get_logs_and_incidents = AsyncMock(return_value="logs")
    service.get_history = AsyncMock(return_value=list(HISTORY))
    monkeypatch.setattr(chat_service, "projection_for_user", AsyncMock(return_value={
        "cycle": None, "constraints": [], "leave_blocks": [],
    }))
    captured = {}

    async def fake_complete(**kwargs):
        captured.update(kwargs)
        return ("I'll add the dentist appointment.", {
            "name": "add_commitment",
            "input": {
                "name": "Dentist appointment",
                "type": "personal",
                "start_date": "2026-10-27",
                "end_date": "2026-10-27",
                "time_start": "15:00",
                "time_end": "16:00",
            },
        })

    monkeypatch.setattr(chat_service, "complete_chat", fake_complete)
    dentist = "Add a dentist appointment next Tuesday Oct 27 from 3pm to 4pm"
    result = await service.send_message(dentist, auto_execute=False)
    assert "Propose ONLY what the latest message asks." in captured["system"]
    earlier = "\n".join(message["content"] for message in captured["messages"][:-1])
    assert "The user declined this change (Mark Sat Oct 31 as leave). Do not re-propose it." in earlier
    assert captured["messages"][-1]["content"] == dentist
    assert result["command"]["action"] == "add_commitment"
    assert result["command"]["payload"]["name"] == "Dentist appointment"
    assert "leave" not in result["command"]["action"]


DECLINED_AT = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
NOW = DECLINED_AT + timedelta(hours=2)
DECLINED_OCT_27 = {
    "id": "proposal-oct-27",
    "message_id": "m-leave-27",
    "status": "declined",
    "created_at": "2026-10-20T12:00:00+00:00",
    "reviewed_at": DECLINED_AT.isoformat(),
    "command": {
        "action": "add_leave",
        "payload": {"type": "other", "date": "2026-10-27"},
        "explanation": "Mark Tue 27 Oct as leave",
    },
}


def test_same_action_and_dates_match_a_single_day_or_a_range():
    leave = {"start_date": "2026-10-27", "end_date": "2026-10-27", "type": "other"}
    assert repeats_declined_proposal(
        "Add a dentist appointment next Wednesday",
        "add_leave",
        leave,
        [DECLINED_OCT_27],
        now=NOW,
    )
    assert not repeats_declined_proposal(
        "Add a dentist appointment next Wednesday",
        "add_commitment",
        {"name": "Dentist", "type": "personal", "start_date": "2026-10-27", "end_date": "2026-10-27"},
        [DECLINED_OCT_27],
        now=NOW,
    )
    assert not repeats_declined_proposal(
        "Please mark 27 Oct as leave again",
        "add_leave",
        {"date": "2026-10-27"},
        [DECLINED_OCT_27],
        now=NOW,
    )
    approved = {**DECLINED_OCT_27, "status": "approved"}
    assert not repeats_declined_proposal(
        "Add a dentist appointment next Wednesday",
        "add_leave",
        {"date": "2026-10-27"},
        [approved],
        now=NOW,
    )


def test_a_cue_word_lets_a_relative_date_through():
    tool = {"type": "other", "date": "2026-10-27"}
    for message in (
        "mark next Tuesday as leave",
        "leave on the 27th",
        "day off 27/10",
    ):
        assert not repeats_declined_proposal(
            message, "add_leave", tool, [DECLINED_OCT_27], now=NOW
        ), message
    assert repeats_declined_proposal(
        "What about next Tuesday?",
        "add_leave",
        tool,
        [DECLINED_OCT_27],
        now=NOW,
    )


def test_only_declines_from_the_last_48_hours_are_blocked():
    tool = {"date": "2026-10-27"}
    assert repeats_declined_proposal(
        "Add a dentist appointment next Wednesday",
        "add_leave",
        tool,
        [DECLINED_OCT_27],
        now=DECLINED_AT + timedelta(hours=48),
    )
    assert not repeats_declined_proposal(
        "Add a dentist appointment next Wednesday",
        "add_leave",
        tool,
        [DECLINED_OCT_27],
        now=DECLINED_AT + timedelta(hours=48, seconds=1),
    )
    old = {
        **DECLINED_OCT_27,
        "created_at": "2026-01-01T00:00:00+00:00",
        "reviewed_at": (DECLINED_AT - timedelta(days=3)).isoformat(),
    }
    assert not repeats_declined_proposal(
        "Add a dentist appointment next Wednesday",
        "add_leave",
        tool,
        [old],
        now=NOW,
    )


def test_the_refusal_tells_the_user_how_to_ask_again():
    assert declined_again_reply(
        "add_leave", {"type": "other", "date": "2026-10-27"}
    ) == "You declined that change. If you still want it, say 'mark 27 Oct as leave'."
    assert declined_again_reply(
        "add_commitment",
        {"name": "Dentist appointment", "start_date": "2026-10-27", "end_date": "2026-10-27"},
    ) == (
        "You declined that change. If you still want it, say "
        "'add Dentist appointment on 27 Oct'."
    )


@pytest.mark.asyncio
async def test_declined_leave_is_dropped_unless_the_latest_message_asks(monkeypatch):
    service = ChatService.__new__(ChatService)
    service.user_id = "user-1"
    service.settings_service = SimpleNamespace(get_snapshot=AsyncMock(return_value={"constraints": []}))
    created = {}

    async def create_proposal(command, message_id=None):
        created["command"] = command
        return {"id": "new-proposal", "command": command}

    service.command_executor = SimpleNamespace(create_proposal=create_proposal, execute=AsyncMock())

    class Proposals:
        def select(self, *args, **kwargs):
            return self

        def eq(self, *args, **kwargs):
            return self

        def order(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def execute(self):
            reviewed = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            return SimpleNamespace(data=[{
                **DECLINED_OCT_27,
                "reviewed_at": reviewed,
            }])

    service.db = SimpleNamespace(client=SimpleNamespace(table=lambda name: Proposals()))

    async def save(role, content, command_id=None):
        return {"id": "m", "role": role, "content": content}

    service._save_message = save
    service._get_calendar_snapshot = AsyncMock(return_value={
        "text": "calendar", "count": 0, "start": None, "end": None, "counts": {},
    })
    service._get_logs_and_incidents = AsyncMock(return_value="logs")
    service.get_history = AsyncMock(return_value=[{"id": "m", "role": "user", "content": "hi"}])
    monkeypatch.setattr(chat_service, "projection_for_user", AsyncMock(return_value={
        "cycle": None, "constraints": [], "leave_blocks": [],
    }))
    monkeypatch.setattr(
        chat_service,
        "complete_chat",
        AsyncMock(return_value=("I'll mark Tue 27 Oct as leave.", {
            "name": "add_leave",
            "input": {"type": "other", "date": "2026-10-27"},
        })),
    )

    unrelated = await service.send_message(
        "Add a dentist appointment next Wednesday",
        auto_execute=False,
    )
    assert unrelated["proposal"] is None
    assert unrelated["is_command"] is False
    assert unrelated["command"] is None
    assert unrelated["response"] == (
        "You declined that change. If you still want it, say 'mark 27 Oct as leave'."
    )
    assert "command" not in created

    asked = await service.send_message(
        "Please mark 27 Oct as leave again",
        auto_execute=False,
    )
    assert asked["proposal"]["id"] == "new-proposal"
    assert asked["command"]["action"] == "add_leave"
    assert asked["command"]["payload"]["date"] == "2026-10-27"
