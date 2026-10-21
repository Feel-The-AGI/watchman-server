"""A date question is one sentence from the cycle, not from the stored sample."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.engines import chat_service
from app.engines.chat_service import SYSTEM_PROMPT, ChatService
from app.engines.day_status import answer_sentence, project_day, schedule_question_date


FOURTEEN = {
    "pattern": [
        {"label": "work_day", "duration": 14},
        {"label": "off", "duration": 14},
    ],
    "anchor_date": "2026-10-21",
    "anchor_cycle_day": 1,
    "cycle_length": 28,
}


def test_christmas_is_a_day_shift_past_the_sample():
    # 21 Oct + 65 days is 25 Dec, cycle day 10 of a 14-on block.
    status = project_day(FOURTEEN, date(2026, 12, 25))
    assert status["work_type"] == "work_day"
    assert status["cycle_day"] == 10
    assert answer_sentence(status) == "Yes. Fri 25 Dec is a day shift."
    assert "20" not in answer_sentence(status)
    asked = schedule_question_date("Am I working on Christmas Day?", date(2026, 10, 21))
    assert asked == date(2026, 12, 25)


def test_leave_and_a_day_before_the_anchor_have_one_answer():
    on_leave = project_day(
        FOURTEEN,
        date(2026, 12, 25),
        leaves=[{"name": "Christmas", "start_date": "2026-12-24", "end_date": "2026-12-26"}],
    )
    assert answer_sentence(on_leave) == "No. Fri 25 Dec is leave, so you are not working."
    before = project_day(FOURTEEN, date(2026, 10, 20))
    assert before["work_type"] == "off"
    assert before["cycle_day"] == 28
    assert answer_sentence(before) == "No. Tue 20 Oct is off."


def test_logs_notes_and_statements_are_not_answered_as_questions():
    today = date(2026, 10, 21)
    assert schedule_question_date("Log overtime on 2026-12-25", today) is None
    assert schedule_question_date("Note that I was working on Christmas Day", today) is None
    assert schedule_question_date("Record an incident on 2026-10-27", today) is None
    assert schedule_question_date("Report overtime on Christmas", today) is None
    assert schedule_question_date("I was working on Christmas Day", today) is None
    assert schedule_question_date("Am I working on Christmas Day?", today) == date(2026, 12, 25)


def test_prompt_does_not_treat_the_sample_as_the_end_of_the_schedule():
    assert "not the end of the rotation" in SYSTEM_PROMPT
    assert "query_calendar" in SYSTEM_PROMPT


def _service(monkeypatch, cycle):
    service = ChatService.__new__(ChatService)
    service.user_id = "user-1"
    service.db = SimpleNamespace()
    service.settings_service = SimpleNamespace(get_snapshot=AsyncMock(return_value={
        "cycle": cycle,
        "leave_blocks": [],
        "commitments": [],
        "constraints": [],
    }))
    service.command_executor = SimpleNamespace(create_proposal=AsyncMock(), execute=AsyncMock())

    async def save(role, content, command_id=None):
        return {"id": "m", "role": role, "content": content}

    service._save_message = save
    service._get_calendar_snapshot = AsyncMock(return_value={
        "text": "Sample around today from 2026-10-21 to 2026-12-20.",
        "count": 61,
        "start": "2026-10-21",
        "end": "2026-12-20",
        "counts": {"work_day": 30, "off": 31},
    })
    service._get_logs_and_incidents = AsyncMock(return_value="none")
    service.get_history = AsyncMock(return_value=[{"role": "user", "content": "question"}])
    monkeypatch.setattr(chat_service, "projection_for_user", AsyncMock(return_value={
        "cycle": cycle,
        "constraints": [],
        "leave_blocks": [],
    }))
    return service


@pytest.mark.asyncio
async def test_working_on_christmas_does_not_call_the_model_or_open_a_proposal(monkeypatch):
    service = _service(monkeypatch, FOURTEEN)

    async def fail_complete(**kwargs):
        raise AssertionError("the model must not invent a second answer")

    monkeypatch.setattr(chat_service, "complete_chat", fail_complete)
    result = await service.send_message("Am I working on 2026-12-25?", auto_execute=False)
    assert result["response"] == "Yes. Fri 25 Dec is a day shift."
    assert result["proposal"] is None
    assert result["is_command"] is False
    service.command_executor.create_proposal.assert_not_called()


@pytest.mark.asyncio
async def test_query_calendar_tool_is_the_whole_answer(monkeypatch):
    service = _service(monkeypatch, FOURTEEN)
    monkeypatch.setattr(
        chat_service,
        "complete_chat",
        AsyncMock(return_value=(
            "Yes, day shift. I can't confirm, the calendar only runs through Dec 20. Likely off.",
            {"name": "query_calendar", "input": {"date": "2026-12-25"}},
        )),
    )
    result = await service.send_message("Look ahead for me", auto_execute=False)
    assert result["response"] == "Yes. Fri 25 Dec is a day shift."
    assert "Dec 20" not in result["response"]
    assert result["proposal"] is None
    service.command_executor.create_proposal.assert_not_called()


@pytest.mark.asyncio
async def test_a_manual_override_is_answered_from_the_stored_day(monkeypatch):
    service = _service(monkeypatch, FOURTEEN)

    async def stored_day(user_id, day):
        assert day == "2026-12-25"
        return {
            "date": day,
            "work_type": "blank",
            "cycle_day": 10,
            "state_json": {
                "manual_override": True,
                "engine_version": 2,
                "is_leave": False,
                "commitments": [],
            },
        }

    service.db.get_calendar_day = stored_day

    async def fail_complete(**kwargs):
        raise AssertionError("the stored override is the answer")

    monkeypatch.setattr(chat_service, "complete_chat", fail_complete)
    result = await service.send_message("Am I working on 2026-12-25?", auto_execute=False)
    assert result["response"] == "No. Fri 25 Dec is untracked."
    assert result["proposal"] is None
