"""Fixes from the multi-tester pass: next-shift answers, yes/no wording,
cancelling several leave ranges, safe leave removal, and rotation guards."""

from datetime import date

import pytest

from app.engines.claude_chat import without_stale_leave_refusal
from app.engines.command_executor import _days_outside, _leave_ranges
from app.engines.day_status import (
    answer_sentence,
    is_yes_no_question,
    next_day_answer,
    next_kind_question,
)
from tests.test_rotation_history import _days, _run, _seeded

TODAY = date(2026, 11, 7)


@pytest.mark.parametrize("text, kind", [
    ("When is my next night shift?", "work_night"),
    ("when's my next night?", "work_night"),
    ("When is my next day off?", "off"),
    ("Which day is my next off day?", "off"),
    ("When is my next leave?", "leave"),
    ("When is my next shift?", "work"),
    ("When am I back at work?", "work"),
    ("When is my next day shift?", "work_day"),
    ("Book leave for my next day off", None),
    ("Am I working on 3 December?", None),
])
def test_next_kind_question(text, kind):
    assert next_kind_question(text) == kind


def _row(day, work_type, leave=False):
    return {"date": day, "work_type": work_type, "state_json": {"is_leave": leave}}


ROWS = [
    _row("2026-11-07", "work_night"),
    _row("2026-11-08", "work_day"),
    _row("2026-11-09", "off", leave=True),
    _row("2026-11-10", "off"),
    _row("2026-11-11", "work_night"),
]


def test_next_day_answer_skips_today_and_leave():
    assert next_day_answer("work_night", ROWS, TODAY) == "Your next night shift is Wed 11 Nov."
    assert next_day_answer("off", ROWS, TODAY) == "Your next day off is Tue 10 Nov."
    assert next_day_answer("leave", ROWS, TODAY) == "Your next leave day is Mon 9 Nov."
    assert next_day_answer("work", ROWS, TODAY) == "Your next shift is Sun 8 Nov (a day shift)."


def test_next_day_answer_when_none_found():
    assert next_day_answer("leave", ROWS[:2], TODAY).startswith("I can't see a leave day")


def test_yes_no_only_for_yes_no_questions():
    status = {"label": "Thu 3 Dec", "work_type": "work_day"}
    assert answer_sentence(status, yes_no=True) == "Yes. Thu 3 Dec is a day shift."
    assert answer_sentence(status, yes_no=False) == "Thu 3 Dec is a day shift."
    assert is_yes_no_question("Am I working on 3 Dec?")
    assert not is_yes_no_question("What shift am I on 3 December?")


def test_leave_ranges_reads_ranges_all_and_single():
    assert _leave_ranges({"start_date": "2026-11-05", "end_date": "2026-11-03"}) == [
        (date(2026, 11, 3), date(2026, 11, 5)),
    ]
    assert _leave_ranges({"ranges": [
        {"start_date": "2026-11-13", "end_date": "2026-11-13"},
        {"start_date": "2026-11-28", "end_date": "2026-11-29"},
        "junk",
    ]}) == [(date(2026, 11, 13), date(2026, 11, 13)), (date(2026, 11, 28), date(2026, 11, 29))]
    ((start, end),) = _leave_ranges({"all": True})
    assert start == date.today() and end.year == date.today().year + 50
    assert _leave_ranges({}) == []


def test_days_outside_several_cuts():
    pieces = _days_outside(date(2026, 3, 1), date(2026, 3, 10), [
        (date(2026, 3, 3), date(2026, 3, 3)),
        (date(2026, 3, 6), date(2026, 3, 7)),
    ])
    assert pieces == [
        (date(2026, 3, 1), date(2026, 3, 2)),
        (date(2026, 3, 4), date(2026, 3, 5)),
        (date(2026, 3, 8), date(2026, 3, 10)),
    ]
    assert _days_outside(date(2026, 3, 1), date(2026, 3, 2), [(date(2026, 2, 1), date(2026, 4, 1))]) == []


def _spans(store):
    return sorted((row["start_date"], row["end_date"]) for row in store.tables["leave_blocks"])


@pytest.mark.asyncio
async def test_cancel_several_separate_dates_in_one_message():
    store, executor = await _seeded()
    await _run(executor, "add_leave", {"name": "A", "start_date": "2026-03-10", "end_date": "2026-03-14"})
    await _run(executor, "add_leave", {"name": "B", "start_date": "2026-04-01", "end_date": "2026-04-01"})
    await _run(executor, "add_leave", {"name": "Keep", "start_date": "2026-05-01", "end_date": "2026-05-01"})
    out = await _run(executor, "remove_leave", {"ranges": [
        {"start_date": "2026-03-11", "end_date": "2026-03-11"},
        {"start_date": "2026-03-13", "end_date": "2026-03-13"},
        {"start_date": "2026-04-01", "end_date": "2026-04-01"},
    ]})
    assert out["success"] is True
    assert _spans(store) == [
        ("2026-03-10", "2026-03-10"), ("2026-03-12", "2026-03-12"),
        ("2026-03-14", "2026-03-14"), ("2026-05-01", "2026-05-01"),
    ]
    await executor.execute({"action": "undo", "payload": {}}, skip_validation=True)
    assert _spans(store) == [
        ("2026-03-10", "2026-03-14"), ("2026-04-01", "2026-04-01"), ("2026-05-01", "2026-05-01"),
    ]


