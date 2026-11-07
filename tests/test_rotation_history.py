"""A rotation change starts on a date and leaves earlier days alone.

Covers the pure history helpers, the executor end to end on an in-memory
store (change, correction, shift, leave rebuild, undo, redo, manual
overrides), single-day lookups, and other writers keeping the history.
"""

import copy
from datetime import date, timedelta

import pytest

from app.engines import rotation_history as rh
from app.engines.calendar_authority import work_type_on
from app.engines.command_executor import rotation_change_start
from app.engines.day_status import project_day
from app.engines.master_settings_service import keep_rotation_history
from tests.test_undo_leave import _Store, _executor

FOUR_FOUR = {
    "name": "4/4",
    "pattern": [{"label": "work_day", "duration": 4}, {"label": "off", "duration": 4}],
    "anchor_date": "2026-11-07",
    "anchor_cycle_day": 1,
    "total_days": 8,
}
TWO_TWO_THREE = [
    {"label": "work_day", "duration": 2},
    {"label": "work_night", "duration": 2},
    {"label": "off", "duration": 3},
]


def d(text):
    return date.fromisoformat(text)


# ---------------------------------------------------------------- pure helpers


def test_no_history_means_one_open_rotation():
    assert rh.history_of(FOUR_FOUR) == []
    assert rh.current_start(FOUR_FOUR) is None
    assert rh.rotation_on(FOUR_FOUR, d("2020-01-01"))["anchor_date"] == "2026-11-07"
    assert len(rh.spans(FOUR_FOUR)) == 1


def test_change_from_keeps_every_earlier_day_on_the_old_rotation():
    new = {"pattern": TWO_TWO_THREE, "anchor_date": "2026-11-16", "anchor_cycle_day": 1, "total_days": 7}
    cycle = rh.change_from(FOUR_FOUR, new, d("2026-11-16"))

    assert [entry["until"] for entry in cycle["history"]] == ["2026-11-15"]
    assert rh.current_start(cycle) == d("2026-11-16")
    assert rh.rotation_on(cycle, d("2026-11-15"))["anchor_date"] == "2026-11-07"
    assert rh.rotation_on(cycle, d("2026-11-16"))["anchor_date"] == "2026-11-16"
    assert rh.rotation_on(cycle, d("2025-01-01"))["anchor_date"] == "2026-11-07"
    # 4/4 from 7 Nov: 11-14 off, 15 back on days. From the 16th, 2-2-3.
    assert work_type_on(cycle, d("2026-11-14")) == "off"
    assert work_type_on(cycle, d("2026-11-15")) == "work_day"
    assert work_type_on(cycle, d("2026-11-16")) == "work_day"
    assert work_type_on(cycle, d("2026-11-18")) == "work_night"


def test_a_second_change_before_the_first_one_replaces_it():
    first = rh.change_from(
        FOUR_FOUR,
        {"pattern": TWO_TWO_THREE, "anchor_date": "2026-12-01", "anchor_cycle_day": 1},
        d("2026-12-01"),
    )
    second = rh.change_from(
        first,
        {"pattern": [{"label": "work_night", "duration": 7}, {"label": "off", "duration": 7}],
         "anchor_date": "2026-11-20", "anchor_cycle_day": 1},
        d("2026-11-20"),
    )
    assert [entry["until"] for entry in second["history"]] == ["2026-11-19"]
    assert second["history"][0]["anchor_date"] == "2026-11-07"
    assert work_type_on(second, d("2026-11-22")) == "work_night"
    assert work_type_on(second, d("2026-12-02")) == "off"


def test_a_later_change_stacks_on_an_earlier_one():
    first = rh.change_from(
        FOUR_FOUR,
        {"pattern": TWO_TWO_THREE, "anchor_date": "2026-12-01", "anchor_cycle_day": 1},
        d("2026-12-01"),
    )
    second = rh.change_from(
        first,
        {"pattern": [{"label": "work_day", "duration": 1}, {"label": "off", "duration": 1}],
         "anchor_date": "2027-01-04", "anchor_cycle_day": 1},
        d("2027-01-04"),
    )
    assert [entry["until"] for entry in second["history"]] == ["2026-11-30", "2027-01-03"]
    pieces = list(rh.split_range(second, d("2026-11-25"), d("2027-01-10")))
    assert [(a.isoformat(), b.isoformat()) for a, b, _ in pieces] == [
        ("2026-11-25", "2026-11-30"),
        ("2026-12-01", "2027-01-03"),
        ("2027-01-04", "2027-01-10"),
    ]


