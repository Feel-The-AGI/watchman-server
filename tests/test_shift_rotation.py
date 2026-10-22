"""update_cycle shift_by_days moves the rotation and leaves marked days in place."""

from datetime import date, timedelta

import pytest
from loguru import logger

from app.engines.calendar_engine import CalendarEngine
from app.engines.calendar_authority import engine_cycle
from app.engines.command_executor import CommandExecutor


ANCHOR = date(2026, 10, 22)
PATTERN = [
    {"label": "work_day", "duration": 5},
    {"label": "work_night", "duration": 5},
    {"label": "off", "duration": 5},
]
CYCLE = {
    "id": "cycle-1",
    "name": "5-5-5",
    "pattern": PATTERN,
    "anchor_date": ANCHOR.isoformat(),
    "anchor_cycle_day": 1,
    "total_days": 15,
}
OVERRIDE_DAY = "2026-10-24"
LEAVE_DAY = "2026-10-25"


class _Settings:
    def __init__(self, cycle):
        self.document = {
            "cycle": cycle,
            "leave_blocks": [{
                "id": "leave-1",
                "name": "Annual",
                "start_date": LEAVE_DAY,
                "end_date": LEAVE_DAY,
            }],
            "commitments": [],
            "constraints": [],
        }

    async def get(self, user_id):
        return {"settings": self.document}

    async def get_snapshot(self, user_id):
        return self.document

    async def update_section(self, user_id, section, value):
        self.document[section] = value
        return {"settings": self.document}


class _DB:
    def __init__(self):
        self.days = [{
            "user_id": "user-1",
            "date": OVERRIDE_DAY,
            "work_type": "work_night",
            "cycle_day": 3,
            "cycle_id": "cycle-1",
            "state_json": {
                "manual_override": True,
                "commitments": [],
                "is_leave": False,
            },
        }]

    async def read_leave_blocks(self, user_id):
        return []

    async def read_commitments(self, user_id):
        return []

    async def read_calendar_days(self, user_id, start, end):
        return [day for day in self.days if start <= day["date"] <= end]

    async def upsert_calendar_days(self, days):
        by_date = {day["date"]: day for day in self.days}
        for day in days:
            by_date[day["date"]] = day
        self.days = list(by_date.values())
        return days

    async def complete_onboarding_if_open(self, user_id):
        return False


def _executor(settings, db):
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.user_id = "user-1"
    executor.db = db
    executor.settings_service = settings
    return executor


def _projected(anchor: date, day: date):
    cycle = {**CYCLE, "anchor_date": anchor.isoformat()}
    generated = CalendarEngine("user").generate_range(
        day, day, engine_cycle(cycle), [],
    )[0]
    return generated.work_type.value, generated.cycle_day


def _stored(db, iso: str):
    return next(day for day in db.days if day["date"] == iso)


async def _apply(shift):
    settings = _Settings({**CYCLE, "pattern": [dict(block) for block in PATTERN]})
    db = _DB()
    executor = _executor(settings, db)
    saved = await executor._action_update_cycle({"shift_by_days": shift})
    await executor._regenerate_calendar()
    return settings, db, saved


@pytest.mark.asyncio
async def test_shifting_forward_one_day_moves_the_rotation():
    settings, db, saved = await _apply(1)
    moved = ANCHOR + timedelta(days=1)
    assert saved["cycle"]["anchor_date"] == moved.isoformat()
    assert settings.document["cycle"]["anchor_date"] == moved.isoformat()
    work_type, cycle_day = _projected(moved, date(2026, 10, 23))
    stored = _stored(db, "2026-10-23")
    assert stored["work_type"] == work_type == "work_day"
    assert stored["cycle_day"] == cycle_day == 1
    # The day that was day 1 is now the last day of the previous cycle.
    previous = _stored(db, "2026-10-22")
    assert previous["work_type"] == "off"
    assert previous["cycle_day"] == 15


