"""Undo removes the leave row a command created, and leave DELETE rebuilds."""

import asyncio
import copy

import pytest
from fastapi import HTTPException

from app.engines.command_executor import CommandExecutor
from app.routes.calendar import delete_leave_block
from app.routes.commands import RedoRequest, UndoRequest, redo_command, undo_command


ANCHOR = "2026-01-05"
SICK_DAY = "2026-01-05"
OTHER_DAY = "2026-01-07"
CYCLE = {
    "id": "cycle-1",
    "pattern": [
        {"label": "work_day", "duration": 1},
        {"label": "off", "duration": 1},
    ],
    "anchor_date": ANCHOR,
    "anchor_cycle_day": 1,
    "total_days": 2,
}


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self.filters = []
        self.payload = None
        self.op = "select"
        self._desc = False
        self._limit = None

    def select(self, *args, **kwargs):
        self.op = "select"
        return self

    def insert(self, data):
        self.op = "insert"
        self.payload = data
        return self

    def update(self, data):
        self.op = "update"
        self.payload = data
        return self

    def delete(self):
        self.op = "delete"
        return self

    def upsert(self, data, on_conflict=None):
        self.op = "upsert"
        self.payload = data
        return self

    def eq(self, key, value):
        self.filters.append(("eq", key, value))
        return self

    def gte(self, key, value):
        self.filters.append(("gte", key, value))
        return self

    def lte(self, key, value):
        self.filters.append(("lte", key, value))
        return self

    def order(self, key, desc=False):
        self._desc = bool(desc)
        self._order_key = key
        return self

    def limit(self, count):
        self._limit = count
        return self

    def _match(self, row):
        for op, key, value in self.filters:
            have = row.get(key)
            if op == "eq" and have != value:
                return False
            if op == "gte" and str(have) < str(value):
                return False
            if op == "lte" and str(have) > str(value):
                return False
        return True

    def execute(self):
        rows = self.store.tables.setdefault(self.name, [])
        if self.op == "insert":
            row = copy.deepcopy(self.payload)
            row.setdefault("id", f"row-{len(rows) + 1}")
            self.store.clock += 1
            row.setdefault("created_at", f"2026-01-01T00:00:{self.store.clock:02d}+00:00")
            row.setdefault("updated_at", row["created_at"])
            rows.append(row)
            return _Result([copy.deepcopy(row)])
        matched = [row for row in rows if self._match(row)]
        if self.op == "update":
            for row in matched:
                row.update(copy.deepcopy(self.payload))
            return _Result([copy.deepcopy(row) for row in matched])
        if self.op == "delete":
            self.store.tables[self.name] = [row for row in rows if row not in matched]
            return _Result([copy.deepcopy(row) for row in matched])
        if self.op == "upsert":
            saved = []
            for item in self.payload:
                incoming = copy.deepcopy(item)
                current = next((
                    row for row in rows
                    if row.get("user_id") == incoming.get("user_id")
                    and row.get("date") == incoming.get("date")
                ), None)
                if current is None:
                    rows.append(incoming)
                    saved.append(incoming)
                else:
                    current.clear()
                    current.update(incoming)
                    saved.append(current)
            return _Result([copy.deepcopy(row) for row in saved])
        if getattr(self, "_order_key", None):
            matched = sorted(matched, key=lambda row: str(row.get(self._order_key) or ""), reverse=self._desc)
        if self._limit is not None:
            matched = matched[:self._limit]
        return _Result([copy.deepcopy(row) for row in matched])