def test_restating_the_same_schedule_adds_no_history():
    same = {**FOUR_FOUR, "anchor_date": "2026-11-15", "anchor_cycle_day": 1}
    cycle = rh.change_from(FOUR_FOUR, same, d("2026-11-20"))
    assert cycle["history"] == []


def test_a_correction_keeps_earlier_rotations():
    changed = rh.change_from(
        FOUR_FOUR,
        {"pattern": TWO_TWO_THREE, "anchor_date": "2026-12-01", "anchor_cycle_day": 1},
        d("2026-12-01"),
    )
    fixed = rh.correction(changed, {"pattern": TWO_TWO_THREE, "anchor_date": "2026-12-01", "anchor_cycle_day": 3})
    assert fixed["history"] == changed["history"]
    assert rh.rotation_on(fixed, d("2026-12-01"))["anchor_cycle_day"] == 3


def test_split_range_inside_one_rotation_is_one_piece():
    cycle = rh.change_from(
        FOUR_FOUR,
        {"pattern": TWO_TWO_THREE, "anchor_date": "2026-12-01", "anchor_cycle_day": 1},
        d("2026-12-01"),
    )
    pieces = list(rh.split_range(cycle, d("2026-12-05"), d("2026-12-09")))
    assert len(pieces) == 1


def test_malformed_history_entries_are_ignored():
    cycle = {**FOUR_FOUR, "history": [
        "junk", {"until": "not a date", "pattern": TWO_TWO_THREE, "anchor_date": "2026-01-01"},
        {"until": "2026-01-01", "pattern": [], "anchor_date": "2026-01-01"},
    ]}
    assert rh.history_of(cycle) == []
    assert rh.rotation_on(cycle, d("2025-06-01"))["anchor_date"] == "2026-11-07"


def test_nested_anchor_and_type_days_blocks_are_read():
    legacy = {
        "pattern": [{"type": "day_shift", "days": 2}, {"type": "off", "days": 2}],
        "anchor": {"date": "2026-01-01", "cycle_day": 2},
    }
    flat = rh.flat_rotation(legacy)
    assert flat["pattern"] == [{"label": "work_day", "duration": 2}, {"label": "off", "duration": 2}]
    assert (flat["anchor_date"], flat["anchor_cycle_day"], flat["total_days"]) == ("2026-01-01", 2, 4)


@pytest.mark.parametrize("payload, expected", [
    ({"pattern": TWO_TWO_THREE}, "today"),
    ({"anchor_date": "2026-11-10", "anchor_cycle_day": 2}, "today"),
    ({"anchor_cycle_day": 3}, "today"),
    ({"pattern": TWO_TWO_THREE, "effective_from": "2026-12-01"}, "2026-12-01"),
    ({"pattern": TWO_TWO_THREE, "correction": True}, None),
    ({"pattern": TWO_TWO_THREE, "correction": "true"}, None),
    ({"shift_by_days": 1}, None),
    ({"shift_by_days": -2, "effective_from": "2026-11-20"}, "2026-11-20"),
    ({}, None),
])
def test_rotation_change_start(payload, expected):
    today = d("2026-11-07")
    got = rotation_change_start(payload, FOUR_FOUR, today)
    want = today if expected == "today" else (d(expected) if expected else None)
    assert got == want


def test_first_rotation_is_never_a_dated_change():
    assert rotation_change_start({"pattern": TWO_TWO_THREE}, None, d("2026-11-07")) is None
    assert rotation_change_start({"pattern": TWO_TWO_THREE}, {}, d("2026-11-07")) is None


