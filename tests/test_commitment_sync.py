"""Creating or updating a commitment shows up on the day view."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from loguru import logger
from postgrest.exceptions import APIError

from app.database import Database
from app.routes.calendar import get_day
from app.routes.commitments import CreateCommitmentRequest, UpdateCommitmentRequest, create_commitment, update_commitment


class _Settings:
    def __init__(self, store):
        self.store = store
        self.op = "select"
        self.payload = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def update(self, data):
        self.op = "update"
        self.payload = data
        return self

    def execute(self):
        if self.op == "update" and self.store.fail_updates:
            self.store.fail_updates -= 1
            raise RuntimeError("settings write failed")
        row = self.store.settings_row
        if self.op == "update":
            row.update(self.payload)
        return SimpleNamespace(data=[row])


class _Store:
    def __init__(self):
        self.commitments = []
        self.days = []
        self.upserts = 0
        self.fail_updates = 0
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
            },
        }
        self.client = SimpleNamespace(table=lambda name: _Settings(self))

    async def get_commitments(self, user_id):
        return [row for row in self.commitments if row.get("user_id") == user_id]

    async def get_active_commitments(self, user_id):
        return [row for row in await self.get_commitments(user_id) if row.get("status") == "active"]

    async def get_commitment(self, commitment_id):
        for row in self.commitments:
            if row["id"] == commitment_id:
                return dict(row)
        return None

    async def create_commitment(self, data):
        row = dict(data)
        row.setdefault("id", str(uuid4()))
        self.commitments.append(row)
        return dict(row)

    async def update_commitment(self, commitment_id, data):
        for index, row in enumerate(self.commitments):
            if row["id"] == commitment_id:
                row.update(data)
                self.commitments[index] = row
                return dict(row)
        return None

    async def read_commitments(self, user_id):
        return await self.get_commitments(user_id)

    async def read_leave_blocks(self, user_id):
        return []

    async def get_active_constraints(self, user_id):
        return []

    async def read_calendar_days(self, user_id, start, end):
        return [day for day in self.days if start <= str(day["date"]) <= end]

    async def upsert_calendar_days(self, days):
        self.upserts += 1
        by_date = {day["date"]: day for day in self.days}
        for day in days:
            by_date[day["date"]] = day
        self.days = list(by_date.values())
        return days

    async def get_calendar_day(self, user_id, day):
        for row in self.days:
            if row.get("user_id") == user_id and str(row.get("date"))[:10] == day:
                return row
        return None


def _install(monkeypatch, store):
    database = lambda use_admin=False: store
    monkeypatch.setattr("app.routes.commitments.Database", database)
    monkeypatch.setattr("app.routes.calendar.Database", database)


def _names(day):
    chips = (day or {}).get("state_json", {}).get("commitments") or []
    return [chip.get("name") for chip in chips]


USER = {"id": "user-1", "tier": "pro", "settings": {}}


@pytest.mark.asyncio
async def test_the_day_shows_a_chip_after_create_and_update(monkeypatch):
    store = _Store()
    _install(monkeypatch, store)

    created = await create_commitment(
        CreateCommitmentRequest(
            name="Dentist appointment",
            type="personal",
            start_date="2026-10-27",
            end_date="2026-10-27",
        ),
        USER,
    )
    day = await get_day("2026-10-27", USER)
    assert _names(day["data"]) == ["Dentist appointment"]
    assert len(store.commitments) == 1
    assert store.settings_row["settings"]["commitments"][0]["name"] == "Dentist appointment"

    await update_commitment(
        created["data"]["id"],
        UpdateCommitmentRequest(name="Haircut"),
        USER,
    )
    updated = await get_day("2026-10-27", USER)
    assert _names(updated["data"]) == ["Haircut"]
    assert len(store.commitments) == 1


@pytest.mark.asyncio
async def test_creating_the_same_commitment_again_finishes_the_first_attempt(monkeypatch):
    store = _Store()
    store.fail_updates = 1
    _install(monkeypatch, store)
    body = CreateCommitmentRequest(
        name="Dentist appointment",
        type="personal",
        start_date="2026-10-27",
        end_date="2026-10-27",
    )

    with pytest.raises(Exception) as failed:
        await create_commitment(body, USER)
    assert getattr(failed.value, "status_code", None) == 500
    assert len(store.commitments) == 1
    assert store.settings_row["settings"]["commitments"] == []

    await create_commitment(body, USER)

    assert len(store.commitments) == 1
    day = await get_day("2026-10-27", USER)
    assert _names(day["data"]) == ["Dentist appointment"]


class _Missing:
    def table(self, name):
        return self

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def single(self):
        return self

    def execute(self):
        raise APIError({
            "message": "Cannot coerce the result to a single JSON object",
            "code": "PGRST116",
            "details": "The result contains 0 rows",
            "hint": None,
        })


class _Broken:
    def table(self, name):
        return self

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def single(self):
        return self

    def execute(self):
        raise APIError({
            "message": "permission denied",
            "code": "42501",
            "details": None,
            "hint": None,
        })


def _capture_logs():
    records = []

    def sink(message):
        records.append(message.record)

    handle = logger.add(sink, level="DEBUG")
    return records, handle


@pytest.mark.asyncio
async def test_a_missing_commitment_row_is_not_an_error():
    database = Database.__new__(Database)
    database.client = _Missing()
    records, handle = _capture_logs()
    try:
        assert await database.get_commitment("missing") is None
    finally:
        logger.remove(handle)
    assert any(record["level"].name == "INFO" for record in records)
    assert not any(record["level"].name == "ERROR" for record in records)


@pytest.mark.asyncio
async def test_a_real_commitment_read_error_is_logged():
    database = Database.__new__(Database)
    database.client = _Broken()
    records, handle = _capture_logs()
    try:
        assert await database.get_commitment("missing") is None
    finally:
        logger.remove(handle)
    assert any(record["level"].name == "ERROR" for record in records)
