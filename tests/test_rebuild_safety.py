"""A rebuild that cannot read or write must not touch stored overrides."""

from copy import deepcopy
from datetime import date, timedelta

import pytest

from app.engines.calendar_engine import CALENDAR_ENGINE_VERSION
from app.engines.command_executor import CommandExecutor


CYCLE = {
    "id": "cycle-1",
    "pattern": [
        {"label": "work_day", "duration": 1},
        {"label": "off", "duration": 1},
    ],
    "anchor_date": "2025-01-01",
    "anchor_cycle_day": 1,
    "total_days": 2,
}

START = date(2025, 1, 1)
END = date(2027, 12, 31)


class Settings:
    def __init__(self, commitments=None):
        self.snapshot = {
            "cycle": CYCLE,
            "leave_blocks": [],
            "commitments": list(commitments or []),
            "constraints": [],
        }

    async def get_snapshot(self, user_id):
        return self.snapshot


class RebuildDB:
    def __init__(
        self,
        days,
        fail_read=False,
        fail_upsert=False,
        fail_commitments=False,
        fail_leaves=False,
    ):
        self.days = days
        self.fail_read = fail_read
        self.fail_upsert = fail_upsert
        self.fail_commitments = fail_commitments
        self.fail_leaves = fail_leaves
        self.commitments = []
        self.leaves = []
        self.reads = []
        self.wrote = False
        self.deletes = 0

    async def get_leave_blocks(self, user_id):
        return list(self.leaves)

    async def read_leave_blocks(self, user_id):
        if self.fail_leaves:
            raise RuntimeError("leave read failed")
        return list(self.leaves)

    async def get_commitments(self, user_id):
        return list(self.commitments)

    async def read_commitments(self, user_id):
        if self.fail_commitments:
            raise RuntimeError("commitments read failed")
        return list(self.commitments)

    async def read_calendar_days(self, user_id, start, end):
        if self.fail_read:
            raise RuntimeError("read failed")
        self.reads.append((start, end))
        span = (date.fromisoformat(end) - date.fromisoformat(start)).days
        matched = [day for day in self.days if start <= day["date"] <= end]
        matched.sort(key=lambda day: day["date"])
        # A single read of the whole rebuild is past the 1,000 row cap.
        if span > 400:
            return matched[:1000]
        return matched

    async def upsert_calendar_days(self, days):
        if self.fail_upsert:
            raise RuntimeError("upsert failed")
        self.wrote = True
        by_date = {day["date"]: day for day in self.days}
        for day in days:
            by_date[day["date"]] = day
        self.days = list(by_date.values())
        return days

    async def delete_calendar_days(self, *args):
        self.deletes += 1
        raise AssertionError("rebuild must not delete calendar days")


def _executor(db, commitments=None):
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.db = db
    executor.user_id = "user-1"
    executor.settings_service = Settings(commitments)
    return executor


def _span(marker=None):
    days = []
    current = START
    while current <= END:
        days.append({
            "user_id": "user-1",
            "date": current.isoformat(),
            "work_type": "off",
            "cycle_day": 1,
            "cycle_id": "cycle-1",
            "state_json": {
                "engine_version": 2,
                "manual_override": False,
                "commitments": [],
                "is_leave": False,
            },
        })
        current += timedelta(days=1)
    if marker:
        days[marker]["work_type"] = "blank"
        days[marker]["state_json"] = {
            "engine_version": 2,
            "manual_override": True,
            "commitments": [],
            "is_leave": False,
        }
    return days


def _blank(days):
    return next(day for day in days if day["work_type"] == "blank")


@pytest.mark.asyncio
async def test_a_failed_read_does_not_write():
    original = _span(marker=-1)
    before = deepcopy(original)
    db = RebuildDB(original, fail_read=True)
    with pytest.raises(RuntimeError, match="read failed"):
        await _executor(db).rebuild_calendar(START, END)
    assert db.days == before
    assert db.wrote is False
    assert db.deletes == 0
    assert _blank(db.days)["date"] == "2027-12-31"