def test_other_writers_keep_the_history():
    stored = {**FOUR_FOUR, "history": [{"until": "2026-01-01"}]}
    incoming = {**FOUR_FOUR, "anchor_cycle_day": 2}
    assert keep_rotation_history(stored, incoming)["history"] == [{"until": "2026-01-01"}]
    explicit = {**incoming, "history": []}
    assert keep_rotation_history(stored, explicit)["history"] == []
    assert "history" not in keep_rotation_history(FOUR_FOUR, incoming)
    assert keep_rotation_history(stored, None) is None


def test_project_day_uses_the_rotation_of_that_date():
    cycle = rh.change_from(
        FOUR_FOUR,
        {"pattern": TWO_TWO_THREE, "anchor_date": "2026-11-16", "anchor_cycle_day": 1},
        d("2026-11-16"),
    )
    assert project_day(cycle, d("2026-11-14"))["work_type"] == "off"
    assert project_day(cycle, d("2026-11-18"))["work_type"] == "work_night"


# ------------------------------------------------------- executor, end to end


def _days(store):
    return {row["date"]: row["work_type"] for row in store.tables["calendar_days"]}


def _settings_cycle(store):
    return store.tables["master_settings"][0]["settings"]["cycle"]


async def _seeded():
    """1 on 1 off from 2026-01-05, with the calendar written."""
    store = _Store()
    executor = _executor(store)
    assert await executor._regenerate_calendar() is True
    return store, executor


async def _run(executor, action, payload):
    return await executor.execute({"action": action, "payload": payload, "explanation": action})


@pytest.mark.asyncio
async def test_change_from_a_date_leaves_every_earlier_stored_day_alone():
    store, executor = await _seeded()
    before = _days(store)

    outcome = await _run(executor, "update_cycle", {
        "pattern": [{"label": "work_night", "duration": 2}, {"label": "off", "duration": 1}],
        "anchor_date": "2026-06-01", "anchor_cycle_day": 1, "effective_from": "2026-06-01",
    })
    assert outcome["success"] is True
    after = _days(store)
    earlier = {day: kind for day, kind in before.items() if day < "2026-06-01"}
    assert {day: after[day] for day in earlier} == earlier
    assert [after[day] for day in ("2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04")] == [
        "work_night", "work_night", "off", "work_night",
    ]
    assert [entry["until"] for entry in _settings_cycle(store)["history"]] == ["2026-05-31"]


@pytest.mark.asyncio
async def test_a_later_full_rebuild_still_respects_the_history():
    store, executor = await _seeded()
    before = _days(store)
    await _run(executor, "update_cycle", {
        "pattern": [{"label": "work_night", "duration": 2}, {"label": "off", "duration": 1}],
        "anchor_date": "2026-06-01", "anchor_cycle_day": 1, "effective_from": "2026-06-01",
    })
    # add_leave rewrites the whole calendar from 1 Jan.
    outcome = await _run(executor, "add_leave", {"name": "Trip", "type": "annual", "date": "2026-03-10"})
    assert outcome["success"] is True
    after = _days(store)
    assert all(after[day] == before[day] for day in before if day < "2026-06-01")
    assert after["2026-06-01"] == "work_night"


@pytest.mark.asyncio
async def test_undo_and_redo_of_a_rotation_change():
    store, executor = await _seeded()
    original = _days(store)
    await _run(executor, "update_cycle", {
        "pattern": [{"label": "work_night", "duration": 2}, {"label": "off", "duration": 1}],
        "effective_from": "2026-06-01",
    })
    changed = _days(store)
    assert changed != original

    undone = await executor.execute({"action": "undo", "payload": {}}, skip_validation=True)
    assert undone["success"] is True
    assert _days(store) == original
    assert not _settings_cycle(store).get("history")

    redone = await executor.execute({"action": "redo", "payload": {}}, skip_validation=True)
    assert redone["success"] is True
    assert _days(store) == changed


