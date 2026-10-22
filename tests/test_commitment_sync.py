"""Creating or updating a commitment shows up on the day view."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from loguru import logger
from postgrest.exceptions import APIError

from app.database import Database
from datetime import date, timedelta

from app.engines.calendar_engine import CALENDAR_ENGINE_VERSION
from app.engines.command_executor import create_command_executor
from app.routes.calendar import get_day, get_year
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
        self.fail_rebuild = False
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

    async def get_calendar_days(self, user_id, start, end):
        return [day for day in self.days if start <= str(day["date"]) <= end]

    async def upsert_calendar_days(self, days):
        if self.fail_rebuild:
            raise RuntimeError("rebuild failed")
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
    assert created["calendar_pending"] is False
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
async def test_two_same_day_commitments_with_the_same_name_are_both_kept(monkeypatch):
    store = _Store()
    _install(monkeypatch, store)
    morning = CreateCommitmentRequest(
        name="Gym", type="personal", start_date="2026-10-27", end_date="2026-10-27", notes="9am",
    )
    afternoon = CreateCommitmentRequest(
        name="Gym", type="personal", start_date="2026-10-27", end_date="2026-10-27", notes="3pm",
    )

    first = await create_commitment(morning, USER)
    second = await create_commitment(afternoon, USER)

    assert first["data"]["id"] != second["data"]["id"]
    assert len(store.commitments) == 2
    assert {row["notes"] for row in store.commitments} == {"9am", "3pm"}
    assert _names( (await get_day("2026-10-27", USER))["data"] ) == ["Gym", "Gym"]


@pytest.mark.asyncio
async def test_a_new_create_does_not_change_a_paused_row(monkeypatch):
    store = _Store()
    store.commitments.append({
        "id": "paused-1",
        "user_id": "user-1",
        "name": "Gym",
        "type": "personal",
        "status": "paused",
        "source": "chat",
        "start_date": "2026-10-27",
        "end_date": "2026-10-27",
    })
    _install(monkeypatch, store)

    await create_commitment(
        CreateCommitmentRequest(
            name="Gym", type="personal", start_date="2026-10-27", end_date="2026-10-27",
        ),
        USER,
    )

    paused = next(row for row in store.commitments if row["id"] == "paused-1")
    assert paused["status"] == "paused"
    assert paused["source"] == "chat"
    assert len(store.commitments) == 2


@pytest.mark.asyncio
async def test_a_failed_settings_write_returns_500_and_the_same_key_finishes(monkeypatch):
    store = _Store()
    store.fail_updates = 1
    _install(monkeypatch, store)
    body = CreateCommitmentRequest(
        name="Gym", type="personal", start_date="2026-10-27", end_date="2026-10-27",
    )

    with pytest.raises(HTTPException) as failed:
        await create_commitment(body, USER, idempotency_key="abc")

    assert failed.value.status_code == 500
    assert len(store.commitments) == 1
    assert store.settings_row["settings"]["commitments"] == []
    assert store.settings_row["settings"].get("commitments_pending_add") in (None, [])

    finished = await create_commitment(body, USER, idempotency_key="abc")
    assert finished["calendar_pending"] is False
    assert finished["data"]["name"] == "Gym"
    assert finished["data"]["id"] == store.commitments[0]["id"]
    assert len(store.commitments) == 1
    assert _names((await get_day("2026-10-27", USER))["data"]) == ["Gym"]
    assert store.settings_row["settings"].get("commitments_pending_add") == []


@pytest.mark.asyncio
async def test_a_repeated_idempotency_key_with_a_different_body_is_rejected(monkeypatch):
    store = _Store()
    _install(monkeypatch, store)
    first = await create_commitment(
        CreateCommitmentRequest(
            name="Gym", type="personal", start_date="2026-10-27", end_date="2026-10-27",
        ),
        USER,
        idempotency_key="abc",
    )

    with pytest.raises(HTTPException) as rejected:
        await create_commitment(
            CreateCommitmentRequest(
                name="Renamed", type="personal", start_date="2026-11-01", end_date="2026-11-01",
            ),
            USER,
            idempotency_key="abc",
        )

    assert rejected.value.status_code == 422
    assert len(store.commitments) == 1
    assert store.commitments[0]["id"] == first["data"]["id"]
    assert store.commitments[0]["name"] == "Gym"


@pytest.mark.asyncio
async def test_a_client_id_must_be_a_uuid(monkeypatch):
    store = _Store()
    _install(monkeypatch, store)

    with pytest.raises(HTTPException) as rejected:
        await create_commitment(
            CreateCommitmentRequest(id="not-a-uuid", name="Gym", type="personal"),
            USER,
        )

    assert rejected.value.status_code == 422
    assert store.commitments == []

    created = await create_commitment(
        CreateCommitmentRequest(
            id=str(uuid4()),
            name="Gym",
            type="personal",
            start_date="2026-10-27",
            end_date="2026-10-27",
        ),
        USER,
    )
    assert created["success"] is True
    assert len(store.commitments) == 1


def _fill_year(store):
    current = date(2026, 1, 1)
    while current.year == 2026:
        store.days.append({
            "user_id": "user-1",
            "date": current.isoformat(),
            "cycle_id": "cycle-1",
            "cycle_day": 1,
            "work_type": "off",
            "state_json": {
                "engine_version": CALENDAR_ENGINE_VERSION,
                "manual_override": False,
                "is_leave": False,
                "commitments": [],
                "tags": [],
            },
        })
        current += timedelta(days=1)


@pytest.mark.asyncio
async def test_a_failed_create_rebuild_is_finished_by_the_year(monkeypatch):
    store = _Store()
    _fill_year(store)
    store.fail_rebuild = True
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
    assert created["success"] is True
    assert created["calendar_pending"] is True
    assert store.settings_row["settings"]["commitments_pending_add"] == [created["data"]["id"]]
    assert _names((await get_day("2026-10-27", USER))["data"]) == []

    store.fail_rebuild = False
    year = await get_year(2026, USER)
    assert year["success"] is True
    october = next(day for day in year["data"] if day["date"] == "2026-10-27")
    assert _names(october) == ["Dentist appointment"]
    assert store.settings_row["settings"]["commitments_pending_add"] == []


@pytest.mark.asyncio
async def test_a_failed_update_rebuild_is_finished_by_the_year(monkeypatch):
    store = _Store()
    _fill_year(store)
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
    assert created["calendar_pending"] is False

    store.fail_rebuild = True
    updated = await update_commitment(
        created["data"]["id"],
        UpdateCommitmentRequest(name="Haircut"),
        USER,
    )
    assert updated["success"] is True
    assert updated["calendar_pending"] is True
    assert store.settings_row["settings"]["commitments_pending_add"] == [created["data"]["id"]]
    assert _names((await get_day("2026-10-27", USER))["data"]) == ["Dentist appointment"]

    store.fail_rebuild = False
    year = await get_year(2026, USER)
    october = next(day for day in year["data"] if day["date"] == "2026-10-27")
    assert _names(october) == ["Haircut"]
    assert store.settings_row["settings"]["commitments_pending_add"] == []


def _chips(day):
    return (day or {}).get("state_json", {}).get("commitments") or []


@pytest.mark.asyncio
async def test_a_date_edit_rebuild_shows_one_chip_on_the_new_date(monkeypatch):
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
    commitment_id = created["data"]["id"]
    updated = await update_commitment(
        commitment_id,
        UpdateCommitmentRequest(start_date="2026-11-05", end_date="2026-11-05"),
        USER,
    )
    assert updated["calendar_pending"] is False
    assert _chips((await get_day("2026-10-27", USER))["data"]) == []
    moved = _chips((await get_day("2026-11-05", USER))["data"])
    assert [chip["name"] for chip in moved] == ["Dentist appointment"]

    # The settings copy can still describe the old span, as it did before
    # updates wrote that copy. The table row is the one that was edited.
    for item in store.settings_row["settings"]["commitments"]:
        if item.get("id") == commitment_id:
            item["start_date"] = "2026-10-27"
            item["end_date"] = "2026-10-27"
    wrote = await create_command_executor(store, USER["id"]).rebuild_calendar(
        date(2026, 10, 1), date(2026, 11, 30)
    )
    assert wrote is True
    assert _chips((await get_day("2026-10-27", USER))["data"]) == []
    again = _chips((await get_day("2026-11-05", USER))["data"])
    assert [chip["name"] for chip in again] == ["Dentist appointment"]
    assert [chip.get("commitment_id") for chip in again] == [commitment_id]


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
