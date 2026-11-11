"""A lapsed Pro owner's public link stops showing the calendar. The row stays."""

from datetime import datetime, timedelta, timezone

import pytest

from app.routes.sharing import get_shared_calendar


class Store:
    def __init__(self, owner):
        self.owner = owner
        self.share = {
            "id": "share-1",
            "user_id": "owner",
            "share_code": "abc",
            "name": "Roster",
            "show_work_types": True,
            "show_commitments": False,
            "view_count": 3,
            "is_active": True,
        }
        self.days_read = 0

    async def get_calendar_share_by_code(self, code):
        if code != "abc":
            return None
        return dict(self.share)

    async def get_user_by_id(self, user_id):
        return dict(self.owner) if self.owner else None

    async def get_user_name_by_id(self, user_id):
        return (self.owner or {}).get("name")

    async def get_calendar_days(self, user_id, start, end):
        self.days_read += 1
        return [{
            "date": "2026-10-22",
            "cycle_day": 1,
            "work_type": "work_day",
            "state_json": {"is_leave": False},
        }]


def _install(monkeypatch, owner):
    store = Store(owner)
    monkeypatch.setattr("app.routes.sharing.Database", lambda use_admin=False: store)
    return store


@pytest.mark.asyncio
async def test_lapsed_pro_hides_the_shared_calendar_and_keeps_the_row(monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    store = _install(monkeypatch, {
        "id": "owner",
        "name": "Ada",
        "tier": "pro",
        "pro_expires_at": past,
    })

    hidden = await get_shared_calendar("abc")

    assert hidden == {
        "success": False,
        "message": "This shared calendar is no longer available",
    }
    assert store.days_read == 0
    assert store.share["view_count"] == 3
    assert store.share["is_active"] is True

    store.owner["pro_expires_at"] = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    shown = await get_shared_calendar("abc")
    assert shown["success"] is True
    assert shown["data"]["days"][0]["date"] == "2026-10-22"
    assert store.share["is_active"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [
    {"id": "owner", "tier": "free"},
    {"id": "owner", "tier": "trial"},
    None,
])
async def test_non_pro_owners_do_not_publish_the_calendar(monkeypatch, owner):
    store = _install(monkeypatch, owner)
    result = await get_shared_calendar("abc")
    assert result["message"] == "This shared calendar is no longer available"
    assert "data" not in result
    assert store.days_read == 0
    assert store.share["is_active"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["pro", "admin"])
async def test_current_pro_and_admin_shares_still_return_days(monkeypatch, tier):
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    store = _install(monkeypatch, {
        "id": "owner",
        "name": "Ada",
        "tier": tier,
        "pro_expires_at": future,
    })
    result = await get_shared_calendar("abc")
    assert result["success"] is True
    assert result["data"]["owner_name"] == "Ada"
    assert len(result["data"]["days"]) == 1
    assert store.share["is_active"] is True