@pytest.mark.asyncio
async def test_a_new_pattern_with_no_anchor_starts_on_day_one_of_its_date():
    store, executor = await _seeded()
    await _run(executor, "update_cycle", {
        "pattern": [{"label": "off", "duration": 3}, {"label": "work_day", "duration": 2}],
        "effective_from": "2026-07-10",
    })
    cycle = _settings_cycle(store)
    assert (cycle["anchor_date"], cycle["anchor_cycle_day"]) == ("2026-07-10", 1)
    days = _days(store)
    assert [days[f"2026-07-{n}"] for n in (10, 11, 12, 13, 14)] == [
        "off", "off", "off", "work_day", "work_day",
    ]


@pytest.mark.asyncio
async def test_a_correction_rewrites_the_past_too():
    store, executor = await _seeded()
    await _run(executor, "update_cycle", {
        "pattern": [{"label": "work_day", "duration": 2}, {"label": "off", "duration": 2}],
        "anchor_date": "2026-01-05", "anchor_cycle_day": 1, "correction": True,
    })
    days = _days(store)
    assert [days[f"2026-01-0{n}"] for n in (5, 6, 7, 8)] == ["work_day", "work_day", "off", "off"]
    assert _settings_cycle(store)["history"] == []


@pytest.mark.asyncio
async def test_a_bare_shift_fixes_the_whole_calendar():
    store, executor = await _seeded()
    assert _days(store)["2026-01-05"] == "work_day"
    await _run(executor, "update_cycle", {"shift_by_days": 1})
    days = _days(store)
    assert days["2026-01-05"] == "off"
    assert days["2026-01-06"] == "work_day"


@pytest.mark.asyncio
async def test_a_shift_from_a_date_moves_only_the_days_after_it():
    store, executor = await _seeded()
    before = _days(store)
    await _run(executor, "update_cycle", {"shift_by_days": 1, "effective_from": "2026-08-01"})
    after = _days(store)
    assert all(after[day] == before[day] for day in before if day < "2026-08-01")
    assert after["2026-08-01"] != before["2026-08-01"]


@pytest.mark.asyncio
async def test_a_manual_override_in_the_past_survives_a_rotation_change():
    store, executor = await _seeded()
    await _run(executor, "override_days", {
        "start_date": "2026-03-02", "end_date": "2026-03-02",
        "work_type": "blank", "preserve_off_days": False,
    })
    await _run(executor, "update_cycle", {
        "pattern": [{"label": "work_night", "duration": 1}, {"label": "off", "duration": 1}],
        "effective_from": "2026-02-01",
    })
    assert _days(store)["2026-03-02"] == "blank"


@pytest.mark.asyncio
async def test_a_rest_write_without_history_does_not_drop_it():
    store, executor = await _seeded()
    await _run(executor, "update_cycle", {
        "pattern": [{"label": "work_night", "duration": 2}, {"label": "off", "duration": 1}],
        "effective_from": "2026-06-01",
    })
    current = copy.deepcopy(_settings_cycle(store))
    current.pop("history")
    current["name"] = "Renamed in the editor"
    await executor.settings_service.update_section("user-1", "cycle", current)
    assert [entry["until"] for entry in _settings_cycle(store)["history"]] == ["2026-05-31"]


# -------------------------------------------------------------- cancel leave


def _leave_spans(store):
    return sorted((row["start_date"], row["end_date"]) for row in store.tables["leave_blocks"])


def _is_leave(store, day):
    row = next(row for row in store.tables["calendar_days"] if row["date"] == day)
    return bool((row.get("state_json") or {}).get("is_leave"))


@pytest.mark.asyncio
async def test_cancelling_one_day_keeps_the_rest_of_the_block_and_undo_restores_it():
    store, executor = await _seeded()
    await _run(executor, "add_leave", {
        "name": "Holiday", "type": "annual", "start_date": "2026-03-10", "end_date": "2026-03-14",
    })
    outcome = await _run(executor, "remove_leave", {"start_date": "2026-03-12", "end_date": "2026-03-12"})
    assert outcome["success"] is True
    assert _leave_spans(store) == [("2026-03-10", "2026-03-11"), ("2026-03-13", "2026-03-14")]
    assert not _is_leave(store, "2026-03-12")
    assert _is_leave(store, "2026-03-11") and _is_leave(store, "2026-03-13")
    names = {row["name"] for row in store.tables["leave_blocks"]}
    assert names == {"Holiday"}

    await executor.execute({"action": "undo", "payload": {}}, skip_validation=True)
    assert _leave_spans(store) == [("2026-03-10", "2026-03-14")]
    assert _is_leave(store, "2026-03-12")

    await executor.execute({"action": "redo", "payload": {}}, skip_validation=True)
    assert _leave_spans(store) == [("2026-03-10", "2026-03-11"), ("2026-03-13", "2026-03-14")]
    assert not _is_leave(store, "2026-03-12")