class _Store:
    def __init__(self, leaves=None, commitments=None):
        self.clock = 0
        self.tables = {
            "master_settings": [{
                "id": "settings-1",
                "user_id": "user-1",
                "version": 1,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "settings": {
                    "cycle": copy.deepcopy(CYCLE),
                    "work": {},
                    "constraints": [],
                    "commitments": [copy.deepcopy(row) for row in (commitments or [])],
                    "leave_blocks": [copy.deepcopy(row) for row in (leaves or [])],
                    "preferences": {},
                },
            }],
            "leave_blocks": [copy.deepcopy(row) for row in (leaves or [])],
            "commitments": [copy.deepcopy(row) for row in (commitments or [])],
            "command_log": [],
            "calendar_days": [],
        }
        self.client = type("Client", (), {"table": lambda _self, name: _Query(self, name)})()

    def _user_rows(self, name, user_id):
        return [row for row in self.tables[name] if row.get("user_id") == user_id]

    async def get_leave_blocks(self, user_id):
        return copy.deepcopy(self._user_rows("leave_blocks", user_id))

    async def read_leave_blocks(self, user_id):
        return copy.deepcopy(self._user_rows("leave_blocks", user_id))

    async def create_leave_block(self, data):
        result = _Query(self, "leave_blocks")
        return result.insert(data).execute().data[0]

    async def delete_leave_block(self, leave_id, user_id):
        result = _Query(self, "leave_blocks")
        return bool(result.delete().eq("id", leave_id).eq("user_id", user_id).execute().data)

    async def get_commitments(self, user_id):
        return copy.deepcopy(self._user_rows("commitments", user_id))

    async def read_commitments(self, user_id):
        return copy.deepcopy(self._user_rows("commitments", user_id))

    async def create_commitment(self, data):
        result = _Query(self, "commitments")
        return result.insert(data).execute().data[0]

    async def delete_commitment(self, commitment_id, user_id):
        result = _Query(self, "commitments")
        return bool(result.delete().eq("id", commitment_id).eq("user_id", user_id).execute().data)

    async def read_calendar_days(self, user_id, start, end):
        return [
            copy.deepcopy(row) for row in self.tables["calendar_days"]
            if row.get("user_id") == user_id and start <= str(row.get("date")) <= end
        ]

    async def upsert_calendar_days(self, days):
        result = _Query(self, "calendar_days")
        return result.upsert(days).execute().data


def _other_leave():
    return {
        "id": "leave-other",
        "user_id": "user-1",
        "name": "Trip",
        "start_date": OTHER_DAY,
        "end_date": OTHER_DAY,
        "effects": {"type": "annual"},
        "notes": "",
    }


def _day(store, day):
    return next(row for row in store.tables["calendar_days"] if row["date"] == day)


def _leave_ids(store):
    return {row["id"] for row in store.tables["leave_blocks"]}


def _executor(store):
    executor = CommandExecutor(store, "user-1")
    executor.effective_tier = "pro"
    return executor


@pytest.mark.asyncio
async def test_undo_and_redo_of_an_approved_sick_day():
    store = _Store(leaves=[_other_leave()])
    executor = _executor(store)

    approved = await executor.execute({
        "action": "add_leave",
        "payload": {"name": "Sick", "type": "sick", "date": SICK_DAY},
        "explanation": "Sick day",
    })
    assert approved["success"] is True
    sick_id = approved["result"]["inserted_leave_id"]
    assert sick_id
    assert sick_id in _leave_ids(store)
    assert "leave-other" in _leave_ids(store)
    sick = _day(store, SICK_DAY)
    assert sick["state_json"]["is_leave"] is True
    assert sick["work_type"] == "work_day"
    other = _day(store, OTHER_DAY)
    assert other["state_json"]["is_leave"] is True

    undone = await executor.execute({
        "action": "undo",
        "payload": {},
        "explanation": "Undo",
    }, skip_validation=True)
    assert undone["success"] is True
    assert sick_id not in _leave_ids(store)
    assert _leave_ids(store) == {"leave-other"}
    restored = _day(store, SICK_DAY)
    assert restored["state_json"]["is_leave"] is False
    assert "leave" not in (restored["state_json"].get("tags") or [])
    assert restored["work_type"] == "work_day"
    assert _day(store, OTHER_DAY)["state_json"]["is_leave"] is True
    logged = next(row for row in store.tables["command_log"] if row["action"] == "add_leave")
    assert logged["status"] == "undone"
    assert logged["payload"]["inserted_leave_id"] == sick_id

    redone = await executor.execute({
        "action": "redo",
        "payload": {},
        "explanation": "Redo",
    }, skip_validation=True)
    assert redone["success"] is True
    assert sick_id in _leave_ids(store)
    assert "leave-other" in _leave_ids(store)
    again = _day(store, SICK_DAY)
    assert again["state_json"]["is_leave"] is True
    assert again["work_type"] == "work_day"
    assert logged["status"] == "redone"


