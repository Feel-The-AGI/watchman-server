"""REST leave is open on every plan, Free included."""

from datetime import date, datetime, timedelta, timezone

import pytest
from app.routes.calendar import LeaveBlockRequest, add_leave_block


def _past():
    return (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


class _Store:
    def __init__(self):
        self.created = None
        self.days = [{
            "user_id": "alice",
            "date": "2026-11-02",
            "cycle_id": "cycle-1",
            "cycle_day": 1,
            "work_type": "work_day",
            "state_json": {"is_leave": False, "tags": []},
        }]

    async def create_leave_block(self, data):
        self.created = dict(data)
        return {**data, "id": "leave-1"}

    async def get_calendar_days(self, user_id, start, end):
        return [day for day in self.days if start <= day["date"] <= end]

    async def upsert_calendar_days(self, days):
        by_date = {day["date"]: day for day in self.days}
        for day in days:
            by_date[day["date"]] = day
        self.days = list(by_date.values())
        return days


def _install(monkeypatch, store):
    monkeypatch.setattr("app.routes.calendar.Database", lambda use_admin=False: store)


def _request(leave_type):
    return LeaveBlockRequest(
        name="Day off",
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 2),
        type=leave_type,
    )


@pytest.mark.asyncio
async def test_free_sick_day_is_saved(monkeypatch):
    store = _Store()
    _install(monkeypatch, store)

    result = await add_leave_block(_request("sick"), {"id": "alice", "tier": "free"})

    assert result["success"] is True
    assert store.created["effects"]["type"] == "sick"
    assert store.days[0]["state_json"]["is_leave"] is True


@pytest.mark.asyncio
async def test_free_vacation_is_saved(monkeypatch):
    store = _Store()
    _install(monkeypatch, store)

    result = await add_leave_block(_request("annual"), {"id": "alice", "tier": "free"})

    assert result["success"] is True
    assert store.created["effects"]["type"] == "annual"
    assert store.days[0]["state_json"]["is_leave"] is True


@pytest.mark.asyncio
async def test_free_leave_without_a_type_is_saved(monkeypatch):
    store = _Store()
    _install(monkeypatch, store)

    result = await add_leave_block(_request(None), {"id": "alice", "tier": "free"})

    assert result["success"] is True
    assert store.days[0]["state_json"]["is_leave"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("leave_type", ["sick", "annual"])
async def test_pro_can_save_sick_and_vacation(monkeypatch, leave_type):
    store = _Store()
    _install(monkeypatch, store)

    result = await add_leave_block(
        _request(leave_type),
        {"id": "alice", "tier": "pro"},
    )

    assert result["success"] is True
    assert store.created["effects"]["type"] == leave_type


@pytest.mark.asyncio
@pytest.mark.parametrize("leave_type", ["sick", "annual"])
async def test_expired_pro_still_plans_leave(monkeypatch, leave_type):
    store = _Store()
    _install(monkeypatch, store)
    user = {"id": "alice", "tier": "pro", "pro_expires_at": _past()}

    result = await add_leave_block(_request(leave_type), user)

    assert result["success"] is True
    assert store.created["effects"]["type"] == leave_type