@pytest.mark.asyncio
async def test_cancel_all_upcoming_leave_keeps_past_leave():
    store, executor = await _seeded()
    past = date(date.today().year - 1, 6, 1).isoformat()
    soon = date.fromordinal(date.today().toordinal() + 5).isoformat()
    later = date.fromordinal(date.today().toordinal() + 20).isoformat()
    for name, day in (("Past", past), ("Soon", soon), ("Later", later)):
        await _run(executor, "add_leave", {"name": name, "date": day})
    out = await _run(executor, "remove_leave", {"all": True})
    assert out["success"] is True
    assert _spans(store) == [(past, past)]


@pytest.mark.asyncio
async def test_undo_restores_leave_that_was_never_in_settings():
    store, executor = await _seeded()
    # A row written only to the table, as the REST leave route does.
    store.tables["leave_blocks"].append({
        "id": "rest-only", "user_id": "user-1", "name": "Trip",
        "start_date": "2026-06-01", "end_date": "2026-06-10", "effects": {"type": "annual"}, "notes": "",
    })
    await _run(executor, "remove_leave", {"start_date": "2026-06-05", "end_date": "2026-06-05"})
    assert _spans(store) == [("2026-06-01", "2026-06-04"), ("2026-06-06", "2026-06-10")]
    await executor.execute({"action": "undo", "payload": {}}, skip_validation=True)
    assert _spans(store) == [("2026-06-01", "2026-06-10")]
    await executor.execute({"action": "redo", "payload": {}}, skip_validation=True)
    assert _spans(store) == [("2026-06-01", "2026-06-04"), ("2026-06-06", "2026-06-10")]


@pytest.mark.asyncio
async def test_a_failed_remainder_insert_keeps_the_original_leave():
    store, executor = await _seeded()
    await _run(executor, "add_leave", {"name": "Trip", "start_date": "2026-06-01", "end_date": "2026-06-10"})
    real_create = store.create_leave_block
    calls = {"n": 0}

    async def flaky(data):
        calls["n"] += 1
        if calls["n"] == 2:
            return None
        return await real_create(data)

    store.create_leave_block = flaky
    out = await executor.execute({
        "action": "remove_leave",
        "payload": {"start_date": "2026-06-05", "end_date": "2026-06-05"},
        "explanation": "cancel",
    })
    assert out["success"] is False
    assert _spans(store) == [("2026-06-01", "2026-06-10")]


@pytest.mark.asyncio
async def test_an_empty_rotation_is_refused():
    store, executor = await _seeded()
    before = _days(store)
    out = await executor.execute({
        "action": "update_cycle", "payload": {"pattern": [], "correction": True}, "explanation": "x",
    })
    assert out["success"] is False
    assert _days(store) == before
    zero = await executor.execute({
        "action": "update_cycle",
        "payload": {"pattern": [{"label": "work_day", "duration": 0}], "correction": True},
        "explanation": "x",
    })
    assert zero["success"] is False


@pytest.mark.asyncio
async def test_a_failed_after_state_read_still_reports_the_saved_change():
    store, executor = await _seeded()
    real = executor.settings_service.get_snapshot
    real_log = executor._log_command
    seen = {}

    async def log_and_note(**kwargs):
        seen["after_state"] = kwargs.get("after_state")
        return await real_log(**kwargs)

    async def failing_after_write(user_id):
        # Reads after the calendar write (the after-state snapshot) fail.
        if seen.get("written"):
            raise RuntimeError("settings read timed out")
        return await real(user_id)

    real_regen = executor._regenerate_calendar

    async def regen_then_break():
        result = await real_regen()
        seen["written"] = True
        return result

    executor.settings_service.get_snapshot = failing_after_write
    executor._regenerate_calendar = regen_then_break
    executor._log_command = log_and_note
    out = await _run(executor, "add_leave", {"name": "Trip", "date": "2026-06-01"})
    assert out["success"] is True
    assert out.get("command_id")
    assert seen["after_state"] == {}


@pytest.mark.parametrize("text, kept", [
    ("Your leave days appear in the CSV export, which is a Pro feature.", True),
    ("Upgrade to Pro to export your data, including leave records.", True),
    ("The leave summary PDF is a Pro feature for exports.", True),
    ("Leave planning is a Pro feature. Upgrade to Pro to plan vacation and time off.", False),
    ("I can't book that. Leave booking is a Pro feature, so nothing has been added.", False),
])
def test_stale_leave_regex_is_narrow(text, kept):
    assert (without_stale_leave_refusal(text) == text) is kept