@pytest.mark.asyncio
async def test_undo_of_an_older_sick_day_log_removes_only_that_row():
    sick = {
        "id": "leave-sick",
        "user_id": "user-1",
        "name": "Sick",
        "start_date": SICK_DAY,
        "end_date": SICK_DAY,
        "effects": {"type": "sick"},
        "notes": "",
    }
    other = _other_leave()
    store = _Store(leaves=[sick, other])
    before = copy.deepcopy(store.tables["master_settings"][0]["settings"])
    before["leave_blocks"] = [copy.deepcopy(other)]
    after = copy.deepcopy(store.tables["master_settings"][0]["settings"])
    store.tables["calendar_days"] = [{
        "user_id": "user-1",
        "date": SICK_DAY,
        "work_type": "off",
        "cycle_id": "cycle-1",
        "cycle_day": 1,
        "state_json": {"is_leave": True, "tags": ["leave"], "commitments": []},
    }]
    store.tables["command_log"] = [{
        "id": "cmd-sick",
        "user_id": "user-1",
        "action": "add_leave",
        "status": "applied",
        "created_at": "2026-01-05T12:00:00+00:00",
        "payload": {"name": "Sick", "type": "sick", "date": SICK_DAY},
        "before_state": before,
        "after_state": after,
        "source": "chat",
        "message_id": None,
        "explanation": "Sick day",
    }]

    result = await _executor(store).execute({
        "action": "undo",
        "payload": {},
        "explanation": "Undo",
    }, skip_validation=True)

    assert result["success"] is True
    assert _leave_ids(store) == {"leave-other"}
    restored = _day(store, SICK_DAY)
    assert restored["state_json"]["is_leave"] is False
    assert restored["work_type"] == "work_day"


@pytest.mark.asyncio
async def test_undo_removes_only_ids_the_snapshot_added():
    added = {
        "id": "leave-a",
        "user_id": "user-1",
        "name": "A",
        "start_date": SICK_DAY,
        "end_date": SICK_DAY,
    }
    kept = {
        "id": "leave-kept",
        "user_id": "user-1",
        "name": "Kept",
        "start_date": OTHER_DAY,
        "end_date": OTHER_DAY,
    }
    before = copy.deepcopy(_Store(leaves=[kept]).tables["master_settings"][0]["settings"])
    after = copy.deepcopy(before)
    after["leave_blocks"] = [copy.deepcopy(kept), copy.deepcopy(added)]
    store = _Store(leaves=[added, kept])
    store.tables["master_settings"][0]["settings"] = copy.deepcopy(after)
    store.tables["command_log"] = [{
        "id": "cmd-added",
        "user_id": "user-1",
        "action": "add_leave",
        "status": "applied",
        "created_at": "2026-01-05T12:00:00+00:00",
        "payload": {},
        "before_state": before,
        "after_state": after,
        "source": "chat",
        "message_id": None,
        "explanation": "Leave",
    }]

    await _executor(store)._action_undo({})

    assert _leave_ids(store) == {"leave-kept"}
    assert any(row["id"] == "leave-kept" for row in store.tables["master_settings"][0]["settings"]["leave_blocks"])


@pytest.mark.asyncio
async def test_undo_refuses_a_command_that_is_not_the_latest(monkeypatch):
    store = _Store(leaves=[_other_leave()])
    monkeypatch.setattr("app.routes.commands.Database", lambda use_admin=False: store)
    executor = _executor(store)
    first = await executor.execute({
        "action": "add_leave",
        "payload": {"name": "Sick", "type": "sick", "date": SICK_DAY},
        "explanation": "Sick day",
    })
    second = await executor.execute({
        "action": "add_leave",
        "payload": {"name": "Later", "type": "personal", "start_date": "2026-01-09", "end_date": "2026-01-09"},
        "explanation": "Later leave",
    })
    assert first["success"] is True and second["success"] is True
    older_id = first["command_id"]
    latest_id = second["command_id"]
    before_ids = _leave_ids(store)

    with pytest.raises(HTTPException) as exc:
        await undo_command(UndoRequest(command_id=older_id), {"id": "user-1"})

    assert exc.value.status_code == 409
    assert "latest" in str(exc.value.detail).lower()
    assert _leave_ids(store) == before_ids
    statuses = {row["id"]: row["status"] for row in store.tables["command_log"]}
    assert statuses[older_id] == "applied"
    assert statuses[latest_id] == "applied"

    undone = await undo_command(UndoRequest(command_id=latest_id), {"id": "user-1"})
    assert undone["success"] is True
    assert second["result"]["inserted_leave_id"] not in _leave_ids(store)
    assert first["result"]["inserted_leave_id"] in _leave_ids(store)
    statuses = {row["id"]: row["status"] for row in store.tables["command_log"]}
    assert statuses[older_id] == "applied"
    assert statuses[latest_id] == "undone"


