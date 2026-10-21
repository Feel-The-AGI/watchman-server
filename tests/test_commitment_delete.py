"""DELETE /api/commitments/{id} drops the settings copy and rebuilds."""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routes.commitments import (
    CreateCommitmentRequest,
    UpdateCommitmentRequest,
    create_commitment,
    delete_commitment,
    get_commitment,
    list_active_commitments,
    list_commitments,
    update_commitment,
)


class _Result:
    def __init__(self, data):
        self.data = data


class _Table:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self.filters = []
        self.payload = None
        self.op = "select"

    def select(self, *args, **kwargs):
        self.op = "select"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def insert(self, data):
        self.op = "insert"
        self.payload = data
        return self

    def update(self, data):
        self.op = "update"
        self.payload = data
        return self

    def execute(self):
        rows = self.store.tables.setdefault(self.name, [])
        if self.op == "insert":
            row = dict(self.payload)
            row.setdefault("id", str(uuid4()))
            row.setdefault("updated_at", "2026-10-21T00:00:00+00:00")
            rows.append(row)
            return _Result([row])
        matched = [row for row in rows if all(row.get(key) == value for key, value in self.filters)]
        if self.op == "update":
            if self.store.fail_updates:
                self.store.fail_updates -= 1
                raise RuntimeError("settings write failed")
            for row in matched:
                row.update(self.payload)
        return _Result(matched)


class _Store:
    def __init__(self):
        self.commitment = {
            "id": "commitment-1",
            "user_id": "user-1",
            "name": "Dentist appointment",
            "type": "personal",
            "status": "active",
            "start_date": "2026-10-27",
            "end_date": "2026-10-27",
        }
        self.commitments = [dict(self.commitment)]
        self.days = [{
            "user_id": "user-1",
            "date": "2026-10-27",
            "cycle_id": "cycle-1",
            "cycle_day": 1,
            "work_type": "off",
            "state_json": {
                "commitments": [{
                    "commitment_id": "commitment-1",
                    "name": "Dentist appointment",
                }],
                "tags": [],
                "is_leave": False,
            },
        }]
        self.upserts = 0
        self.fail_updates = 0
        self.fail_rebuild = False
        self.admin_calls = []
        self.tables = {
            "master_settings": [{
                "id": "settings-1",
                "user_id": "user-1",
                "version": 1,
                "updated_at": "2026-10-21T00:00:00+00:00",
                "settings": {
                    "cycle": {
                        "id": "cycle-1",
                        "pattern": [{"label": "off", "duration": 1}],
                        "anchor_date": "2026-10-21",
                        "anchor_cycle_day": 1,
                        "total_days": 1,
                    },
                    "commitments": [dict(self.commitment)],
                    "leave_blocks": [],
                    "constraints": [],
                },
            }],
        }
        self.client = type("Client", (), {"table": lambda _self, name: _Table(self, name)})()

    async def get_commitment(self, commitment_id):
        for row in self.commitments:
            if row["id"] == commitment_id:
                return dict(row)
        return None

    async def delete_commitment(self, commitment_id, user_id):
        kept = []
        removed = False
        for row in self.commitments:
            if row["id"] == commitment_id and row["user_id"] == user_id:
                removed = True
                continue
            kept.append(row)
        self.commitments = kept
        return removed

    async def get_commitments(self, user_id):
        return [row for row in self.commitments if row["user_id"] == user_id]

    async def read_commitments(self, user_id):
        return await self.get_commitments(user_id)

    async def get_leave_blocks(self, user_id):
        return []

    async def read_leave_blocks(self, user_id):
        return []

    async def get_active_constraints(self, user_id):
        return []

    async def read_calendar_days(self, user_id, start, end):
        return [day for day in self.days if start <= day["date"] <= end]

    async def upsert_calendar_days(self, days):
        if self.fail_rebuild:
            raise RuntimeError("rebuild failed")
        self.upserts += 1
        by_date = {day["date"]: day for day in self.days}
        for day in days:
            by_date[day["date"]] = day
        self.days = list(by_date.values())
        return days


def _settings(store):
    return store.tables["master_settings"][0]["settings"]


def _install(monkeypatch, store):
    def database(use_admin=False):
        store.admin_calls.append(use_admin)
        return store

    monkeypatch.setattr("app.routes.commitments.Database", database)


@pytest.mark.asyncio
async def test_delete_removes_the_settings_copy_and_rebuilds(monkeypatch):
    store = _Store()
    _install(monkeypatch, store)

    result = await delete_commitment("commitment-1", {"id": "user-1"})

    assert result["success"] is True
    assert store.commitments == []
    assert _settings(store)["commitments"] == []
    assert store.upserts == 1
    assert store.admin_calls == [True]
    day = next(row for row in store.days if row["date"] == "2026-10-27")
    assert day["state_json"]["commitments"] == []
    assert _settings(store)["commitments_pending_removal"] == []


