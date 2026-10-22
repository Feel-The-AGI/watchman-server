"""Chat cycle edits and undo keep the cycles row aligned with settings."""

import copy
from datetime import date

import pytest

from app.engines.command_executor import CommandExecutor
from app.routes.cycles import (
    CreateCycleRequest,
    CycleBlockSchema,
    UpdateCycleRequest,
    create_cycle,
    update_cycle,
)
from app.routes.master_settings import (
    UpdateSectionRequest,
    UpdateSettingsRequest,
    update_master_settings,
    update_section,
)


ANCHOR = "2026-10-22"
SHIFTED = "2026-10-23"
PATTERN = [
    {"label": "work_day", "duration": 5},
    {"label": "work_night", "duration": 5},
    {"label": "off", "duration": 5},
]


def _cycle(anchor=ANCHOR, cycle_id="cycle-1"):
    return {
        "id": cycle_id,
        "name": "5-5-5",
        "pattern": [dict(block) for block in PATTERN],
        "anchor_date": anchor,
        "anchor_cycle_day": 1,
        "total_days": 15,
    }


def _row(anchor=ANCHOR, cycle_id="cycle-1"):
    return {
        "id": cycle_id,
        "user_id": "user-1",
        "name": "5-5-5",
        "pattern": [dict(block) for block in PATTERN],
        "cycle_length": 15,
        "anchor_date": anchor,
        "anchor_cycle_day": 1,
        "is_active": True,
    }


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self.filters = {}
        self._update = None

    def select(self, *args):
        return self

    def update(self, data):
        self._update = data
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, n):
        return self

    def execute(self):
        rows = self.db.commands if self.table == "command_log" else []
        if self._update is not None:
            matched = [row for row in rows if all(row.get(key) == value for key, value in self.filters.items())]
            for row in matched:
                row.update(self._update)
            return _Result(matched)
        matched = [row for row in rows if all(row.get(key) == value for key, value in self.filters.items())]
        return _Result(matched)


class _Client:
    def __init__(self, db):
        self.db = db

    def table(self, name):
        return _Query(self.db, name)


class _Cycles:
    def __init__(self, rows):
        self.rows = rows
        self.commands = []
        self.updated = []
        self.client = _Client(self)

    async def get_cycles(self, user_id):
        return [row for row in self.rows if row.get("user_id") == user_id]

    async def update_cycle(self, cycle_id, data, user_id):
        self.updated.append((cycle_id, dict(data)))
        for row in self.rows:
            if row.get("id") == cycle_id and row.get("user_id") == user_id:
                row.update(data)
                return dict(row)
        return None

    async def complete_onboarding_if_open(self, user_id):
        return False


class _Settings:
    def __init__(self, cycle):
        self.document = {
            "cycle": cycle,
            "leave_blocks": [],
            "commitments": [],
            "constraints": [],
        }
        self.writes = []

    async def get(self, user_id):
        return {"settings": self.document, "version": 1}

    async def update_section(self, user_id, section, value):
        self.document = {**self.document, section: value}
        self.writes.append(value)
        return {"settings": self.document}

    async def update(self, user_id, settings):
        self.document = settings
        return {"settings": self.document}


def _world(rows, cycle=None):
    settings = _Settings(cycle or _cycle())
    db = _Cycles(rows)
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.user_id = "user-1"
    executor.db = db
    executor.settings_service = settings
    return settings, db, executor


@pytest.mark.asyncio
async def test_a_shift_updates_both_copies_and_the_reverse_restores_them():
    settings, db, executor = _world([_row()])
    await executor._action_update_cycle({"shift_by_days": 1})
    assert settings.document["cycle"]["anchor_date"] == SHIFTED
    assert db.rows[0]["anchor_date"] == SHIFTED
    assert db.rows[0]["anchor_cycle_day"] == 1

    await executor._action_update_cycle({"shift_by_days": -1})
    assert settings.document["cycle"]["anchor_date"] == ANCHOR
    assert db.rows[0]["anchor_date"] == ANCHOR