@pytest.mark.asyncio
async def test_cancelling_a_range_that_covers_whole_blocks_removes_them():
    store, executor = await _seeded()
    await _run(executor, "add_leave", {"name": "A", "start_date": "2026-04-01", "end_date": "2026-04-02"})
    await _run(executor, "add_leave", {"name": "B", "start_date": "2026-04-05", "end_date": "2026-04-05"})
    await _run(executor, "add_leave", {"name": "Keep", "start_date": "2026-05-01", "end_date": "2026-05-01"})
    outcome = await _run(executor, "remove_leave", {"start_date": "2026-03-30", "end_date": "2026-04-06"})
    assert outcome["success"] is True
    assert _leave_spans(store) == [("2026-05-01", "2026-05-01")]


@pytest.mark.asyncio
async def test_cancelling_a_date_with_no_leave_is_no_change():
    store, executor = await _seeded()
    outcome = await _run(executor, "remove_leave", {"start_date": "2026-04-01", "end_date": "2026-04-01"})
    assert outcome["status"] == "no_change"
    assert outcome["changed"] is False


@pytest.mark.asyncio
async def test_a_reversed_cancel_range_is_read_in_order():
    store, executor = await _seeded()
    await _run(executor, "add_leave", {"name": "A", "start_date": "2026-04-01", "end_date": "2026-04-03"})
    outcome = await _run(executor, "remove_leave", {"start_date": "2026-04-03", "end_date": "2026-04-01"})
    assert outcome["success"] is True
    assert _leave_spans(store) == []


@pytest.mark.asyncio
async def test_free_accounts_book_leave_inside_their_horizon():
    store, executor = await _seeded()
    executor.effective_tier = "free"
    soon = (date.today() + timedelta(days=10)).isoformat()
    outcome = await _run(executor, "add_leave", {"name": "Trip", "type": "annual", "date": soon})
    assert outcome["success"] is True
    far = (date.today() + timedelta(days=200)).isoformat()
    blocked = await _run(executor, "add_leave", {"name": "Later", "type": "annual", "date": far})
    assert blocked["success"] is False
    assert blocked["code"] == "free_horizon"


# ------------------------------------------------- chat stamps the start date


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_input, expected", [
    ({"pattern": TWO_TWO_THREE}, "today"),
    ({"pattern": TWO_TWO_THREE, "effective_from": "2026-12-01"}, "2026-12-01"),
    ({"pattern": TWO_TWO_THREE, "correction": True}, None),
    ({"shift_by_days": 1}, None),
])
async def test_chat_stamps_effective_from_on_a_rotation_change(monkeypatch, tool_input, expected):
    from unittest.mock import AsyncMock

    from app.engines import chat_service as chat_module
    from tests.test_free_leave_chat import _install, _service

    service, created, _executed = _service()
    service.settings_service.get_snapshot = AsyncMock(return_value={"cycle": dict(FOUR_FOUR)})
    _install(monkeypatch, "", {"name": "update_cycle", "input": dict(tool_input)})
    monkeypatch.setattr(chat_module, "local_today", lambda *_args, **_kwargs: d("2026-11-07"))

    await service.send_message("Change my rotation", effective_tier="pro")

    command = created.await_args.args[0]
    want = "2026-11-07" if expected == "today" else expected
    assert command["payload"].get("effective_from") == want


# ------------------------------------------- stale "leave is Pro" history