@pytest.mark.asyncio
async def test_undo_of_a_new_commitment_removes_only_that_row():
    kept = {
        "id": "commitment-gym",
        "user_id": "user-1",
        "name": "Gym",
        "type": "personal",
        "status": "active",
        "start_date": OTHER_DAY,
        "end_date": OTHER_DAY,
    }
    store = _Store(commitments=[kept])
    executor = _executor(store)
    approved = await executor.execute({
        "action": "add_commitment",
        "payload": {"name": "Dentist", "type": "personal", "start_date": SICK_DAY, "end_date": SICK_DAY},
        "explanation": "Dentist",
    })
    assert approved["success"] is True
    created_id = approved["result"]["inserted_commitment_id"]
    ids = {row["id"] for row in store.tables["commitments"]}
    assert ids == {"commitment-gym", created_id}

    undone = await executor.execute({
        "action": "undo",
        "payload": {},
        "explanation": "Undo",
    }, skip_validation=True)
    assert undone["success"] is True
    assert {row["id"] for row in store.tables["commitments"]} == {"commitment-gym"}
    chips = _day(store, SICK_DAY)["state_json"]["commitments"]
    assert all(chip.get("commitment_id") != created_id for chip in chips)

    redone = await executor.execute({
        "action": "redo",
        "payload": {},
        "explanation": "Redo",
    }, skip_validation=True)
    assert redone["success"] is True
    assert created_id in {row["id"] for row in store.tables["commitments"]}


@pytest.mark.asyncio
async def test_delete_leave_rebuilds_the_day_back_to_the_rotation(monkeypatch):
    sick = {
        "id": "leave-sick",
        "user_id": "user-1",
        "name": "Sick",
        "start_date": SICK_DAY,
        "end_date": SICK_DAY,
        "effects": {"type": "sick"},
        "notes": "",
    }
    ghost_leave = {
        "id": "leave-ghost",
        "name": "Ghost",
        "start_date": SICK_DAY,
        "end_date": SICK_DAY,
    }
    ghost_commitment = {
        "id": "commitment-ghost",
        "name": "Ghost chip",
        "type": "personal",
        "status": "active",
        "start_date": SICK_DAY,
        "end_date": SICK_DAY,
    }
    store = _Store(leaves=[sick, _other_leave()])
    settings = store.tables["master_settings"][0]["settings"]
    settings["leave_blocks"] = [sick, _other_leave(), ghost_leave]
    settings["commitments"] = [ghost_commitment]
    store.tables["calendar_days"] = [{
        "user_id": "user-1",
        "date": SICK_DAY,
        "work_type": "off",
        "cycle_id": "cycle-1",
        "cycle_day": 1,
        "state_json": {"is_leave": True, "tags": ["leave"], "commitments": [
            {"commitment_id": "commitment-ghost", "name": "Ghost chip"},
        ], "available_hours": 16.0},
    }]
    monkeypatch.setattr("app.routes.calendar.Database", lambda use_admin=False: store)

    result = await delete_leave_block("leave-sick", {"id": "user-1"})

    assert result["success"] is True
    assert _leave_ids(store) == {"leave-other"}
    saved_settings = store.tables["master_settings"][0]["settings"]
    listed = {row["id"] for row in saved_settings["leave_blocks"]}
    assert "leave-ghost" in listed
    assert "leave-other" in listed
    assert [row["id"] for row in saved_settings["commitments"]] == ["commitment-ghost"]
    restored = _day(store, SICK_DAY)
    assert restored["state_json"]["is_leave"] is False
    assert restored["work_type"] == "work_day"
    assert restored["state_json"]["commitments"] == []


def _settings_leave_ids(store):
    settings = store.tables["master_settings"][0]["settings"]
    return {row.get("id") for row in settings.get("leave_blocks") or []}


def _wire(monkeypatch, store):
    monkeypatch.setattr("app.routes.commands.Database", lambda use_admin=False: store)


@pytest.mark.asyncio
async def test_a_second_undo_does_not_restore_a_settings_only_leave():
    store = _Store()
    executor = _executor(store)
    approved = await executor.execute({
        "action": "add_leave",
        "payload": {"name": "Sick", "type": "sick", "date": SICK_DAY},
        "explanation": "Sick day",
    })
    leave_id = approved["result"]["inserted_leave_id"]
    first = await executor.execute({"action": "undo", "payload": {}, "explanation": "Undo"})
    assert first["success"] is True
    assert leave_id not in _leave_ids(store)
    assert leave_id not in _settings_leave_ids(store)

    second = await executor.execute({"action": "undo", "payload": {}, "explanation": "Undo"})

    assert second["result"]["message"] == "Nothing to undo"
    assert leave_id not in _leave_ids(store)
    assert leave_id not in _settings_leave_ids(store)
    assert not any(row.get("action") in ("undo", "redo") for row in store.tables["command_log"])


