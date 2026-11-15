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


def test_remove_leave_card_names_every_range():
    from app.engines.proposal_effects import describe_command

    text = describe_command("remove_leave", {"ranges": [
        {"start_date": "2026-11-23", "end_date": "2026-11-23"},
        {"start_date": "2026-11-28", "end_date": "2026-11-29"},
    ]})
    assert text == "Remove leave on Mon 23 Nov and Sat 28 Nov to Sun 29 Nov"
    assert describe_command("remove_leave", {"all": True}) == "Cancel all upcoming leave"


def test_stats_count_a_leave_day_once():
    from app.engines.stats_engine import create_stats_engine

    days = [
        {"date": "2026-11-01", "work_type": "work_day", "state_json": {}},
        {"date": "2026-11-02", "work_type": "work_day", "state_json": {"is_leave": True}},
        {"date": "2026-11-03", "work_type": "work_night", "state_json": {"is_leave": True}},
        {"date": "2026-11-04", "work_type": "off", "state_json": {"is_leave": True}},
        {"date": "2026-11-05", "work_type": "off", "state_json": None},
    ]
    engine = create_stats_engine("user-1")
    year = engine.compute_yearly_stats(days, 2026)
    assert (year["total_work_days"], year["total_work_nights"], year["total_off_days"], year["total_leave_days"]) == (1, 0, 1, 3)
    assert year["total_work_days"] + year["total_work_nights"] + year["total_off_days"] + year["total_leave_days"] == year["total_days"]
    month = engine.compute_monthly_stats(days, 2026, 11)
    assert (month["work_days"], month["work_nights"], month["off_days"], month["leave_days"]) == (1, 0, 1, 3)


def test_free_stats_stop_at_the_horizon():
    from datetime import timedelta

    from app.routes.stats import _within_plan

    near = (date.today() + timedelta(days=10)).isoformat()
    far = (date.today() + timedelta(days=200)).isoformat()
    days = [{"date": near}, {"date": far}]
    assert _within_plan({"id": "u", "tier": "free"}, days) == [{"date": near}]
    assert _within_plan({"id": "u", "tier": "pro"}, days) == days


@pytest.mark.asyncio
async def test_a_rotation_longer_than_a_year_is_refused():
    store, executor = await _seeded()
    before = _days(store)
    out = await executor.execute({
        "action": "update_cycle",
        "payload": {"pattern": [{"label": "work_day", "duration": 999}, {"label": "off", "duration": 1}], "correction": True},
        "explanation": "x",
    })
    assert out["success"] is False
    assert "366" in out["error"]
    assert _days(store) == before


def test_browser_timezone_is_validated():
    from app.routes.chat import valid_timezone

    assert valid_timezone("Africa/Accra") == "Africa/Accra"
    assert valid_timezone(" America/Los_Angeles ") == "America/Los_Angeles"
    for bad in (None, "", "Not/AZone", "x" * 100, 5, "../../etc/passwd"):
        assert valid_timezone(bad) is None


def test_leave_questions_get_a_leave_answer():
    from app.engines.day_status import is_leave_question

    on_leave = {"label": "Sat 28 Nov", "work_type": "off", "on_leave": True}
    off = {"label": "Sat 5 Dec", "work_type": "off", "on_leave": False}
    assert answer_sentence(on_leave, about_leave=True) == "Yes. Sat 28 Nov is a leave day."
    assert answer_sentence(off, about_leave=True) == "No. Sat 5 Dec is not leave. It is off."
    assert answer_sentence(on_leave) == "No. Sat 28 Nov is leave, so you are not working."
    assert is_leave_question("Am I on leave on 28 November?")
    assert not is_leave_question("Am I working on 28 November?")


def test_a_rotation_change_past_the_free_horizon_is_a_planned_date():
    from datetime import timedelta

    from app.planning_horizon import horizon_violation, payload_dates

    far = (date.today() + timedelta(days=200)).isoformat()
    payload = {"pattern": [{"label": "work_day", "duration": 7}], "effective_from": far, "anchor_date": far}
    assert horizon_violation("free", payload_dates(payload)) is not None
    assert horizon_violation("pro", payload_dates(payload)) is None
    assert horizon_violation("free", payload_dates({"anchor_date": far})) is None


@pytest.mark.asyncio
async def test_undo_reports_what_it_undid():
    store, executor = await _seeded()
    await executor.execute({"action": "add_leave", "payload": {"name": "Trip", "date": "2026-06-01"}, "explanation": "Add Trip on Mon 1 Jun"})
    out = await executor.execute({"action": "undo", "payload": {}}, skip_validation=True)
    assert out["result"]["undone_explanation"] == "Add Trip on Mon 1 Jun"


class _Rows:
    def __init__(self, rows):
        self.rows = rows
        self.deleted = []
        self.created = []

    async def read_commitments(self, user_id):
        return list(self.rows)

    async def delete_commitment(self, commitment_id, user_id):
        self.deleted.append(commitment_id)
        self.rows = [row for row in self.rows if row["id"] != commitment_id]
        return True

    async def create_commitment(self, data):
        self.created.append(data)
        self.rows.append(data)
        return data


def _bare_executor(rows):
    from app.engines.command_executor import CommandExecutor

    executor = CommandExecutor.__new__(CommandExecutor)
    executor.user_id = "user-1"
    executor.db = _Rows(rows)
    return executor


@pytest.mark.asyncio
async def test_commitment_is_found_by_name():
    executor = _bare_executor([
        {"id": "a", "name": "Gym", "type": "personal"},
        {"id": "b", "name": "French class", "type": "education"},
        {"id": "c", "name": "Gym leave", "type": "leave"},
    ])
    assert await executor._commitment_id_by_name("gym") == "a"
    assert await executor._commitment_id_by_name("the french class") == "b"
    with pytest.raises(ValueError, match="no commitment called Yoga"):
        await executor._commitment_id_by_name("Yoga")
    with pytest.raises(ValueError, match="which commitment"):
        await executor._commitment_id_by_name("")


@pytest.mark.asyncio
async def test_two_matching_commitments_ask_which():
    executor = _bare_executor([
        {"id": "a", "name": "Gym morning", "type": "personal"},
        {"id": "b", "name": "Gym evening", "type": "personal"},
    ])
    with pytest.raises(ValueError, match="More than one"):
        await executor._commitment_id_by_name("gym")


@pytest.mark.asyncio
async def test_undo_puts_a_removed_commitment_back():
    row = {"id": "a", "name": "Gym", "type": "personal", "start_date": "2026-11-16"}
    executor = _bare_executor([])
    command = {
        "action": "remove_commitment",
        "before_state": {"commitments": [row]},
        "after_state": {"commitments": []},
    }
    await executor._reverse_commitment_removal(command)
    assert executor.db.created[0]["id"] == "a"
    assert executor.db.created[0]["user_id"] == "user-1"
    await executor._repeat_commitment_removal(command)
    assert executor.db.deleted == ["a"]
    await executor._reverse_commitment_removal({"action": "add_leave"})
    assert len(executor.db.created) == 1
