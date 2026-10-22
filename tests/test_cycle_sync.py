"""Chat cycle edits and undo keep the cycles row aligned with settings."""

import copy

import pytest

from app.engines.command_executor import CommandExecutor
from app.routes.cycles import UpdateCycleRequest, update_cycle


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