@pytest.mark.asyncio
async def test_a_failed_upsert_leaves_overrides_untouched():
    original = _span(marker=-1)
    before = deepcopy(original)
    db = RebuildDB(original, fail_upsert=True)
    with pytest.raises(RuntimeError, match="upsert failed"):
        await _executor(db).rebuild_calendar(START, END)
    assert db.days == before
    assert db.wrote is False
    assert db.deletes == 0
    assert _blank(db.days)["state_json"]["manual_override"] is True


@pytest.mark.asyncio
async def test_overrides_past_the_1000_row_cap_are_read_year_by_year():
    original = _span(marker=-1)
    assert len(original) > 1000
    assert original.index(_blank(original)) >= 1000
    db = RebuildDB(original)
    await _executor(db).rebuild_calendar(START, END)
    assert db.reads == [
        ("2025-01-01", "2025-12-31"),
        ("2026-01-01", "2026-12-31"),
        ("2027-01-01", "2027-12-31"),
    ]
    assert db.deletes == 0
    kept = _blank(db.days)
    assert kept["date"] == "2027-12-31"
    assert kept["work_type"] == "blank"
    assert kept["state_json"]["manual_override"] is True
    assert kept["state_json"]["engine_version"] == CALENDAR_ENGINE_VERSION


@pytest.mark.asyncio
async def test_a_failed_commitments_read_keeps_chips_and_writes_nothing():
    original = _span()
    original[0]["state_json"]["commitments"] = [{
        "commitment_id": "dentist",
        "name": "Dentist appointment",
    }]
    before = deepcopy(original)
    db = RebuildDB(original, fail_commitments=True)
    with pytest.raises(RuntimeError, match="commitments read failed"):
        await _executor(db).rebuild_calendar(START, END)
    assert db.wrote is False
    assert db.deletes == 0
    assert db.days == before
    assert db.days[0]["state_json"]["commitments"][0]["name"] == "Dentist appointment"


@pytest.mark.asyncio
async def test_a_failed_leave_read_keeps_chips_and_writes_nothing():
    original = _span()
    original[0]["state_json"]["commitments"] = [{
        "commitment_id": "dentist",
        "name": "Dentist appointment",
    }]
    original[0]["state_json"]["is_leave"] = True
    before = deepcopy(original)
    db = RebuildDB(original, fail_leaves=True)
    with pytest.raises(RuntimeError, match="leave read failed"):
        await _executor(db).rebuild_calendar(START, END)
    assert db.wrote is False
    assert db.deletes == 0
    assert db.days == before
    assert db.days[0]["state_json"]["is_leave"] is True
    assert db.days[0]["state_json"]["commitments"][0]["name"] == "Dentist appointment"


@pytest.mark.asyncio
async def test_paused_and_completed_commitments_are_not_stamped():
    day = date(2026, 10, 27).isoformat()
    original = [{
        "user_id": "user-1",
        "date": day,
        "work_type": "off",
        "cycle_day": 1,
        "cycle_id": "cycle-1",
        "state_json": {
            "engine_version": 2,
            "manual_override": False,
            "is_leave": False,
            "commitments": [
                {"commitment_id": "paused-1", "name": "Paused class"},
                {"commitment_id": "done-1", "name": "Finished course"},
            ],
        },
    }]
    db = RebuildDB(original)
    db.commitments = [
        {
            "id": "paused-1",
            "name": "Paused class",
            "type": "education",
            "status": "paused",
            "start_date": day,
            "end_date": day,
        },
        {
            "id": "done-1",
            "name": "Finished course",
            "type": "education",
            "status": "completed",
            "start_date": day,
            "end_date": day,
        },
        {
            "id": "active-1",
            "name": "Dentist appointment",
            "type": "personal",
            "status": "active",
            "start_date": day,
            "end_date": day,
        },
    ]
    # The settings copy of the paused class has no status. The table status wins.
    await _executor(db, commitments=[
        {
            "id": "paused-1",
            "name": "Paused class",
            "type": "education",
            "start_date": day,
            "end_date": day,
        },
        {
            "id": "legacy-1",
            "name": "Gym",
            "type": "personal",
            "start_date": day,
            "end_date": day,
        },
    ]).rebuild_calendar(date(2026, 10, 27), date(2026, 10, 27))
    names = {chip["name"] for chip in db.days[0]["state_json"]["commitments"]}
    assert names == {"Dentist appointment", "Gym"}
