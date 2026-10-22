"""Master Settings stays authoritative, and the onboarding night-shift rule is read."""

from datetime import date

import pytest

from app.engines.calendar_authority import (
    annotate_night_study,
    commitment_is_study,
    effective_constraints,
    engine_cycle,
    is_night_study_rule,
    may_replace_cycle,
    merge_constraints,
    night_shift_dates,
    projection_inputs,
    remember_cycle,
)
from app.engines.calendar_engine import CalendarEngine
from app.engines.command_executor import CommandExecutor


ONBOARDING_RULE = {
    "id": "night-rule",
    "name": "No study on night shifts",
    "is_active": True,
    "is_system": True,
    "rule": {"type": "no_activity_on", "activity": "study", "work_types": ["work_night"]},
}

ROTATION = {
    "id": "chat-cycle",
    "pattern": [
        {"label": "work_day", "duration": 1},
        {"label": "work_night", "duration": 1},
        {"label": "off", "duration": 1},
    ],
    "anchor_date": "2026-10-21",
    "anchor_cycle_day": 1,
    "total_days": 3,
}


def test_onboarding_rule_is_recognized_and_inactive_rules_are_not():
    client_rule = {"is_active": True, "rule": {"type": "no_study_on", "value": "work_night"}}
    assert is_night_study_rule(ONBOARDING_RULE)
    assert is_night_study_rule(client_rule)
    assert is_night_study_rule({"is_active": True, "rule": {"type": "no_study_on", "values": ["work_night"]}})
    assert is_night_study_rule({"active": True, "rule": "no_study_on_night_shift"})
    assert is_night_study_rule({"active": True, "rule": "no_study_on:work_night"})
    assert not is_night_study_rule({**ONBOARDING_RULE, "is_active": False})
    assert not is_night_study_rule({"is_active": True, "rule": {"type": "no_study_on", "value": "work_day"}})
    assert not is_night_study_rule({
        "is_active": True,
        "rule": {"type": "no_activity_on", "activity": "study", "work_types": ["off"]},
    })


def test_document_constraints_win_over_a_duplicate_relational_rule():
    document = [{"id": "kept", "active": False, "rule": "no_study_on_night_shift"}]
    merged = merge_constraints(document, [ONBOARDING_RULE])
    assert merged == document
    assert not any(is_night_study_rule(item) for item in merged)


def test_night_shift_dates_follow_the_cycle():
    # 21 Oct is day 1 (day shift), 22 Oct is night, 23 Oct is off.
    nights = night_shift_dates(ROTATION, {
        "type": "education",
        "start_date": "2026-10-21",
        "end_date": "2026-10-23",
    })
    assert nights == ["2026-10-22"]
    assert night_shift_dates(ROTATION, {"type": "education", "name": "Course"}) == []
    assert commitment_is_study({"type": "education"})
    assert not commitment_is_study({"type": "personal"})


def test_generation_marks_only_night_shifts_when_the_rule_is_active():
    engine = CalendarEngine("user")
    prepared = engine_cycle(ROTATION)
    days = engine.generate_range(
        date(2026, 10, 21),
        date(2026, 10, 23),
        prepared,
        [],
        [ONBOARDING_RULE],
    )
    by_date = {day.date.isoformat(): day for day in days}
    assert by_date["2026-10-21"].state_json["study_allowed"] is True
    assert by_date["2026-10-22"].state_json["study_allowed"] is False
    assert "study" in by_date["2026-10-22"].state_json["blocked_activities"]
    assert by_date["2026-10-23"].state_json["study_allowed"] is True

    unmarked = engine.generate_range(date(2026, 10, 22), date(2026, 10, 22), prepared, [])
    assert "study_allowed" not in unmarked[0].state_json

    client_days = engine.generate_range(
        date(2026, 10, 22),
        date(2026, 10, 22),
        prepared,
        [],
        [{"is_active": True, "rule": {"type": "no_study_on", "value": "work_night"}}],
    )
    assert client_days[0].state_json["study_allowed"] is False