@pytest.mark.asyncio
async def test_delete_clears_a_settings_copy_when_the_row_is_already_gone(monkeypatch):
    store = _Store()
    store.commitments = []
    _install(monkeypatch, store)

    result = await delete_commitment("commitment-1", {"id": "user-1"})

    assert result["success"] is True
    assert _settings(store)["commitments"] == []
    assert store.upserts == 1
    day = next(row for row in store.days if row["date"] == "2026-10-27")
    assert day["state_json"]["commitments"] == []


@pytest.mark.asyncio
async def test_settings_write_failure_can_be_retried(monkeypatch):
    store = _Store()
    store.fail_updates = 1
    _install(monkeypatch, store)

    with pytest.raises(HTTPException) as failed:
        await delete_commitment("commitment-1", {"id": "user-1"})
    assert failed.value.status_code == 500
    assert store.commitments
    assert _settings(store)["commitments"]
    assert store.upserts == 0

    result = await delete_commitment("commitment-1", {"id": "user-1"})

    assert result["success"] is True
    assert store.commitments == []
    assert _settings(store)["commitments"] == []
    assert store.upserts == 1


@pytest.mark.asyncio
async def test_rebuild_failure_can_be_retried(monkeypatch):
    store = _Store()
    store.fail_rebuild = True
    _install(monkeypatch, store)

    with pytest.raises(HTTPException) as failed:
        await delete_commitment("commitment-1", {"id": "user-1"})
    assert failed.value.status_code == 500
    assert store.commitments == []
    assert _settings(store)["commitments"] == []
    assert "commitment-1" in _settings(store)["commitments_pending_removal"]
    assert store.upserts == 0

    store.fail_rebuild = False
    result = await delete_commitment("commitment-1", {"id": "user-1"})

    assert result["success"] is True
    assert store.upserts == 1
    assert _settings(store)["commitments_pending_removal"] == []
    day = next(row for row in store.days if row["date"] == "2026-10-27")
    assert day["state_json"]["commitments"] == []


@pytest.mark.asyncio
async def test_delete_rejects_a_missing_or_foreign_commitment(monkeypatch):
    store = _Store()
    _install(monkeypatch, store)

    with pytest.raises(HTTPException) as missing:
        await delete_commitment("missing", {"id": "user-1"})
    assert missing.value.status_code == 404

    with pytest.raises(HTTPException) as foreign:
        await delete_commitment("commitment-1", {"id": "someone-else"})
    assert foreign.value.status_code == 403
    assert store.commitments
    assert _settings(store)["commitments"]
    assert store.upserts == 0
    assert store.admin_calls == [True, True]


@pytest.mark.asyncio
async def test_every_commitment_route_uses_the_admin_client_and_ownership(monkeypatch):
    flags = []
    calls = []

    class AdminDB:
        def __init__(self, use_admin=False):
            flags.append(use_admin)

        async def get_commitments(self, user_id):
            calls.append(("list", user_id))
            return [{
                "id": "mine",
                "user_id": user_id,
                "status": "active",
                "type": "personal",
            }]

        async def get_active_commitments(self, user_id):
            calls.append(("active", user_id))
            return []

        async def get_commitment(self, commitment_id):
            calls.append(("get", commitment_id))
            return {
                "id": commitment_id,
                "user_id": "someone-else",
                "name": "Theirs",
                "type": "personal",
                "status": "active",
            }

        async def create_commitment(self, data):
            calls.append(("create", data["user_id"]))
            return dict(data)

        async def update_commitment(self, commitment_id, data):
            raise AssertionError("must not update another user's commitment")

        async def delete_commitment(self, commitment_id, user_id):
            raise AssertionError("must not delete another user's commitment")

    monkeypatch.setattr("app.routes.commitments.Database", AdminDB)
    user = {"id": "user-1", "tier": "pro", "settings": {}}

    listed = await list_commitments(user=user)
    assert listed["data"][0]["user_id"] == "user-1"
    active = await list_active_commitments(user=user)
    assert active["count"] == 0

    with pytest.raises(HTTPException) as got:
        await get_commitment("theirs", user)
    assert got.value.status_code == 403

    with pytest.raises(HTTPException) as updated:
        await update_commitment("theirs", UpdateCommitmentRequest(name="Nope"), user)
    assert updated.value.status_code == 403

    with pytest.raises(HTTPException) as deleted:
        await delete_commitment("theirs", user)
    assert deleted.value.status_code == 403

    created = await create_commitment(
        CreateCommitmentRequest(name="Mine", type="personal"),
        user,
    )
    assert created["data"]["user_id"] == "user-1"
    assert flags == [True, True, True, True, True, True]
    assert ("list", "user-1") in calls
    assert ("active", "user-1") in calls
    assert ("create", "user-1") in calls
