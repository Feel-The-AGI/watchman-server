"""Free planning stops on a rolling date three months from today."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.engines.chat_service import ChatService
from app.engines.command_executor import CommandExecutor, create_command_executor
from app.planning_horizon import (
    FREE_HORIZON_MONTHS,
    add_months,
    clip_range,
    horizon_detail,
    horizon_end,
    horizon_violation,
    payload_dates,
)
from app.engines.calendar_engine import CALENDAR_ENGINE_VERSION
from app.routes.calendar import (
    GenerateCalendarRequest,
    _free_view,
    _rebuild_span,
    generate_calendar,
    get_day,
    get_month,
    get_year,
)
from app.routes.commands import ReviewProposalRequest, review_proposal
from app.routes.commitments import (
    CreateCommitmentRequest,
    UpdateCommitmentRequest,
    create_commitment,
    update_commitment,
)


TODAY = date(2026, 10, 22)
HORIZON = date(2027, 1, 22)
NEXT = date(2027, 1, 23)
MESSAGE = (
    "The free plan covers 3 months of planning ahead. "
    "Upgrade to Pro to plan further."
)
CYCLE = {
    "id": "cycle-1",
    "pattern": [
        {"label": "work_day", "duration": 1},
        {"label": "off", "duration": 1},
    ],
    "anchor_date": "2026-01-01",
    "anchor_cycle_day": 1,
    "total_days": 2,
}


def _pin(monkeypatch):
    def end(today=None):
        if today is not None:
            return add_months(today, FREE_HORIZON_MONTHS)
        return HORIZON

    monkeypatch.setattr("app.planning_horizon.horizon_end", end)
    monkeypatch.setattr("app.routes.calendar.horizon_end", end)


def test_add_months_clamps_the_day_at_a_short_month():
    assert FREE_HORIZON_MONTHS == 3
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert add_months(date(2023, 11, 30), 3) == date(2024, 2, 29)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)
    assert add_months(TODAY, FREE_HORIZON_MONTHS) == HORIZON


def test_horizon_includes_the_end_day_and_blocks_the_next():
    assert horizon_end(TODAY) == HORIZON
    assert horizon_violation("free", [HORIZON], TODAY) is None
    assert horizon_violation("free", ["2027-01-22"], TODAY) is None
    blocked = horizon_violation("free", [NEXT], TODAY)
    assert blocked["code"] == "free_horizon"
    assert blocked["horizon_end"] == "2027-01-22"
    assert blocked["message"] == MESSAGE
    assert clip_range(date(2027, 1, 1), date(2027, 12, 31), "free", TODAY) == (
        date(2027, 1, 1),
        HORIZON,
    )
    assert clip_range(NEXT, date(2027, 12, 31), "free", TODAY) is None
    assert clip_range(HORIZON, HORIZON, "free", TODAY) == (HORIZON, HORIZON)


@pytest.mark.parametrize("tier", ["pro", "admin", "trial"])
def test_paid_tiers_have_no_horizon(tier):
    assert horizon_violation(tier, [date(2030, 1, 1)], TODAY) is None
    assert clip_range(date(2027, 1, 1), date(2028, 12, 31), tier, TODAY) == (
        date(2027, 1, 1),
        date(2028, 12, 31),
    )


def test_a_free_year_is_complete_once_the_allowed_days_exist(monkeypatch):
    _pin(monkeypatch)
    days = []
    current = date(2027, 1, 1)
    while current <= HORIZON:
        days.append({
            "date": current.isoformat(),
            "state_json": {"engine_version": CALENDAR_ENGINE_VERSION},
        })
        current += timedelta(days=1)
    assert _rebuild_span(days, 2027, "free") is None
    assert _rebuild_span(days, 2028, "free") is None
    assert _rebuild_span([], 2027, "free") == (date(2027, 1, 1), HORIZON)
    assert _rebuild_span(days[:-1], 2027, "free") == (date(2027, 1, 1), HORIZON)


def test_anchor_dates_are_not_treated_as_planned_days():
    assert payload_dates({
        "anchor_date": "2030-01-01",
        "shift_by_days": 4,
        "schedule": {"date": "2027-01-23"},
    }) == [NEXT]


class _Days:
    def __init__(self, days):
        self.days = days

    def __init_db(self, use_admin=False):
        return self

    async def get_calendar_day(self, user_id, date_str):
        return next((day for day in self.days if day["date"] == date_str), None)

    async def get_calendar_days(self, user_id, start, end):
        return [day for day in self.days if start <= day["date"] <= end]


def _install_days(monkeypatch, days):
    store = _Days(days)
    monkeypatch.setattr("app.routes.calendar.Database", lambda use_admin=False: store)
    return store


@pytest.mark.asyncio
async def test_reads_keep_later_rows_and_tell_the_client_where_to_lock(monkeypatch):
    _pin(monkeypatch)
    later = {
        "user_id": "user-1",
        "date": NEXT.isoformat(),
        "work_type": "work_night",
        "state_json": {},
    }
    edge = {
        "user_id": "user-1",
        "date": HORIZON.isoformat(),
        "work_type": "off",
        "state_json": {},
    }
    before = deepcopy([later, edge])
    store = _install_days(monkeypatch, [later, edge])
    free = {"id": "user-1", "tier": "free"}

    hidden = await get_day(NEXT.isoformat(), free)
    assert hidden["data"] is None
    assert hidden["free_horizon_end"] == HORIZON.isoformat()
    shown = await get_day(HORIZON.isoformat(), free)
    assert shown["data"]["date"] == HORIZON.isoformat()
    assert shown["free_horizon_end"] == HORIZON.isoformat()
    month = await get_month(2027, 1, free)
    assert [day["date"] for day in month["data"]] == [HORIZON.isoformat()]
    assert month["free_horizon_end"] == HORIZON.isoformat()
    assert store.days == before

    pro = await get_day(NEXT.isoformat(), {"id": "user-1", "tier": "pro"})
    assert pro["data"]["work_type"] == "work_night"
    assert "free_horizon_end" not in pro
    trial = await get_month(2027, 1, {"id": "user-1", "tier": "trial"})
    assert NEXT.isoformat() in [day["date"] for day in trial["data"]]
    assert "free_horizon_end" not in trial


@pytest.mark.asyncio
async def test_generate_blocks_a_year_past_the_horizon_and_clips_one_that_overlaps(monkeypatch):
    _pin(monkeypatch)
    spans = []

    class DB:
        def __init__(self, use_admin=False):
            self.opened = True

        async def get_calendar_days(self, user_id, start, end):
            return []

    async def rebuild(self, start, end):
        spans.append((start, end))
        return True

    monkeypatch.setattr("app.routes.calendar.Database", DB)
    monkeypatch.setattr(
        "app.routes.calendar.projection_for_user",
        AsyncMock(return_value={"cycle": CYCLE}),
    )
    monkeypatch.setattr(
        "app.engines.command_executor.CommandExecutor.rebuild_calendar",
        rebuild,
    )

    with pytest.raises(HTTPException) as caught:
        await generate_calendar(GenerateCalendarRequest(year=2028), {"id": "user-1", "tier": "free"})
    assert caught.value.status_code == 403
    assert caught.value.detail == horizon_detail()
    assert caught.value.detail["code"] == "free_horizon"
    assert caught.value.detail["horizon_end"] == HORIZON.isoformat()
    assert spans == []

    overlap = await generate_calendar(
        GenerateCalendarRequest(year=2027), {"id": "user-1", "tier": "free"}
    )
    assert overlap["success"] is True
    assert spans == [(date(2027, 1, 1), HORIZON)]

    for tier in ("pro", "admin", "trial"):
        spans.clear()
        await generate_calendar(GenerateCalendarRequest(year=2028), {"id": "user-1", "tier": tier})
        assert spans == [(date(2028, 1, 1), date(2028, 12, 31))]


@pytest.mark.asyncio
async def test_opening_a_later_year_does_not_generate_or_drop_stored_days(monkeypatch):
    _pin(monkeypatch)
    later = {
        "user_id": "user-1",
        "date": "2028-06-01",
        "work_type": "work_day",
        "state_json": {"engine_version": 1},
    }
    calls = []

    class DB:
        def __init__(self, use_admin=False):
            pass

        async def get_calendar_days(self, user_id, start, end):
            return [later] if start <= later["date"] <= end else []

    async def rebuild(self, start, end):
        calls.append((start, end))
        return True

    monkeypatch.setattr("app.routes.calendar.Database", DB)
    monkeypatch.setattr(
        "app.engines.command_executor.CommandExecutor.rebuild_calendar",
        rebuild,
    )
    body = await get_year(2028, {"id": "user-1", "tier": "free"})
    assert calls == []
    assert body["data"] == []
    assert body["free_horizon_end"] == HORIZON.isoformat()
    assert later["work_type"] == "work_day"


class _Settings:
    async def get_snapshot(self, user_id):
        return {
            "cycle": CYCLE,
            "leave_blocks": [],
            "commitments": [],
            "constraints": [],
        }


class _RebuildDB:
    def __init__(self, days, user):
        self.days = days
        self.user = user
        self.upserts = []

    async def get_user_by_id(self, user_id):
        return self.user

    async def read_leave_blocks(self, user_id):
        return []

    async def read_commitments(self, user_id):
        return []

    async def read_calendar_days(self, user_id, start, end):
        return [day for day in self.days if start <= day["date"] <= end]

    async def upsert_calendar_days(self, days):
        self.upserts.extend(day["date"] for day in days)
        by_date = {day["date"]: day for day in self.days}
        for day in days:
            by_date[day["date"]] = day
        self.days = list(by_date.values())
        return days


def _executor(db):
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.db = db
    executor.user_id = "user-1"
    executor.settings_service = _Settings()
    return executor


def _stale_later_day():
    return {
        "user_id": "user-1",
        "date": NEXT.isoformat(),
        "work_type": "work_night",
        "cycle_id": "old-cycle",
        "cycle_day": 9,
        "state_json": {
            "manual_override": False,
            "engine_version": 1,
            "commitments": [{"commitment_id": "gone", "name": "Ghost"}],
            "is_leave": False,
        },
    }


@pytest.mark.asyncio
async def test_rebuild_rewrites_a_stored_day_past_the_horizon_but_reads_hide_it(monkeypatch):
    _pin(monkeypatch)
    original = _stale_later_day()
    db = _RebuildDB([original], {"id": "user-1", "tier": "free"})
    wrote = await _executor(db).rebuild_calendar(HORIZON, date(2027, 12, 31))
    assert wrote is True
    assert NEXT.isoformat() in db.upserts
    rewritten = next(day for day in db.days if day["date"] == NEXT.isoformat())
    assert rewritten["work_type"] != "work_night"
    assert rewritten["work_type"] in {"work_day", "off"}
    assert rewritten["state_json"].get("commitments") in ([], None)
    hidden = _free_view({"id": "user-1", "tier": "free"}, db.days)
    assert NEXT.isoformat() not in {day["date"] for day in hidden["days"]}
    assert hidden["free_horizon_end"] == HORIZON.isoformat()
    assert any(day["date"] == NEXT.isoformat() for day in db.days)

    only_later = _RebuildDB([_stale_later_day()], {"id": "user-1", "tier": "free"})
    assert await _executor(only_later).rebuild_calendar(NEXT, date(2027, 12, 31)) is True
    assert NEXT.isoformat() in only_later.upserts
    assert only_later.days[0]["work_type"] != "work_night"

    for tier in ("pro", "admin", "trial"):
        paid = _RebuildDB([], {"id": "user-1", "tier": tier})
        assert await _executor(paid).rebuild_calendar(NEXT, NEXT) is True
        assert paid.upserts == [NEXT.isoformat()]


class _Commitments:
    def __init__(self):
        self.created = None
        self.updated = None

    async def read_commitments(self, user_id):
        return []

    async def create_commitment(self, data):
        self.created = data
        return data

    async def get_commitment(self, commitment_id):
        return {
            "id": commitment_id,
            "user_id": "user-1",
            "name": "Far trip",
            "type": "personal",
            "status": "active",
            "start_date": "2028-01-01",
            "end_date": "2028-01-05",
        }

    async def update_commitment(self, commitment_id, data):
        self.updated = data
        return {"id": commitment_id, **data}


def _commitment_db(monkeypatch):
    store = _Commitments()
    monkeypatch.setattr(
        "app.routes.commitments.Database",
        lambda use_admin=False: store,
    )
    monkeypatch.setattr(
        "app.routes.commitments._publish_commitment",
        AsyncMock(return_value="ready"),
    )
    return store


@pytest.mark.asyncio
async def test_commitment_dates_stop_at_the_horizon(monkeypatch):
    _pin(monkeypatch)
    user = {"id": "user-1", "tier": "free", "settings": {}}

    def refuse(**kwargs):
        raise AssertionError("commitment write should not open the database")

    monkeypatch.setattr("app.routes.commitments.Database", refuse)
    with pytest.raises(HTTPException) as caught:
        await create_commitment(
            CreateCommitmentRequest(
                name="Trip",
                type="personal",
                start_date=NEXT,
                end_date=NEXT,
            ),
            user,
        )
    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "free_horizon"
    assert caught.value.detail["horizon_end"] == HORIZON.isoformat()

    store = _commitment_db(monkeypatch)
    saved = await create_commitment(
        CreateCommitmentRequest(
            name="Trip",
            type="personal",
            start_date=HORIZON,
            end_date=HORIZON,
        ),
        user,
    )
    assert saved["success"] is True
    assert store.created["start_date"] == HORIZON.isoformat()

    with pytest.raises(HTTPException) as moved:
        await update_commitment(
            "commitment-1",
            UpdateCommitmentRequest(end_date=NEXT),
            user,
        )
    assert moved.value.status_code == 403
    assert store.updated is None

    renamed = await update_commitment(
        "commitment-1",
        UpdateCommitmentRequest(name="Still there"),
        user,
    )
    assert renamed["success"] is True
    assert store.updated == {"name": "Still there"}

    for tier in ("pro", "admin", "trial"):
        paid = _commitment_db(monkeypatch)
        await create_commitment(
            CreateCommitmentRequest(name="Trip", type="personal", start_date=NEXT, end_date=NEXT),
            {"id": "user-1", "tier": tier, "settings": {}},
        )
        assert paid.created["start_date"] == NEXT.isoformat()


def _chat(monkeypatch, tool, version=""):
    monkeypatch.setattr(
        "app.prompts.registry.get_settings",
        lambda: SimpleNamespace(
            chat_prompt_version=version,
            onboarding_prompt_version="",
            chat_prompt_allowlist="",
            onboarding_prompt_allowlist="",
        ),
    )
    service = ChatService.__new__(ChatService)
    service.user_id = "user-1"
    service.db = SimpleNamespace()
    service.settings_service = SimpleNamespace(
        get_snapshot=AsyncMock(return_value={"constraints": []})
    )
    service.command_executor = SimpleNamespace(
        create_proposal=AsyncMock(return_value={"id": "proposal-1"}),
        execute=AsyncMock(),
    )

    async def save(role, content, command_id=None):
        return {"id": "message", "role": role, "content": content}

    service._save_message = save
    service._get_calendar_snapshot = AsyncMock(return_value={
        "text": "", "count": 0, "start": None, "end": None, "counts": {},
    })
    service._get_logs_and_incidents = AsyncMock(return_value="")
    service.get_history = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "app.engines.chat_service.projection_for_user",
        AsyncMock(return_value={"cycle": None, "constraints": [], "leave_blocks": []}),
    )
    monkeypatch.setattr(
        "app.engines.chat_service.complete_chat",
        AsyncMock(return_value=("Noted.", tool)),
    )
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("version,prompt_id", [("", "chat-v2"), ("chat-v1", "chat-v1")])
async def test_free_chat_past_the_horizon_proposes_nothing(monkeypatch, version, prompt_id):
    _pin(monkeypatch)
    service = _chat(monkeypatch, {
        "name": "add_leave",
        "input": {"name": "Holiday", "start_date": NEXT.isoformat(), "end_date": NEXT.isoformat()},
    }, version)
    result = await service.send_message("Add that leave", auto_execute=False, effective_tier="free")
    assert result["response"] == MESSAGE
    assert result["code"] == "free_horizon"
    assert result["horizon_end"] == HORIZON.isoformat()
    assert result["proposal"] is None
    assert result["is_command"] is False
    assert result["prompt_version"] == prompt_id
    assert result["assistant_message"]["content"] == MESSAGE
    service.command_executor.create_proposal.assert_not_awaited()
    service.command_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["pro", "trial", "admin"])
async def test_paid_chat_can_plan_past_the_horizon(monkeypatch, tier):
    _pin(monkeypatch)
    service = _chat(monkeypatch, {
        "name": "add_leave",
        "input": {"name": "Holiday", "start_date": NEXT.isoformat(), "end_date": NEXT.isoformat()},
    })
    result = await service.send_message("Add that leave", auto_execute=False, effective_tier=tier)
    assert result["is_command"] is True
    assert result["proposal"]["id"] == "proposal-1"
    assert "code" not in result
    service.command_executor.create_proposal.assert_awaited()


@pytest.mark.asyncio
async def test_free_chat_keeps_edits_on_the_horizon_and_blocks_the_next_day(monkeypatch):
    _pin(monkeypatch)
    allowed = [
        ("add_commitment", {"name": "Dentist", "type": "personal", "date": HORIZON.isoformat()}),
        ("override_days", {
            "start_date": "2026-11-02",
            "end_date": "2026-11-02",
            "work_type": "work_day",
        }),
        ("create_daily_log", {"date": HORIZON.isoformat(), "overtime_hours": 2}),
        ("add_leave", {
            "name": "Sick",
            "type": "sick",
            "start_date": HORIZON.isoformat(),
            "end_date": HORIZON.isoformat(),
        }),
    ]
    for name, payload in allowed:
        service = _chat(monkeypatch, {"name": name, "input": payload})
        result = await service.send_message("Please do that", auto_execute=False, effective_tier="free")
        assert result["is_command"] is True, name
        assert "code" not in result
        service.command_executor.create_proposal.assert_awaited()

    blocked = [
        ("override_days", {"start_date": NEXT.isoformat(), "end_date": NEXT.isoformat(), "work_type": "off"}),
        ("add_commitment", {"name": "Course", "type": "education", "start_date": NEXT.isoformat()}),
        ("add_leave", {"name": "Sick", "type": "sick", "start_date": NEXT.isoformat(), "end_date": NEXT.isoformat()}),
    ]
    for name, payload in blocked:
        service = _chat(monkeypatch, {"name": name, "input": payload})
        result = await service.send_message("Please do that", auto_execute=False, effective_tier="free")
        assert result["code"] == "free_horizon", name
        assert result["proposal"] is None
        service.command_executor.create_proposal.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_and_proposal_refuse_a_free_date_past_the_horizon(monkeypatch):
    _pin(monkeypatch)
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.user_id = "user-1"
    executor.db = SimpleNamespace()
    executor.effective_tier = "free"
    outcome = await executor.execute({
        "action": "override_days",
        "payload": {
            "start_date": NEXT.isoformat(),
            "end_date": NEXT.isoformat(),
            "work_type": "off",
        },
    })
    assert outcome["success"] is False
    assert outcome["code"] == "free_horizon"
    assert outcome["horizon_end"] == HORIZON.isoformat()
    with pytest.raises(ValueError, match="3 months"):
        await executor.create_proposal({
            "action": "add_leave",
            "payload": {"name": "Holiday", "start_date": NEXT.isoformat(), "end_date": NEXT.isoformat()},
        })


@pytest.mark.asyncio
async def test_approving_a_past_horizon_proposal_returns_403(monkeypatch):
    _pin(monkeypatch)
    proposal_id = uuid4()
    row = {
        "id": str(proposal_id),
        "user_id": "user-1",
        "status": "pending",
        "command": {
            "action": "add_leave",
            "payload": {"name": "Holiday", "start_date": NEXT.isoformat(), "end_date": NEXT.isoformat()},
        },
        "message_id": None,
        "expires_at": None,
    }

    class Query:
        def __init__(self):
            self.filters = {}
            self.values = None

        def update(self, values):
            self.values = values
            return self

        def eq(self, key, value):
            self.filters[key] = value
            return self

        def or_(self, expression):
            return self

        def execute(self):
            matched = row["status"] == self.filters.get("status", row["status"])
            if self.values and matched:
                row.update(self.values)
                return SimpleNamespace(data=[deepcopy(row)])
            if matched:
                return SimpleNamespace(data=[deepcopy(row)])
            return SimpleNamespace(data=[])

    monkeypatch.setattr(
        "app.routes.commands.Database",
        lambda use_admin=False: SimpleNamespace(client=SimpleNamespace(table=lambda name: Query())),
    )

    async def execute(command, source, message_id, proposal_id):
        return {
            "success": False,
            "error": MESSAGE,
            "code": "free_horizon",
            "horizon_end": HORIZON.isoformat(),
        }

    monkeypatch.setattr(
        "app.routes.commands.create_command_executor",
        lambda *args: SimpleNamespace(execute=execute),
    )
    with pytest.raises(HTTPException) as caught:
        await review_proposal(
            proposal_id,
            ReviewProposalRequest(decision="approve"),
            {"id": "user-1", "tier": "free"},
        )
    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "free_horizon"
    assert caught.value.detail["horizon_end"] == HORIZON.isoformat()
    assert row["status"] == "pending"
    assert row["reviewed_at"] is None


def test_expired_pro_is_capped_like_free():
    user = {
        "id": "user-1",
        "tier": "pro",
        "pro_expires_at": datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),
    }
    from app.middleware.auth import get_effective_tier
    assert get_effective_tier(user) == "free"
    assert horizon_violation(get_effective_tier(user), [NEXT], TODAY)["code"] == "free_horizon"


FAR = date(2027, 12, 15)


def _rotation(anchor="2026-01-01"):
    return {
        "id": "cycle-1",
        "name": "Rotation",
        "pattern": [
            {"label": "work_day", "duration": 1},
            {"label": "off", "duration": 1},
        ],
        "anchor_date": anchor,
        "anchor_cycle_day": 1,
        "total_days": 2,
    }


class _Query:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self.kind = "select"
        self.payload = None

    def select(self, *args, **kwargs):
        self.kind = "select"
        return self

    def insert(self, data):
        self.kind = "insert"
        self.payload = data
        return self

    def update(self, data):
        self.kind = "update"
        self.payload = data
        return self

    def eq(self, key, value):
        return self

    def execute(self):
        if self.name == "master_settings" and self.kind == "select":
            return SimpleNamespace(data=[deepcopy(self.store.settings_row)])
        if self.name == "master_settings" and self.kind == "update":
            self.store.settings_row["settings"] = self.payload["settings"]
            self.store.settings_row["version"] = self.payload["version"]
            return SimpleNamespace(data=[deepcopy(self.store.settings_row)])
        if self.name == "command_log" and self.kind == "insert":
            return SimpleNamespace(data=[self.payload])
        return SimpleNamespace(data=[])


class _ShiftStore:
    """Enough of a database for a free user's rotation save to rewrite stored days."""

    def __init__(self):
        self.user = {"id": "user-1", "tier": "free"}
        self.settings_row = {
            "id": "settings-1",
            "user_id": "user-1",
            "version": 1,
            "updated_at": "2026-10-22T00:00:00Z",
            "settings": {
                "cycle": _rotation(),
                "work": {},
                "constraints": [],
                "commitments": [],
                "leave_blocks": [],
                "preferences": {},
            },
        }
        self.cycles = [{
            "id": "cycle-1",
            "user_id": "user-1",
            "is_active": True,
            **_rotation(),
            "cycle_length": 2,
        }]
        self.days = []
        self.client = SimpleNamespace(table=lambda name: _Query(self, name))
        self.reset_later_day()

    def reset_later_day(self):
        self.days = [{
            "user_id": "user-1",
            "date": FAR.isoformat(),
            "work_type": "work_night",
            "cycle_id": "old-cycle",
            "cycle_day": 9,
            "state_json": {
                "engine_version": 1,
                "manual_override": False,
                "commitments": [{"commitment_id": "gone", "name": "Ghost"}],
                "is_leave": False,
            },
        }]

    async def get_user_by_id(self, user_id):
        return dict(self.user)

    async def get_cycles(self, user_id):
        return [deepcopy(row) for row in self.cycles]

    async def get_active_cycle(self, user_id):
        return deepcopy(self.cycles[0])

    async def update_cycle(self, cycle_id, data, user_id=None):
        row = self.cycles[0]
        row.update(data)
        return deepcopy(row)

    async def complete_onboarding_if_open(self, user_id):
        return False

    async def read_leave_blocks(self, user_id):
        return []

    async def get_leave_blocks(self, user_id):
        return []

    async def read_commitments(self, user_id):
        return []

    async def get_commitments(self, user_id):
        return []

    async def read_calendar_days(self, user_id, start, end):
        return [day for day in self.days if start <= day["date"] <= end]

    async def upsert_calendar_days(self, days):
        by_date = {day["date"]: day for day in self.days}
        for day in days:
            by_date[day["date"]] = day
        self.days = list(by_date.values())
        return days

    def later(self):
        return next(day for day in self.days if day["date"] == FAR.isoformat())