@pytest.mark.asyncio
async def test_undo_after_a_shift_restores_the_row():
    settings, db, executor = _world([_row()])
    original = copy.deepcopy(settings.document)
    await executor._action_update_cycle({"shift_by_days": 1})
    assert db.rows[0]["anchor_date"] == SHIFTED
    db.commands.append({
        "id": "cmd-1",
        "user_id": "user-1",
        "status": "applied",
        "before_state": original,
        "after_state": copy.deepcopy(settings.document),
    })

    await executor._action_undo({})
    assert settings.document["cycle"]["anchor_date"] == ANCHOR
    assert db.rows[0]["anchor_date"] == ANCHOR
    assert db.commands[0]["status"] == "undone"

    await executor._action_redo({})
    assert settings.document["cycle"]["anchor_date"] == SHIFTED
    assert db.rows[0]["anchor_date"] == SHIFTED
    assert db.commands[0]["status"] == "redone"


@pytest.mark.asyncio
async def test_a_row_with_a_different_id_is_left_unchanged():
    other = _row(cycle_id="other")
    settings, db, executor = _world([other])
    await executor._action_update_cycle({"shift_by_days": 1})
    assert settings.document["cycle"]["anchor_date"] == SHIFTED
    assert db.rows[0]["id"] == "other"
    assert db.rows[0]["anchor_date"] == ANCHOR
    assert db.rows[0]["name"] == "5-5-5"
    assert db.updated == []


@pytest.mark.asyncio
async def test_no_cycle_row_does_not_fail_the_shift():
    settings, db, executor = _world([])
    saved = await executor._action_update_cycle({"shift_by_days": 1})
    assert saved["cycle"]["anchor_date"] == SHIFTED
    assert settings.document["cycle"]["anchor_date"] == SHIFTED
    assert db.rows == []
    assert db.updated == []


@pytest.mark.asyncio
async def test_a_name_only_patch_keeps_the_shifted_anchor(monkeypatch):
    """The row still has the pre-shift anchor. The PATCH must not copy it back."""
    settings = _Settings(_cycle(SHIFTED))
    db = _Cycles([_row(ANCHOR)])
    monkeypatch.setattr("app.routes.cycles.Database", lambda **kwargs: db)
    monkeypatch.setattr("app.routes.cycles.MasterSettingsService", lambda _db: settings)

    result = await update_cycle("cycle-1", UpdateCycleRequest(name="Days"), {"id": "user-1"})

    assert result["success"] is True
    assert settings.document["cycle"]["anchor_date"] == SHIFTED
    assert settings.document["cycle"]["name"] == "Days"
    assert settings.document["cycle"]["pattern"] == PATTERN
    assert db.rows[0]["anchor_date"] == SHIFTED
    assert db.rows[0]["name"] == "Days"
    assert db.rows[0]["is_active"] is True


def _cycle_without_id(anchor=ANCHOR):
    cycle = _cycle(anchor)
    cycle.pop("id")
    return cycle


@pytest.mark.asyncio
async def test_a_shift_without_an_id_syncs_the_only_active_row():
    settings, db, executor = _world([_row()], _cycle_without_id())
    saved = await executor._action_update_cycle({"shift_by_days": 1})
    assert "id" not in saved["cycle"]
    assert settings.document["cycle"]["anchor_date"] == SHIFTED
    assert db.rows[0]["id"] == "cycle-1"
    assert db.rows[0]["anchor_date"] == SHIFTED
    assert db.updated[0][0] == "cycle-1"


@pytest.mark.asyncio
async def test_a_shift_without_an_id_skips_two_active_rows():
    rows = [_row(cycle_id="cycle-1"), _row(cycle_id="cycle-2")]
    settings, db, executor = _world(rows, _cycle_without_id())
    saved = await executor._action_update_cycle({"shift_by_days": 1})
    assert saved["cycle"]["anchor_date"] == SHIFTED
    assert settings.document["cycle"]["anchor_date"] == SHIFTED
    assert [row["anchor_date"] for row in db.rows] == [ANCHOR, ANCHOR]
    assert db.updated == []


class _CreateStore:
    def __init__(self, cycle, rows):
        self.settings = {
            "cycle": cycle,
            "leave_blocks": [],
            "constraints": [],
            "commitments": [],
        }
        self.rows = rows
        self.rebuilt = []


class _CreateDB:
    def __init__(self, store):
        self.store = store

    async def get_cycles(self, user_id):
        return [row for row in self.store.rows if row.get("user_id") == user_id]

    async def update_cycle(self, cycle_id, data, user_id):
        for row in self.store.rows:
            if row.get("id") == cycle_id and row.get("user_id") == user_id:
                row.update(data)
                return dict(row)
        return None

    async def create_cycle(self, data):
        row = {"id": "new-row", **data}
        self.store.rows.append(row)
        return row

    async def get_active_cycle(self, user_id):
        active = [
            row for row in self.store.rows
            if row.get("user_id") == user_id and row.get("is_active")
        ]
        return active[0] if len(active) == 1 else None

    async def get_leave_blocks(self, user_id):
        return []

    async def complete_onboarding_if_open(self, user_id):
        return False


