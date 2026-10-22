"""One local HTTP pass: expired card, Oct 15 Off, undo, and an honest no-change."""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg.types.json import Json

from app.database import Database
from app.engines.chat_context import local_today
from app.middleware.auth import get_current_user
from app.routes import calendar, chat, commands
from tests.integration.pg_client import PgClient
from tests.integration.test_calendar_scenarios import _apply, _pattern, _seed

TODAY = date(2026, 10, 22)
FROZEN = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
PATTERN = _pattern([("work_day", 6), ("off", 1)])


def _database(connection):
    database = Database.__new__(Database)
    database.client = PgClient(connection)
    database.use_admin = True
    return database


def _stamp(connection, user_id):
    rows = connection.execute(
        "SELECT id FROM command_log WHERE user_id = %s ORDER BY ctid",
        (user_id,),
    ).fetchall()
    base = datetime(2099, 1, 1, tzinfo=timezone.utc)
    for index, (row_id,) in enumerate(rows, start=1):
        connection.execute(
            "UPDATE command_log SET created_at = %s WHERE id = %s",
            (base + timedelta(seconds=index), row_id),
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_oct_15_off_override_and_expired_proposal(db, monkeypatch):
    assert local_today("Africa/Accra", FROZEN) == TODAY
    monkeypatch.setattr(
        "app.engines.chat_service.local_today",
        lambda tz_name=None, now=None: local_today(tz_name, FROZEN),
    )

    async def fail_model(**kwargs):
        raise AssertionError("this phrase is resolved without the model")

    monkeypatch.setattr("app.engines.chat_service.complete_chat", fail_model)
    for module in (chat, commands, calendar):
        monkeypatch.setattr(module, "Database", lambda **kwargs: _database(db))
    db.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS prompt_version TEXT")
    db.execute("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS prompt_version TEXT")

    user_id, _cycle = _seed(db, PATTERN, "2026-10-19")
    await _apply(db, user_id, "update_cycle", {
        "pattern": PATTERN, "anchor_date": "2026-10-19", "anchor_cycle_day": 1,
    })
    _stamp(db, user_id)
    before = db.execute(
        "SELECT work_type::text FROM calendar_days WHERE user_id = %s AND date = '2026-10-15'",
        (user_id,),
    ).fetchone()
    assert before[0] == "work_day"

    old_id = str(uuid4())
    db.execute(
        """
        INSERT INTO proposals (id, user_id, command, validation, status, created_at, expires_at)
        VALUES (%s, %s, %s, '{}', 'pending', '2025-12-29T00:00:00Z', NULL)
        """,
        (old_id, user_id, Json({
            "action": "override_days",
            "payload": {"start_date": "2025-10-15", "end_date": "2025-10-15", "work_type": "off"},
            "explanation": "Executing override_days",
        })),
    )
    holder = {"user": {
        "id": user_id, "tier": "pro", "timezone": "Africa/Accra", "onboarding_completed": True,
    }}
    app = FastAPI()
    app.include_router(chat.router, prefix="/api")
    app.include_router(commands.router, prefix="/api")
    app.include_router(calendar.router, prefix="/api/calendar")

    async def current_user():
        return holder["user"]

    app.dependency_overrides[get_current_user] = current_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        pending = await client.get("/api/commands/proposals/pending")
        assert old_id not in {row["id"] for row in pending.json()["proposals"]}
        expired = await client.post(f"/api/commands/proposals/{old_id}/review", json={"decision": "approve"})
        assert expired.status_code == 410
        assert expired.json()["detail"] == "This proposal expired and was not applied."

        sent = await client.post("/api/chat/message", json={
            "content": "Remove Oct 15th, I didn't go to work that day",
        })
        assert sent.status_code == 200, sent.text
        command = sent.json()["proposal"]["command"]
        assert command["payload"] == {
            "start_date": "2026-10-15",
            "end_date": "2026-10-15",
            "work_type": "off",
            "preserve_off_days": False,
        }
        assert command["explanation"] == "Mark Thu 15 Oct 2026 as Off"
        review = await client.post(
            f"/api/commands/proposals/{sent.json()['proposal']['id']}/review",
            json={"decision": "approve"},
        )
        assert review.status_code == 200, review.text
        assert review.json()["status"] == "applied"
        assert review.json()["changed"] is True
        day = await client.get("/api/calendar/day/2026-10-15")
        assert day.json()["data"]["work_type"] == "off"
        assert day.json()["data"]["state_json"]["manual_override"] is True
        month = await client.get("/api/calendar/month/2026/10")
        match = next(item for item in month.json()["data"] if str(item["date"])[:10] == "2026-10-15")
        assert match["work_type"] == "off"

        _stamp(db, user_id)
        undo = await client.post("/api/commands/undo", json={})
        assert undo.status_code == 200, undo.text
        restored = await client.get("/api/calendar/day/2026-10-15")
        assert restored.json()["data"]["work_type"] == "work_day"

        again = await client.post("/api/chat/message", json={"content": "I was off on Oct 18"})
        second = await client.post(
            f"/api/commands/proposals/{again.json()['proposal']['id']}/review",
            json={"decision": "approve"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "no_change"
        assert second.json()["changed"] is False
        assert second.json()["message"] == "No change: Sun 18 Oct 2026 was already Off"
        assert "Done" not in second.json()["message"]
