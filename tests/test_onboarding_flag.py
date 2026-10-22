"""Saving a rotation finishes setup when onboarding is still open."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database import Database
from app.engines.command_executor import CommandExecutor
from app.routes.cycles import CreateCycleRequest, CycleBlockSchema, create_cycle


@pytest.mark.asyncio
async def test_an_open_account_is_marked_finished():
    database = Database.__new__(Database)

    async def get_user_by_id(user_id):
        return {"id": user_id, "onboarding_completed": False}

    async def complete_onboarding(user_id):
        return {"id": user_id, "onboarding_completed": True}

    database.get_user_by_id = get_user_by_id
    database.complete_onboarding = complete_onboarding
    assert await database.complete_onboarding_if_open("user-1") is True


@pytest.mark.asyncio
async def test_a_finished_account_is_left_alone():
    database = Database.__new__(Database)

    async def get_user_by_id(user_id):
        return {"id": user_id, "onboarding_completed": True}

    async def complete_onboarding(user_id):
        raise AssertionError("already finished")

    database.get_user_by_id = get_user_by_id
    database.complete_onboarding = complete_onboarding
    assert await database.complete_onboarding_if_open("user-1") is False


@pytest.mark.asyncio
async def test_creating_a_cycle_finishes_open_onboarding(monkeypatch):
    finished = []

    class DB:
        def __init__(self, use_admin=False):
            pass

        async def get_cycles(self, user_id):
            return []

        async def create_cycle(self, data):
            return {"id": "cycle-1", **data}

        async def complete_onboarding_if_open(self, user_id):
            finished.append(user_id)
            return True

    monkeypatch.setattr("app.routes.cycles.Database", DB)
    monkeypatch.setattr("app.routes.cycles.remember_cycle", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.routes.cycles.projection_for_user",
        AsyncMock(return_value={
            "cycle": {
                "anchor_date": "2026-10-22",
                "anchor_cycle_day": 1,
                "pattern": [{"label": "off", "duration": 1}],
            },
        }),
    )
    monkeypatch.setattr(
        "app.routes.cycles.create_command_executor",
        lambda *args: SimpleNamespace(rebuild_calendar=AsyncMock(return_value=True)),
    )

    result = await create_cycle(
        CreateCycleRequest(
            pattern=[CycleBlockSchema(label="off", duration=1)],
            anchor_date="2026-10-22",
            anchor_cycle_day=1,
        ),
        {"id": "user-1", "tier": "pro"},
    )
    assert result["success"] is True
    assert finished == ["user-1"]


@pytest.mark.asyncio
async def test_a_chat_cycle_update_finishes_open_onboarding():
    finished = []
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.user_id = "user-1"

    class DB:
        async def complete_onboarding_if_open(self, user_id):
            finished.append(user_id)
            return True

    class Settings:
        async def get(self, user_id):
            return {"settings": {"cycle": {}, "work": {}}}

        async def update_section(self, user_id, section, value):
            return {}

    executor.db = DB()
    executor.settings_service = Settings()
    saved = await executor._action_update_cycle({
        "pattern": [{"label": "off", "duration": 2}],
        "anchor_date": "2026-10-22",
        "anchor_cycle_day": 1,
    })
    assert saved["cycle"]["anchor_date"] == "2026-10-22"
    assert finished == ["user-1"]
