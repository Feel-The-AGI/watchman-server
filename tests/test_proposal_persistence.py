"""Approving a chat command writes the row the calendar reads."""

from unittest.mock import AsyncMock

import pytest

from app.engines.command_executor import CommandExecutor


DENTIST = {
    "name": "Dentist appointment",
    "type": "personal",
    "start_date": "2026-10-27",
    "end_date": "2026-10-27",
    "time_start": "15:00",
    "time_end": "16:00",
    "days_of_week": ["tuesday"],
}


class FakeDB:
    def __init__(self):
        self.commitments = []
        self.leaves = []
        self.upserts = []
        self.fail_commitment = False
        self.days = []

    async def create_commitment(self, data):
        if self.fail_commitment:
            return None
        self.commitments.append(dict(data))
        return dict(data)

    async def get_commitments(self, user_id):
        return list(self.commitments)

    async def delete_commitment(self, commitment_id):
        self.commitments = [row for row in self.commitments if row["id"] != commitment_id]
        return True

    async def create_leave_block(self, data):
        self.leaves.append(dict(data))
        return dict(data)

    async def get_leave_blocks(self, user_id):
        return list(self.leaves)

    async def delete_leave_block(self, leave_id, user_id):
        self.leaves = [row for row in self.leaves if row["id"] != leave_id]
        return True

    async def get_calendar_days(self, user_id, start, end):
        return list(self.days)

    async def delete_calendar_days(self, user_id, start, end):
        return True

    async def upsert_calendar_days(self, days):
        self.upserts = days
        return days


class Settings:
    def __init__(self, cycle=True):
        self.snapshot = {
            "cycle": {
                "id": "cycle-1",
                "name": "Rotation",
                "pattern": [{"label": "off", "duration": 7}],
                "anchor_date": "2026-10-21",
                "anchor_cycle_day": 1,
                "total_days": 7,
            } if cycle else None,
            "constraints": [],
            "leave_blocks": [],
            "commitments": [],
        }

    async def get_snapshot(self, user_id):
        return self.snapshot

    async def add_to_list(self, user_id, section, item):
        self.snapshot.setdefault(section, []).append(item)
        return self.snapshot

    async def remove_from_list(self, user_id, section, item_id):
        return self.snapshot


def _executor(db, settings):
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.db = db
    executor.user_id = "user-1"
    executor.settings_service = settings
    executor._log_command = AsyncMock(return_value={"id": "log-1"})
    return executor


def _day(upserts, day):
    return next(row for row in upserts if row["date"] == day)


@pytest.mark.asyncio
async def test_approving_dentist_persists_commitment_and_calendar_day():
    db = FakeDB()
    executor = _executor(db, Settings(cycle=True))
    result = await executor.execute(
        {"action": "add_commitment", "payload": DENTIST, "explanation": "Executing add_commitment"},
        source="api",
        proposal_id="fb796e92-630a-4394-8adf-abc62e89f280",
    )
    assert result["success"] is True
    assert len(db.commitments) == 1
    saved = db.commitments[0]
    assert saved["name"] == "Dentist appointment"
    assert saved["type"] == "personal"
    assert saved["start_date"] == "2026-10-27"
    assert saved["source_text"] == "fb796e92-630a-4394-8adf-abc62e89f280"
    day = _day(db.upserts, "2026-10-27")
    chips = day["state_json"]["commitments"]
    assert chips[0]["name"] == "Dentist appointment"
    assert chips[0]["time_start"] == "15:00"
    assert chips[0]["time_end"] == "16:00"
    assert not any(
        chip.get("name") == "Dentist appointment"
        for row in db.upserts if row["date"] != "2026-10-27"
        for chip in row["state_json"]["commitments"]
    )


@pytest.mark.asyncio
async def test_failed_commitment_insert_is_not_a_successful_approval():
    db = FakeDB()
    db.fail_commitment = True
    executor = _executor(db, Settings(cycle=True))
    result = await executor.execute(
        {"action": "add_commitment", "payload": DENTIST, "explanation": "Add it"},
        source="api",
    )
    assert result["success"] is False
    assert db.commitments == []
    assert db.upserts == []


@pytest.mark.asyncio
async def test_add_leave_type_other_is_not_a_commitment_row():
    db = FakeDB()
    executor = _executor(db, Settings(cycle=True))
    result = await executor.execute(
        {
            "action": "add_leave",
            "payload": {"type": "other", "date": "2026-10-31"},
            "explanation": "Executing add_leave",
        },
        source="api",
        proposal_id="fc181bbd-3a20-477a-bf8c-5837d804d7e1",
    )
    assert result["success"] is True
    assert db.commitments == []
    assert len(db.leaves) == 1
    leave = db.leaves[0]
    assert leave["start_date"] == "2026-10-31"
    assert leave["end_date"] == "2026-10-31"
    assert leave["effects"]["type"] == "other"
    assert "type" not in leave or leave.get("type") is None
    day = _day(db.upserts, "2026-10-31")
    assert day["state_json"]["is_leave"] is True
    assert "leave" in day["state_json"]["tags"]


@pytest.mark.asyncio
async def test_commitment_is_painted_when_there_is_no_cycle_to_regenerate():
    db = FakeDB()
    db.days = [{
        "user_id": "user-1",
        "date": "2026-10-27",
        "work_type": "work_day",
        "cycle_id": None,
        "cycle_day": 1,
        "state_json": {"commitments": [], "tags": [], "available_hours": 4, "used_hours": 0},
    }]
    executor = _executor(db, Settings(cycle=False))
    result = await executor.execute(
        {"action": "add_commitment", "payload": DENTIST, "explanation": "Add it"},
        source="api",
    )
    assert result["success"] is True
    day = _day(db.upserts, "2026-10-27")
    assert day["work_type"] == "work_day"
    assert day["state_json"]["commitments"][0]["name"] == "Dentist appointment"