def test_master_settings_cycle_is_not_replaced_by_a_different_row():
    table_cycle = {
        "id": "old-row",
        "name": "Old",
        "pattern": [{"label": "off", "duration": 2}],
        "anchor_date": "2020-01-01",
        "anchor_cycle_day": 1,
        "cycle_length": 2,
    }
    chosen = projection_inputs(
        {"cycle": ROTATION, "leave_blocks": [], "constraints": []},
        table_cycle,
        [{"name": "Table leave", "start_date": "2026-01-01", "end_date": "2026-01-02"}],
        [ONBOARDING_RULE],
    )
    assert chosen["cycle"]["id"] == "chat-cycle"
    assert chosen["leave_blocks"] == []
    assert may_replace_cycle(ROTATION, table_cycle) is False
    assert may_replace_cycle(None, table_cycle) is True
    assert may_replace_cycle({"id": "old-row", "pattern": [{"label": "off", "duration": 1}]}, table_cycle)


def test_empty_document_reads_relational_cycle_without_dropping_it():
    table_cycle = {
        "id": "old-row",
        "pattern": [{"label": "work_night", "duration": 4}],
        "anchor_date": "2026-01-01",
        "anchor_cycle_day": 1,
        "cycle_length": 4,
    }
    chosen = projection_inputs({"cycle": None, "leave_blocks": [], "constraints": []}, table_cycle, [], [])
    assert chosen["cycle"]["id"] == "old-row"
    assert chosen["cycle"]["total_days"] == 4


class MemorySettings:
    def __init__(self, cycle):
        self.cycle = cycle
        self.writes = []

    async def get(self, user_id):
        return {"settings": {"cycle": self.cycle, "constraints": [], "leave_blocks": []}, "version": 1}

    async def update_section(self, user_id, section, value):
        self.writes.append(value)
        self.cycle = value
        return {"settings": {"cycle": value}}


SAVED_ROW = {
    "id": "saved-row",
    "name": "My Rotation",
    "pattern": [{"label": "work_night", "duration": 5}, {"label": "off", "duration": 5}],
    "anchor_date": "2026-10-21",
    "anchor_cycle_day": 1,
    "cycle_length": 10,
}


@pytest.mark.asyncio
async def test_cycle_create_copies_into_an_empty_document_only():
    """A new row fills an empty document. A different cycle id does not replace one."""
    empty = MemorySettings(None)
    wrote = await remember_cycle(None, "user", SAVED_ROW, service=empty)
    assert wrote is True
    assert empty.writes[-1]["id"] == "saved-row"
    assert empty.cycle["pattern"][0]["label"] == "work_night"

    occupied = MemorySettings(ROTATION)
    wrote = await remember_cycle(None, "user", SAVED_ROW, service=occupied)
    assert wrote is False
    assert occupied.writes == []
    assert occupied.cycle["id"] == "chat-cycle"

    same = MemorySettings(dict(ROTATION))
    wrote = await remember_cycle(None, "user", {**SAVED_ROW, "id": "chat-cycle"}, service=same)
    assert wrote is True
    assert same.cycle["id"] == "chat-cycle"
    assert same.cycle["anchor_date"] == "2026-10-21"


@pytest.mark.asyncio
async def test_agent_blocks_study_on_a_real_night_and_allows_an_off_day():
    class DB:
        async def get_active_constraints(self, user_id):
            return [{
                "id": "client-rule",
                "is_active": True,
                "rule": {"type": "no_study_on", "value": "work_night"},
            }]

    executor = CommandExecutor.__new__(CommandExecutor)
    executor.db = DB()
    executor.user_id = "user"
    settings = {"cycle": ROTATION, "constraints": [], "leave_blocks": []}

    blocked = await executor._check_constraints("add_commitment", {
        "type": "education",
        "name": "Night class",
        "start_date": "2026-10-22",
        "end_date": "2026-10-22",
    }, settings)
    assert blocked[0][0]["rule"] == "no_activity_on"
    assert "2026-10-22" in blocked[0][0]["dates"]

    allowed = await executor._check_constraints("add_commitment", {
        "type": "education",
        "name": "Off day class",
        "start_date": "2026-10-23",
        "end_date": "2026-10-23",
    }, settings)
    assert allowed[0] == []

    personal = await executor._check_constraints("add_commitment", {
        "type": "personal",
        "start_date": "2026-10-22",
        "end_date": "2026-10-22",
    }, settings)
    assert personal[0] == []


@pytest.mark.asyncio
async def test_effective_constraints_include_the_onboarding_row():
    class DB:
        async def get_active_constraints(self, user_id):
            return [ONBOARDING_RULE]

    merged = await effective_constraints(DB(), "user", [])
    assert merged[0]["id"] == "night-rule"