@pytest.mark.parametrize("text", [
    "Leave planning is a Pro feature. Upgrade to Pro to plan vacation and time off.",
    "I can't book that. Leave booking is a Pro feature, so nothing has been added to your calendar. Upgrading to Pro would let me set it up for you.",
    "I can't book that either. Leave booking is a Pro feature, so nothing has been added for 20 March 2027.",
])
def test_old_leave_refusals_are_withdrawn_from_model_history(text):
    from app.engines.claude_chat import STALE_LEAVE_NOTE, history_to_messages

    history = [
        {"role": "user", "content": "Book leave"},
        {"role": "assistant", "content": text},
        {"role": "user", "content": "Book annual leave"},
    ]
    messages = history_to_messages(list(reversed(history)), "Book annual leave")
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert "pro feature" not in assistant["content"].lower()
    assert STALE_LEAVE_NOTE in assistant["content"]


def test_unrelated_pro_replies_are_kept():
    from app.engines.claude_chat import without_stale_leave_refusal

    text = "Exports are a Pro feature. Your next off day is Wed 11 Nov."
    assert without_stale_leave_refusal(text) == text


def test_the_v2_prompt_says_leave_is_on_every_plan():
    from app.prompts.registry import load_version

    text = load_version("chat-v2").text
    assert "Never say leave or leave booking is a Pro feature" in text


# ------------------------------------------------ whole-calendar correction


def test_whole_calendar_correction_drops_earlier_rotations():
    changed = rh.change_from(
        FOUR_FOUR,
        {"pattern": TWO_TWO_THREE, "anchor_date": "2026-12-01", "anchor_cycle_day": 1},
        d("2026-12-01"),
    )
    fixed = rh.correction(changed, {**FOUR_FOUR, "anchor_cycle_day": 2}, whole_calendar=True)
    assert fixed["history"] == []
    assert rh.rotation_on(fixed, d("2025-01-01"))["anchor_cycle_day"] == 2


@pytest.mark.parametrize("payload", [
    {"pattern": TWO_TWO_THREE, "whole_calendar": True},
    {"pattern": TWO_TWO_THREE, "correction": True, "whole_calendar": True, "effective_from": "2026-12-01"},
])
def test_whole_calendar_is_never_a_dated_change(payload):
    from app.engines.command_executor import explicit_change_start

    assert rotation_change_start(payload, FOUR_FOUR, d("2026-11-07")) is None
    assert explicit_change_start(payload, FOUR_FOUR) is None


@pytest.mark.asyncio
async def test_a_whole_calendar_correction_rewrites_dates_before_an_earlier_change():
    store, executor = await _seeded()
    await _run(executor, "update_cycle", {
        "pattern": [{"label": "work_night", "duration": 2}, {"label": "off", "duration": 1}],
        "effective_from": "2026-06-01",
    })
    await _run(executor, "update_cycle", {
        "pattern": [{"label": "off", "duration": 2}, {"label": "work_day", "duration": 2}],
        "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
        "correction": True, "whole_calendar": True,
    })
    days = _days(store)
    assert [days[f"2026-01-0{n}"] for n in (5, 6, 7, 8)] == ["off", "off", "work_day", "work_day"]
    assert _settings_cycle(store)["history"] == []
    assert "work_night" not in set(days.values())


@pytest.mark.asyncio
async def test_a_plain_correction_keeps_an_earlier_change():
    store, executor = await _seeded()
    before = _days(store)
    await _run(executor, "update_cycle", {
        "pattern": [{"label": "work_night", "duration": 2}, {"label": "off", "duration": 1}],
        "effective_from": "2026-06-01",
    })
    await _run(executor, "update_cycle", {
        "pattern": [{"label": "work_night", "duration": 2}, {"label": "off", "duration": 1}],
        "anchor_date": "2026-06-01", "anchor_cycle_day": 2, "correction": True,
    })
    after = _days(store)
    assert all(after[day] == before[day] for day in before if day < "2026-06-01")
    assert after["2026-06-01"] == "work_night" and after["2026-06-02"] == "off"


def test_the_v2_prompt_books_leave_from_a_date_alone():
    from app.prompts.registry import load_version

    text = load_version("chat-v2").text
    assert "Booking leave needs only a date" in text
    assert "Do not ask what kind of leave it is" in text