@pytest.mark.asyncio
async def test_redo_then_undo_of_a_reused_form_row_keeps_that_row():
    form = {
        "id": "form-leave",
        "user_id": "user-1",
        "name": "Sick",
        "start_date": SICK_DAY,
        "end_date": SICK_DAY,
        "effects": {"type": "sick"},
        "notes": "",
    }
    store = _Store(leaves=[form])
    before = copy.deepcopy(store.tables["master_settings"][0]["settings"])
    before["leave_blocks"] = []
    after = copy.deepcopy(before)
    after["leave_blocks"] = [copy.deepcopy(form)]
    store.tables["master_settings"][0]["settings"] = copy.deepcopy(after)
    store.tables["command_log"] = [{
        "id": "cmd-reuse",
        "user_id": "user-1",
        "action": "add_leave",
        "status": "applied",
        "created_at": "2026-01-05T12:00:00+00:00",
        "payload": {"name": "Sick", "type": "sick", "inserted_leave_id": ""},
        "before_state": before,
        "after_state": after,
        "source": "api",
        "message_id": None,
        "explanation": "Reuse the form row",
    }]
    executor = _executor(store)

    await executor.execute({"action": "undo", "payload": {}, "explanation": "Undo"})
    await executor.execute({"action": "redo", "payload": {}, "explanation": "Redo"})
    await executor.execute({"action": "undo", "payload": {}, "explanation": "Undo"})

    assert _leave_ids(store) == {"form-leave"}
    assert "form-leave" not in _settings_leave_ids(store)


@pytest.mark.asyncio
async def test_undo_after_redo_by_id(monkeypatch):
    store = _Store()
    _wire(monkeypatch, store)
    executor = _executor(store)
    approved = await executor.execute({
        "action": "add_leave",
        "payload": {"name": "Sick", "type": "sick", "date": SICK_DAY},
        "explanation": "Sick day",
    })
    command_id = approved["command_id"]
    leave_id = approved["result"]["inserted_leave_id"]
    await undo_command(UndoRequest(), {"id": "user-1"})
    await redo_command(RedoRequest(), {"id": "user-1"})

    undone = await undo_command(UndoRequest(command_id=command_id), {"id": "user-1"})

    assert undone["success"] is True
    assert leave_id not in _leave_ids(store)
    status = next(row["status"] for row in store.tables["command_log"] if row["id"] == command_id)
    assert status == "undone"


@pytest.mark.asyncio
async def test_a_second_concurrent_undo_is_refused(monkeypatch):
    store = _Store()
    _wire(monkeypatch, store)
    executor = _executor(store)
    approved = await executor.execute({
        "action": "add_leave",
        "payload": {"name": "Sick", "type": "sick", "date": SICK_DAY},
        "explanation": "Sick day",
    })
    command_id = approved["command_id"]
    leave_id = approved["result"]["inserted_leave_id"]

    async def once():
        return await undo_command(UndoRequest(command_id=command_id), {"id": "user-1"})

    outcomes = await asyncio.gather(once(), once(), return_exceptions=True)
    successes = [item for item in outcomes if isinstance(item, dict) and item.get("success")]
    conflicts = [item for item in outcomes if isinstance(item, HTTPException)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409
    assert leave_id not in _leave_ids(store)


@pytest.mark.asyncio
async def test_redo_refuses_when_a_newer_command_was_applied(monkeypatch):
    store = _Store()
    _wire(monkeypatch, store)
    executor = _executor(store)
    leave = await executor.execute({
        "action": "add_leave",
        "payload": {"name": "Sick", "type": "sick", "date": SICK_DAY},
        "explanation": "Sick day",
    })
    await undo_command(UndoRequest(), {"id": "user-1"})
    added = await executor.execute({
        "action": "add_commitment",
        "payload": {"name": "Gym", "type": "personal", "start_date": OTHER_DAY, "end_date": OTHER_DAY},
        "explanation": "Gym",
    })
    assert added["success"] is True

    with pytest.raises(HTTPException) as exc:
        await redo_command(RedoRequest(command_id=leave["command_id"]), {"id": "user-1"})

    assert exc.value.status_code == 409
    assert "newer" in str(exc.value.detail).lower()
    assert leave["result"]["inserted_leave_id"] not in _leave_ids(store)
    assert added["result"]["inserted_commitment_id"] in {
        row["id"] for row in store.tables["commitments"]
    }
    status = next(row["status"] for row in store.tables["command_log"] if row["id"] == leave["command_id"])
    assert status == "undone"