def _later_was_rewritten(store):
    day = store.later()
    assert day["work_type"] in {"work_day", "off"}
    assert day["work_type"] != "work_night"
    names = [
        chip.get("name")
        for chip in (day["state_json"].get("commitments") or [])
        if isinstance(chip, dict)
    ]
    assert "Ghost" not in names
    hidden = _free_view({"id": "user-1", "tier": "free"}, store.days)
    assert FAR.isoformat() not in {item["date"] for item in hidden["days"]}
    assert any(item["date"] == FAR.isoformat() for item in store.days)


@pytest.mark.asyncio
async def test_free_cycle_patch_rewrites_days_past_the_horizon(monkeypatch):
    _pin(monkeypatch)
    from app.routes.cycles import UpdateCycleRequest, update_cycle

    store = _ShiftStore()
    monkeypatch.setattr("app.routes.cycles.Database", lambda use_admin=False: store)
    result = await update_cycle(
        "cycle-1",
        UpdateCycleRequest(anchor_date=date(2026, 1, 2)),
        {"id": "user-1", "tier": "free"},
    )
    assert result["success"] is True
    _later_was_rewritten(store)


@pytest.mark.asyncio
async def test_free_master_settings_shift_rewrites_days_past_the_horizon(monkeypatch):
    _pin(monkeypatch)
    from app.routes.master_settings import UpdateSettingsRequest, update_master_settings

    store = _ShiftStore()
    monkeypatch.setattr("app.routes.master_settings.Database", lambda use_admin=False: store)
    shifted = _rotation("2026-01-02")
    result = await update_master_settings(
        UpdateSettingsRequest(
            settings={**store.settings_row["settings"], "cycle": shifted},
            expected_version=1,
        ),
        {"id": "user-1", "tier": "free"},
    )
    assert result["version"] == 2
    _later_was_rewritten(store)


@pytest.mark.asyncio
async def test_free_chat_shift_rewrites_days_past_the_horizon(monkeypatch):
    _pin(monkeypatch)
    store = _ShiftStore()
    executor = create_command_executor(store, "user-1")
    executor.effective_tier = "free"
    outcome = await executor.execute(
        {"action": "update_cycle", "payload": {"shift_by_days": 1}, "explanation": "Move it"},
        source="chat",
    )
    assert outcome["success"] is True
    assert store.settings_row["settings"]["cycle"]["anchor_date"] == "2026-01-02"
    _later_was_rewritten(store)