class _CreateSettings:
    def __init__(self, db):
        self.db = db

    async def get(self, user_id):
        return {"settings": self.db.store.settings, "version": 1}

    async def update_section(self, user_id, section, value):
        self.db.store.settings = {**self.db.store.settings, section: value}
        return {"settings": self.db.store.settings}

    async def get_snapshot(self, user_id):
        return self.db.store.settings


def _executor_factory(db, user_id):
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.db = db
    executor.user_id = user_id
    executor.settings_service = _CreateSettings(db)

    async def rebuild(start_date, end_date):
        snapshot = await executor.settings_service.get_snapshot(user_id)
        db.store.rebuilt.append({
            "cycle": dict(snapshot.get("cycle") or {}),
            "start": start_date,
            "end": end_date,
        })
        return True

    executor.rebuild_calendar = rebuild
    return executor


def _old_row():
    return {
        "id": "old-cycle",
        "user_id": "user-1",
        "name": "Old",
        "pattern": [{"label": "off", "duration": 2}],
        "cycle_length": 2,
        "anchor_date": "2020-01-01",
        "anchor_cycle_day": 1,
        "is_active": True,
    }


NEW_PATTERN = [
    CycleBlockSchema(label="work_night", duration=4),
    CycleBlockSchema(label="off", duration=3),
]


async def _post_cycle(monkeypatch, store):
    db = _CreateDB(store)
    monkeypatch.setattr("app.routes.cycles.Database", lambda **kwargs: db)
    monkeypatch.setattr("app.engines.calendar_authority.MasterSettingsService", _CreateSettings)
    monkeypatch.setattr("app.routes.cycles.create_command_executor", _executor_factory)
    result = await create_cycle(
        CreateCycleRequest(
            name="New rotation",
            pattern=NEW_PATTERN,
            anchor_date=date(2026, 11, 2),
            anchor_cycle_day=1,
        ),
        {"id": "user-1", "tier": "pro"},
    )
    return result


@pytest.mark.asyncio
async def test_creating_a_cycle_replaces_a_different_settings_cycle(monkeypatch):
    old = {
        "id": "old-cycle",
        "name": "Old",
        "pattern": [{"label": "off", "duration": 2}],
        "anchor_date": "2020-01-01",
        "anchor_cycle_day": 1,
        "total_days": 2,
    }
    store = _CreateStore(old, [_old_row()])
    result = await _post_cycle(monkeypatch, store)

    assert result["success"] is True
    saved = store.settings["cycle"]
    assert saved["id"] == "new-row"
    assert saved["name"] == "New rotation"
    assert saved["anchor_date"] == "2026-11-02"
    assert saved["anchor_cycle_day"] == 1
    assert saved["total_days"] == 7
    assert saved["pattern"] == [
        {"label": "work_night", "duration": 4},
        {"label": "off", "duration": 3},
    ]
    assert store.rows[0]["is_active"] is False
    assert store.rows[1]["id"] == "new-row"
    assert store.rows[1]["is_active"] is True
    assert len(store.rebuilt) == 1
    assert store.rebuilt[0]["cycle"]["id"] == "new-row"
    assert store.rebuilt[0]["cycle"]["anchor_date"] == "2026-11-02"
    assert store.rebuilt[0]["start"] == date(2026, 1, 1)


@pytest.mark.asyncio
async def test_creating_a_cycle_fills_empty_settings(monkeypatch):
    store = _CreateStore(None, [])
    result = await _post_cycle(monkeypatch, store)

    assert result["success"] is True
    saved = store.settings["cycle"]
    assert saved["id"] == "new-row"
    assert saved["anchor_date"] == "2026-11-02"
    assert saved["pattern"][0]["label"] == "work_night"
    assert store.rebuilt[0]["cycle"]["id"] == "new-row"
    assert store.rebuilt[0]["start"] == date(2026, 1, 1)


