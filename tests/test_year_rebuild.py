"""GET /year keeps document leave, commitments, and manual overrides."""

from types import SimpleNamespace

import pytest

from app.engines.calendar_engine import CALENDAR_ENGINE_VERSION
from app.routes.calendar import get_year


CYCLE = {
    "id": "cycle-1",
    "name": "14/14",
    "pattern": [
        {"label": "work_day", "duration": 14},
        {"label": "off", "duration": 14},
    ],
    "anchor_date": "2026-10-21",
    "anchor_cycle_day": 1,
    "total_days": 28,
}

COMMITMENT = {
    "id": "commitment-1",
    "user_id": "user-1",
    "name": "Dentist appointment",
    "type": "personal",
    "start_date": "2026-10-27",
    "end_date": "2026-10-27",
    "constraints_json": {},
    "recurrence": {},
}


class Store:
    def __init__(self):
        self.settings = {
            "cycle": CYCLE,
            "leave_blocks": [{
                "id": "leave-1",
                "name": "Booked leave",
                "start_date": "2026-11-02",
                "end_date": "2026-11-02",
            }],
            "commitments": [],
            "constraints": [],
        }
        self.commitments = [COMMITMENT]
        self.leaves = []
        self.deletes = 0
        self.upserts = 0
        self.days = [
            {
                "user_id": "user-1",
                "date": "2026-01-01",
                "cycle_id": "cycle-1",
                "cycle_day": 20,
                "work_type": "blank",
                "state_json": {
                    "manual_override": True,
                    "engine_version": 2,
                    "is_leave": False,
                    "commitments": [],
                    "tags": [],
                },
            },
            {
                "user_id": "user-1",
                "date": "2026-10-27",
                "cycle_id": "cycle-1",
                "cycle_day": 7,
                "work_type": "work_day",
                "state_json": {
                    "engine_version": 2,
                    "is_leave": False,
                    "commitments": [{
                        "commitment_id": "commitment-1",
                        "name": "Dentist appointment",
                        "type": "personal",
                    }],
                    "tags": [],
                },
            },
            {
                "user_id": "user-1",
                "date": "2026-11-02",
                "cycle_id": "cycle-1",
                "cycle_day": 13,
                "work_type": "work_day",
                "state_json": {
                    "engine_version": 2,
                    "is_leave": True,
                    "commitments": [],
                    "tags": ["leave"],
                },
            },
        ]


STORE = Store()


class Query:
    def __init__(self, table):
        self.table = table

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def insert(self, data):
        self._insert = data
        return self

    def execute(self):
        if self.table == "master_settings":
            return SimpleNamespace(data=[{
                "id": "settings-1",
                "user_id": "user-1",
                "settings": STORE.settings,
                "version": 1,
                "updated_at": "2026-10-21T00:00:00+00:00",
            }])
        return SimpleNamespace(data=[])


class FakeDatabase:
    def __init__(self, use_admin=False):
        self.client = SimpleNamespace(table=lambda name: Query(name))

    async def get_calendar_days(self, user_id, start, end):
        rows = [day for day in STORE.days if start <= day["date"] <= end]
        return sorted(rows, key=lambda day: day["date"])

    async def read_calendar_days(self, user_id, start, end):
        return await self.get_calendar_days(user_id, start, end)

    async def get_leave_blocks(self, user_id):
        return list(STORE.leaves)

    async def get_commitments(self, user_id):
        return list(STORE.commitments)

    async def get_active_cycle(self, user_id):
        return None

    async def delete_calendar_days(self, user_id, start, end):
        STORE.deletes += 1
        STORE.days = [day for day in STORE.days if not (start <= day["date"] <= end)]
        return True

    async def upsert_calendar_days(self, days):
        STORE.upserts += 1
        by_date = {day["date"]: day for day in STORE.days}
        for day in days:
            by_date[day["date"]] = day
        STORE.days = list(by_date.values())
        return days


def _day(payload, day):
    return next(item for item in payload["data"] if item["date"] == day)


@pytest.mark.asyncio
async def test_failed_rebuild_returns_the_stored_days(monkeypatch):
    stored = [{
        "user_id": "user-1",
        "date": "2026-01-01",
        "cycle_id": "cycle-1",
        "cycle_day": 20,
        "work_type": "blank",
        "state_json": {
            "manual_override": True,
            "engine_version": 2,
            "is_leave": False,
            "commitments": [],
            "tags": [],
        },
    }]

    class StoredDays:
        def __init__(self, use_admin=False):
            pass

        async def get_calendar_days(self, user_id, start, end):
            return list(stored)

    async def fail_rebuild(self, start_date, end_date):
        raise RuntimeError("upsert failed")

    monkeypatch.setattr("app.routes.calendar.Database", StoredDays)
    monkeypatch.setattr(
        "app.engines.command_executor.CommandExecutor.rebuild_calendar",
        fail_rebuild,
    )
    result = await get_year(2026, {"id": "user-1"})
    assert result["success"] is True
    assert result["year"] == 2026
    assert result["count"] == 1
    day = result["data"][0]
    assert day["date"] == "2026-01-01"
    assert day["work_type"] == "blank"
    assert day["state_json"]["manual_override"] is True
    assert day["state_json"]["engine_version"] == 2


@pytest.mark.asyncio
async def test_v2_year_keeps_document_leave_override_and_commitment(monkeypatch):
    monkeypatch.setattr("app.routes.calendar.Database", FakeDatabase)
    first = await get_year(2026, {"id": "user-1"})
    blank = _day(first, "2026-01-01")
    dentist = _day(first, "2026-10-27")
    leave = _day(first, "2026-11-02")

    assert blank["work_type"] == "blank"
    assert blank["state_json"]["manual_override"] is True
    assert blank["state_json"]["engine_version"] == CALENDAR_ENGINE_VERSION
    assert "block_day" in blank["state_json"]
    assert "shift_badge" in blank["state_json"]

    assert leave["state_json"]["is_leave"] is True
    assert leave["state_json"]["engine_version"] == CALENDAR_ENGINE_VERSION
    assert leave["shift_badge"].startswith("D")
    assert leave["block_day"] == leave["state_json"]["block_day"]

    names = [chip["name"] for chip in dentist["state_json"]["commitments"]]
    assert names == ["Dentist appointment"]
    assert dentist["state_json"]["engine_version"] == CALENDAR_ENGINE_VERSION
    assert dentist["shift_badge"].startswith("D")
    assert STORE.leaves == []

    upserts_after_first = STORE.upserts
    assert STORE.deletes == 0
    second = await get_year(2026, {"id": "user-1"})
    assert STORE.upserts == upserts_after_first
    assert STORE.deletes == 0
    assert _day(second, "2026-01-01")["work_type"] == "blank"
    assert _day(second, "2026-11-02")["state_json"]["is_leave"] is True
    assert [chip["name"] for chip in _day(second, "2026-10-27")["state_json"]["commitments"]] == [
        "Dentist appointment"
    ]
