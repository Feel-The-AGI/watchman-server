"""A failed commitment delete is finished by the next calendar read."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.engines.calendar_engine import CALENDAR_ENGINE_VERSION
from app.engines.command_executor import CommandExecutor
from app.routes.calendar import get_year


class _Settings:
    def __init__(self, store):
        self.store = store
        self.op = "select"
        self.payload = None

    def select(self, *args, **kwargs):
        self.op = "select"
        return self

    def eq(self, *args, **kwargs):
        return self

    def update(self, data):
        self.op = "update"
        self.payload = data
        return self

    def execute(self):
        row = self.store.settings_row
        if self.op == "update":
            row.update(self.payload)
        return SimpleNamespace(data=[row])


class _YearDB:
    def __init__(self, fail_read=False):
        self.fail_read = fail_read
        self.upserts = 0
        self.days = []
        current = date(2026, 1, 1)
        while current.year == 2026:
            state = {
                "engine_version": CALENDAR_ENGINE_VERSION,
                "manual_override": False,
                "is_leave": False,
                "commitments": [],
                "tags": [],
            }
            if current.isoformat() == "2026-10-27":
                state["commitments"] = [{
                    "commitment_id": "commitment-1",
                    "name": "Dentist appointment",
                }]
            self.days.append({
                "user_id": "user-1",
                "date": current.isoformat(),
                "cycle_id": "cycle-1",
                "cycle_day": 1,
                "work_type": "off",
                "state_json": state,
            })
            current += timedelta(days=1)
        self.settings_row = {
            "id": "settings-1",
            "user_id": "user-1",
            "version": 1,
            "updated_at": "2026-10-21T00:00:00+00:00",
            "settings": {
                "cycle": {
                    "id": "cycle-1",
                    "pattern": [{"label": "off", "duration": 1}],
                    "anchor_date": "2026-01-01",
                    "anchor_cycle_day": 1,
                    "total_days": 1,
                },
                "commitments": [],
                "leave_blocks": [],
                "constraints": [],
                "commitments_pending_removal": ["commitment-1"],
            },
        }
        self.client = SimpleNamespace(table=lambda name: _Settings(self))

    async def get_calendar_days(self, user_id, start, end):
        return [day for day in self.days if start <= day["date"] <= end]

    async def read_calendar_days(self, user_id, start, end):
        return await self.get_calendar_days(user_id, start, end)

    async def read_commitments(self, user_id):
        if self.fail_read:
            raise RuntimeError("commitments read failed")
        return []

    async def read_leave_blocks(self, user_id):
        return []

    async def get_active_constraints(self, user_id):
        return []

    async def upsert_calendar_days(self, days):
        self.upserts += 1
        by_date = {day["date"]: day for day in self.days}
        for day in days:
            by_date[day["date"]] = day
        self.days = list(by_date.values())
        return days


def _october(payload):
    return next(day for day in payload["data"] if day["date"] == "2026-10-27")


@pytest.mark.asyncio
async def test_opening_the_year_finishes_a_pending_removal(monkeypatch):
    database = _YearDB()
    monkeypatch.setattr("app.routes.calendar.Database", lambda use_admin=False: database)

    result = await get_year(2026, {"id": "user-1"})

    assert result["success"] is True
    assert database.upserts == 1
    assert database.settings_row["settings"]["commitments_pending_removal"] == []
    assert _october(result)["state_json"]["commitments"] == []


@pytest.mark.asyncio
async def test_a_failed_pending_rebuild_keeps_the_ids(monkeypatch):
    database = _YearDB(fail_read=True)
    monkeypatch.setattr("app.routes.calendar.Database", lambda use_admin=False: database)

    result = await get_year(2026, {"id": "user-1"})

    assert result["success"] is True
    assert database.upserts == 0
    assert database.settings_row["settings"]["commitments_pending_removal"] == ["commitment-1"]
    assert _october(result)["state_json"]["commitments"][0]["name"] == "Dentist appointment"


class _ProjectDB:
    def __init__(self, fail=None):
        self.fail = fail
        self.upserts = None
        self.commitments = [{
            "id": "gym",
            "name": "Gym",
            "type": "personal",
            "status": "active",
            "start_date": "2026-10-27",
            "end_date": "2026-10-27",
        }]
        self.days = [
            {
                "user_id": "user-1",
                "date": "2026-10-27",
                "work_type": "off",
                "state_json": {
                    "commitments": [
                        {"commitment_id": "gym", "name": "Gym"},
                        {"commitment_id": "gone", "name": "Dentist appointment"},
                    ],
                    "tags": [],
                    "is_leave": False,
                },
            },
            {
                "user_id": "user-1",
                "date": "2026-10-28",
                "work_type": "off",
                "state_json": {
                    "commitments": [
                        {"commitment_id": "gone", "name": "Dentist appointment"},
                    ],
                    "tags": [],
                    "is_leave": False,
                },
            },
        ]

    async def read_commitments(self, user_id):
        if self.fail == "commitments":
            raise RuntimeError("commitments read failed")
        return list(self.commitments)

    async def read_leave_blocks(self, user_id):
        if self.fail == "leaves":
            raise RuntimeError("leave read failed")
        return []

    async def read_calendar_days(self, user_id, start, end):
        return [day for day in self.days if start <= day["date"] <= end]

    async def upsert_calendar_days(self, days):
        self.upserts = days
        return days


def _executor(database):
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.db = database
    executor.user_id = "user-1"
    return executor


@pytest.mark.asyncio
async def test_projecting_saved_events_drops_a_deleted_commitment():
    database = _ProjectDB()
    await _executor(database)._project_saved_events()
    by_date = {day["date"]: day for day in database.upserts}
    october = [chip["name"] for chip in by_date["2026-10-27"]["state_json"]["commitments"]]
    assert october == ["Gym"]
    assert by_date["2026-10-28"]["state_json"]["commitments"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ["commitments", "leaves"])
async def test_a_failed_saved_event_read_writes_nothing(failed):
    database = _ProjectDB(fail=failed)
    with pytest.raises(RuntimeError):
        await _executor(database)._project_saved_events()
    assert database.upserts is None