class _SettingsService:
    def __init__(self, db):
        self.db = db

    async def update(self, user_id, settings, expected_version=None):
        self.db.document = settings
        return {"settings": settings, "version": 2}

    async def update_section(self, user_id, section, value):
        self.db.document = {**self.db.document, section: value}
        return {"settings": self.db.document}


def _settings_db(rows, document):
    db = _Cycles(rows)
    db.document = document
    return db


@pytest.mark.asyncio
async def test_a_master_settings_cycle_save_updates_the_row(monkeypatch):
    row = _row(ANCHOR)
    db = _settings_db([row], {"cycle": _cycle(ANCHOR), "work": {"shift_hours": 12}})
    monkeypatch.setattr("app.routes.master_settings.Database", lambda **kwargs: db)
    monkeypatch.setattr(
        "app.routes.master_settings.create_master_settings_service",
        lambda _db: _SettingsService(db),
    )
    saved_cycle = _cycle(SHIFTED)
    await update_section("cycle", UpdateSectionRequest(value=saved_cycle), {"id": "user-1"})
    assert db.rows[0]["anchor_date"] == SHIFTED
    assert db.rows[0]["id"] == "cycle-1"
    assert db.rows[0]["is_active"] is True

    db.rows[0]["anchor_date"] = ANCHOR
    await update_master_settings(
        UpdateSettingsRequest(settings={"cycle": saved_cycle, "work": {"shift_hours": 10}}),
        {"id": "user-1"},
    )
    assert db.rows[0]["anchor_date"] == SHIFTED


@pytest.mark.asyncio
async def test_a_master_settings_save_without_a_cycle_leaves_the_row(monkeypatch):
    db = _settings_db([_row(ANCHOR)], {"cycle": _cycle(ANCHOR), "work": {"shift_hours": 12}})
    monkeypatch.setattr("app.routes.master_settings.Database", lambda **kwargs: db)
    monkeypatch.setattr(
        "app.routes.master_settings.create_master_settings_service",
        lambda _db: _SettingsService(db),
    )
    await update_section("work", UpdateSectionRequest(value={"shift_hours": 8}), {"id": "user-1"})
    assert db.rows[0]["anchor_date"] == ANCHOR
    assert db.updated == []

    await update_master_settings(
        UpdateSettingsRequest(settings={"work": {"shift_hours": 6}}),
        {"id": "user-1"},
    )
    assert db.rows[0]["anchor_date"] == ANCHOR
    assert db.updated == []


@pytest.mark.asyncio
async def test_activating_a_cycle_replaces_a_different_settings_cycle(monkeypatch):
    chosen = {
        "id": "chosen-row",
        "user_id": "user-1",
        "name": "Chosen",
        "pattern": [
            {"label": "work_night", "duration": 4},
            {"label": "off", "duration": 3},
        ],
        "cycle_length": 7,
        "anchor_date": "2026-11-02",
        "anchor_cycle_day": 1,
        "is_active": False,
    }
    old_settings = {
        "id": "old-cycle",
        "name": "Old",
        "pattern": [{"label": "off", "duration": 2}],
        "anchor_date": "2020-01-01",
        "anchor_cycle_day": 1,
        "total_days": 2,
    }
    store = _CreateStore(old_settings, [_old_row(), chosen])
    db = _CreateDB(store)
    monkeypatch.setattr("app.routes.cycles.Database", lambda **kwargs: db)
    monkeypatch.setattr("app.engines.calendar_authority.MasterSettingsService", _CreateSettings)
    monkeypatch.setattr("app.routes.cycles.create_command_executor", _executor_factory)

    result = await update_cycle(
        "chosen-row",
        UpdateCycleRequest(is_active=True, name="Chosen now"),
        {"id": "user-1"},
    )

    assert result["success"] is True
    saved = store.settings["cycle"]
    assert saved["id"] == "chosen-row"
    assert saved["name"] == "Chosen now"
    assert saved["anchor_date"] == "2026-11-02"
    assert saved["anchor_cycle_day"] == 1
    assert saved["total_days"] == 7
    assert saved["pattern"][0]["label"] == "work_night"
    assert store.rows[0]["id"] == "old-cycle"
    assert store.rows[0]["is_active"] is False
    assert store.rows[1]["is_active"] is True
    assert store.rebuilt[0]["cycle"]["id"] == "chosen-row"
    assert store.rebuilt[0]["cycle"]["anchor_date"] == "2026-11-02"
    assert store.rebuilt[0]["start"] == date(2026, 1, 1)
