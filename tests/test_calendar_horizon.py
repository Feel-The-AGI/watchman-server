"""Stored days include the rotation before the anchor, and badges are D1..N then OFF."""

from datetime import date, timedelta

from app.engines.calendar_authority import engine_cycle
from app.engines.calendar_engine import (
    CalendarEngine,
    block_day_for,
    generation_bounds,
    present_calendar_day,
    shift_badge,
    year_bounds,
)
from app.engines.day_status import chat_sample_end
from app.routes.calendar import _year_is_incomplete


FOURTEEN = {
    "id": "cycle-1",
    "pattern": [
        {"label": "work_day", "duration": 14},
        {"label": "off", "duration": 14},
    ],
    "anchor_date": "2026-10-21",
    "anchor_cycle_day": 1,
    "cycle_length": 28,
}


def _october():
    engine = CalendarEngine("user")
    days = engine.generate_range(
        date(2026, 10, 1),
        date(2026, 10, 31),
        engine_cycle(FOURTEEN),
        [],
    )
    return {day.date.isoformat(): day for day in days}


def test_days_before_the_anchor_match_the_onboarding_preview():
    # The preview projects October, including 1–20 Oct, for an anchor of 21 Oct.
    by_date = _october()
    assert by_date["2026-10-01"].work_type.value == "work_day"
    assert by_date["2026-10-06"].work_type.value == "work_day"
    assert by_date["2026-10-07"].work_type.value == "off"
    assert by_date["2026-10-20"].work_type.value == "off"
    assert by_date["2026-10-21"].work_type.value == "work_day"
    assert by_date["2026-10-31"].work_type.value == "work_day"
    assert by_date["2026-10-20"].cycle_day == 28
    assert by_date["2026-10-21"].cycle_day == 1


def test_shift_badges_run_d1_through_d14_then_off():
    engine = CalendarEngine("user")
    start = date(2026, 10, 21)
    days = engine.generate_range(
        start,
        start + timedelta(days=27),
        engine_cycle(FOURTEEN),
        [],
    )
    badges = [day.state_json["shift_badge"] for day in days]
    cycle_days = [day.cycle_day for day in days]
    assert badges == [f"D{n}" for n in range(1, 15)] + ["OFF"] * 14
    assert cycle_days == list(range(1, 29))
    assert days[0].state_json["block_day"] == 1
    assert days[13].state_json["block_day"] == 14
    assert days[14].state_json["block_day"] == 1
    assert days[14].work_type.value == "off"
    # The grid was showing D1–D5 because it used (cycle_day - 1) % 5 + 1.
    assert shift_badge("work_day", 6) == "D6"
    assert ((6 - 1) % 5) + 1 == 1


def test_a_night_block_numbers_from_n1_not_from_the_cycle_index():
    pattern = [
        {"label": "work_day", "duration": 5},
        {"label": "work_night", "duration": 5},
        {"label": "off", "duration": 5},
    ]
    assert block_day_for(6, pattern) == 1
    assert shift_badge("work_night", 1) == "N1"
    assert shift_badge("off", 1) == "OFF"


def test_horizon_includes_pre_anchor_days_and_the_next_year():
    start, end = generation_bounds(date(2026, 10, 21), date(2026, 10, 21))
    assert start == date(2026, 1, 1)
    assert end == date(2027, 12, 31)
    assert year_bounds(2026) == (date(2026, 1, 1), date(2026, 12, 31))
    # The chat sample is what made the model say the calendar ended on 20 Dec.
    assert chat_sample_end(date(2026, 10, 21)) == date(2026, 12, 20)
    assert end > date(2026, 12, 20)


def test_a_year_that_starts_at_the_anchor_is_incomplete():
    partial = [{"date": f"2026-12-{day:02d}"} for day in range(1, 32)]
    assert _year_is_incomplete(partial, 2026)
    assert not _year_is_incomplete([{"date": "x"}] * 365, 2026)


def test_present_calendar_day_exposes_the_badge_beside_cycle_day():
    day = _october()["2026-10-21"]
    shown = present_calendar_day({
        "date": "2026-10-21",
        "cycle_day": day.cycle_day,
        "work_type": day.work_type.value,
        "state_json": day.state_json,
    })
    assert shown["cycle_day"] == 1
    assert shown["block_day"] == 1
    assert shown["shift_badge"] == "D1"