@pytest.mark.asyncio
async def test_shifting_backward_one_day_moves_the_rotation():
    settings, db, saved = await _apply(-1)
    moved = ANCHOR - timedelta(days=1)
    assert saved["cycle"]["anchor_date"] == moved.isoformat()
    stored = _stored(db, "2026-10-22")
    work_type, cycle_day = _projected(moved, date(2026, 10, 22))
    assert stored["work_type"] == work_type == "work_day"
    assert stored["cycle_day"] == cycle_day == 2
    assert _stored(db, "2026-10-21")["cycle_day"] == 1


@pytest.mark.asyncio
async def test_shifting_by_a_whole_cycle_leaves_the_rotation_unchanged():
    settings, db, saved = await _apply(15)
    assert saved["cycle"]["anchor_date"] == (ANCHOR + timedelta(days=15)).isoformat()
    for iso, expected_day in (("2026-10-22", 1), ("2026-10-27", 6)):
        stored = _stored(db, iso)
        work_type, cycle_day = _projected(ANCHOR, date.fromisoformat(iso))
        assert stored["cycle_day"] == cycle_day == expected_day
        assert stored["work_type"] == work_type
    assert settings.document["cycle"]["pattern"] == PATTERN


@pytest.mark.asyncio
async def test_a_shift_keeps_an_override_and_a_leave_day():
    settings, db, saved = await _apply(1)
    moved = ANCHOR + timedelta(days=1)
    assert saved["cycle"]["anchor_date"] == moved.isoformat()
    override = _stored(db, OVERRIDE_DAY)
    assert override["work_type"] == "work_night"
    assert override["state_json"]["manual_override"] is True
    leave = _stored(db, LEAVE_DAY)
    assert leave["state_json"]["is_leave"] is True
    assert "leave" in leave["state_json"]["tags"]
    # The leave stays on that date. The shift underneath it still moves.
    work_type, cycle_day = _projected(moved, date.fromisoformat(LEAVE_DAY))
    assert leave["work_type"] == work_type
    assert leave["cycle_day"] == cycle_day
    assert settings.document["leave_blocks"][0]["start_date"] == LEAVE_DAY


async def _save(payload):
    settings = _Settings({**CYCLE, "pattern": [dict(block) for block in PATTERN]})
    saved = await _executor(settings, _DB())._action_update_cycle(payload)
    return saved


@pytest.mark.asyncio
async def test_a_nested_anchor_wins_over_a_shift():
    payload = {
        "anchor": {"date": "2026-11-01", "cycle_day": 1},
        "shift_by_days": 4,
    }
    saved, records = await _capture(_save(payload))
    assert saved["cycle"]["anchor_date"] == "2026-11-01"
    assert saved["cycle"]["anchor_cycle_day"] == 1
    assert any(
        "2026-11-01" in record["message"] and "4" in record["message"]
        for record in records
    )


@pytest.mark.asyncio
async def test_an_explicit_anchor_wins_over_a_shift():
    payload = {
        "anchor_date": "2026-11-01",
        "shift_by_days": 4,
        "name": "Renamed",
    }
    saved, records = await _capture(_save(payload))
    assert saved["cycle"]["anchor_date"] == "2026-11-01"
    assert saved["cycle"]["name"] == "Renamed"
    assert any(
        "2026-11-01" in record["message"] and "4" in record["message"]
        for record in records
    )


async def _capture(awaitable):
    records = []
    handle = logger.add(lambda message: records.append(message.record), level="WARNING")
    try:
        saved = await awaitable
    finally:
        logger.remove(handle)
    return saved, records


@pytest.mark.asyncio
@pytest.mark.parametrize("shift", [True, False, 1.5, "abc", 367, -367])
async def test_a_shift_that_cannot_be_applied_leaves_the_anchor(shift):
    saved, records = await _capture(_save({
        "shift_by_days": shift,
        "name": "Renamed",
    }))
    assert saved["cycle"]["anchor_date"] == ANCHOR.isoformat()
    assert saved["cycle"]["name"] == "Renamed"
    assert records


@pytest.mark.asyncio
@pytest.mark.parametrize("shift, days", [("2", 2), (366, 366)])
async def test_a_whole_day_shift_is_applied(shift, days):
    saved = await _save({"shift_by_days": shift})
    assert saved["cycle"]["anchor_date"] == (ANCHOR + timedelta(days=days)).isoformat()
