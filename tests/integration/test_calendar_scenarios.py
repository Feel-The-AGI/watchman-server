"""Stubbed calendar scenarios against Postgres.

Each case sends one recorded tool call through CommandExecutor, the same
apply path chat uses after the model picks a tool, and checks the stored days.
"""

from uuid import uuid4

import pytest
from psycopg.types.json import Json

from app.database import Database
from app.engines.command_executor import CommandExecutor
from tests.integration.pg_client import PgClient


def _seed(connection, pattern, anchor, anchor_day=1, name="Rotation"):
    user_id = connection.execute(
        """
        INSERT INTO users (auth_id, email, name, timezone)
        VALUES (gen_random_uuid(), %s, 'Scenario', 'Africa/Accra')
        RETURNING id::text
        """,
        (f"scenario-{uuid4()}@example.com",),
    ).fetchone()[0]
    cycle_id = str(uuid4())
    length = sum(block["duration"] for block in pattern)
    connection.execute(
        """
        INSERT INTO cycles (
            id, user_id, name, is_active, pattern, cycle_length,
            anchor_date, anchor_cycle_day
        )
        VALUES (%s, %s, %s, true, %s, %s, %s, %s)
        """,
        (cycle_id, user_id, name, Json(pattern), length, anchor, anchor_day),
    )
    settings = {
        "cycle": {
            "id": cycle_id,
            "name": name,
            "pattern": pattern,
            "anchor_date": anchor,
            "anchor_cycle_day": anchor_day,
            "total_days": length,
        },
        "work": {"shift_hours": 12, "shift_start": "06:00"},
        "constraints": [],
        "commitments": [],
        "leave_blocks": [],
        "preferences": {"timezone": "Africa/Accra"},
    }
    connection.execute(
        """
        INSERT INTO master_settings (user_id, settings, version)
        VALUES (%s, %s, 1)
        """,
        (user_id, Json(settings)),
    )
    return user_id, cycle_id


def _db(connection):
    database = Database.__new__(Database)
    database.client = PgClient(connection)
    database.use_admin = True
    return database


async def _apply(connection, user_id, action, payload):
    executor = CommandExecutor(_db(connection), user_id)
    result = await executor.execute(
        {"action": action, "payload": payload, "explanation": action},
        source="api",
    )
    assert result["success"] is True, result
    return result


def _day(connection, user_id, day):
    row = connection.execute(
        """
        SELECT work_type::text,
               COALESCE((state_json->>'is_leave')::boolean, false),
               state_json->'commitments',
               COALESCE((state_json->>'manual_override')::boolean, false)
        FROM calendar_days
        WHERE user_id = %s AND date = %s
        """,
        (user_id, day),
    ).fetchone()
    assert row is not None, day
    return {
        "work_type": row[0],
        "is_leave": row[1],
        "commitments": row[2] or [],
        "manual_override": row[3],
    }


def _pattern(blocks):
    return [{"label": label, "duration": duration} for label, duration in blocks]


ROTATIONS = [
    (
        "14/14",
        _pattern([("work_day", 14), ("off", 14)]),
        {"2026-01-05": "work_day", "2026-01-18": "work_day", "2026-01-19": "off", "2026-02-01": "off"},
    ),
    (
        "28/28",
        _pattern([("work_day", 28), ("off", 28)]),
        {"2026-01-05": "work_day", "2026-02-01": "work_day", "2026-02-02": "off"},
    ),
    (
        "2-2-3",
        _pattern([
            ("work_day", 2), ("off", 2), ("work_day", 3),
            ("off", 2), ("work_day", 2), ("off", 3),
        ]),
        {"2026-01-05": "work_day", "2026-01-07": "off", "2026-01-09": "work_day", "2026-01-12": "off"},
    ),
    (
        "dupont",
        _pattern([
            ("work_night", 4), ("off", 3), ("work_day", 3), ("off", 1),
            ("work_night", 3), ("off", 3), ("work_day", 4), ("off", 7),
        ]),
        {"2026-01-05": "work_night", "2026-01-09": "off", "2026-01-12": "work_day"},
    ),
    (
        "4on/4off",
        _pattern([("work_day", 4), ("off", 4)]),
        {"2026-01-05": "work_day", "2026-01-08": "work_day", "2026-01-09": "off", "2026-01-12": "off"},
    ),
    (
        "5/2 office",
        _pattern([("work_day", 5), ("off", 2)]),
        {"2026-01-05": "work_day", "2026-01-09": "work_day", "2026-01-10": "off", "2026-01-11": "off", "2026-01-12": "work_day"},
    ),
    (
        "21/7",
        _pattern([("work_day", 21), ("off", 7)]),
        {"2026-01-05": "work_day", "2026-01-25": "work_day", "2026-01-26": "off"},
    ),
    (
        "7 days 7 nights 7 off",
        _pattern([("work_day", 7), ("work_night", 7), ("off", 7)]),
        {"2026-01-05": "work_day", "2026-01-12": "work_night", "2026-01-19": "off", "2026-01-26": "work_day"},
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize("name,pattern,expected", ROTATIONS, ids=[item[0] for item in ROTATIONS])
async def test_rotation_pattern_projects_from_the_anchor(db, name, pattern, expected):
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05", name=name)
    await _apply(db, user_id, "update_cycle", {
        "name": name,
        "pattern": pattern,
        "anchor_date": "2026-01-05",
        "anchor_cycle_day": 1,
    })
    for day, work_type in expected.items():
        assert _day(db, user_id, day)["work_type"] == work_type


@pytest.mark.integration
async def test_anchor_cycle_day_shifts_the_pattern_by_three(db):
    pattern = _pattern([("work_day", 14), ("off", 14)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05", anchor_day=4)
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern,
        "anchor_date": "2026-01-05",
        "anchor_cycle_day": 4,
    })
    # Day 4 of 14-on is still work. Day 15 of the cycle is the first off day,
    # which now falls 11 days after the anchor instead of 14.
    assert _day(db, user_id, "2026-01-05")["work_type"] == "work_day"
    assert _day(db, user_id, "2026-01-16")["work_type"] == "off"


@pytest.mark.integration
async def test_days_before_the_anchor_are_projected(db):
    pattern = _pattern([("work_day", 14), ("off", 14)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern,
        "anchor_date": "2026-01-05",
        "anchor_cycle_day": 1,
    })
    # 1 Jan is four days before the anchor: cycle day 25, which is off.
    assert _day(db, user_id, "2026-01-01")["work_type"] == "off"
    assert _day(db, user_id, "2026-12-31")["work_type"]
    assert _day(db, user_id, "2027-01-01")["work_type"]


@pytest.mark.integration
async def test_year_boundary_continues_the_cycle(db):
    pattern = _pattern([("work_day", 2), ("off", 2)])
    user_id, _cycle_id = _seed(db, pattern, "2026-12-30")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern,
        "anchor_date": "2026-12-30",
        "anchor_cycle_day": 1,
    })
    assert _day(db, user_id, "2026-12-30")["work_type"] == "work_day"
    assert _day(db, user_id, "2026-12-31")["work_type"] == "work_day"
    assert _day(db, user_id, "2027-01-01")["work_type"] == "off"
    assert _day(db, user_id, "2027-01-02")["work_type"] == "off"


@pytest.mark.integration
async def test_leap_day_stays_on_the_cycle(db):
    pattern = _pattern([("work_day", 1), ("off", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2024-02-28")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern,
        "anchor_date": "2024-02-28",
        "anchor_cycle_day": 1,
    })
    assert _day(db, user_id, "2024-02-28")["work_type"] == "work_day"
    assert _day(db, user_id, "2024-02-29")["work_type"] == "off"
    assert _day(db, user_id, "2024-03-01")["work_type"] == "work_day"


@pytest.mark.integration
async def test_manual_override_survives_a_new_pattern(db):
    pattern = _pattern([("work_day", 14), ("off", 14)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern,
        "anchor_date": "2026-01-05",
        "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "override_days", {
        "start_date": "2026-01-10",
        "end_date": "2026-01-10",
        "work_type": "blank",
        "preserve_off_days": False,
    })
    await _apply(db, user_id, "update_cycle", {
        "name": "28/28",
        "pattern": _pattern([("work_day", 28), ("off", 28)]),
        "anchor_date": "2026-03-01",
        "anchor_cycle_day": 1,
    })
    kept = _day(db, user_id, "2026-01-10")
    assert kept["work_type"] == "blank"
    assert kept["manual_override"] is True
    assert _day(db, user_id, "2026-03-01")["work_type"] == "work_day"


@pytest.mark.integration
async def test_unemployed_pattern_has_no_work_days_after_the_anchor(db):
    pattern = _pattern([("work_day", 14), ("off", 14)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern,
        "anchor_date": "2026-01-05",
        "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "update_cycle", {
        "name": "Unemployed",
        "pattern": _pattern([("off", 1)]),
        "anchor_date": "2026-03-01",
        "anchor_cycle_day": 1,
    })
    assert _day(db, user_id, "2026-03-01")["work_type"] == "off"
    assert _day(db, user_id, "2026-06-15")["work_type"] == "off"


@pytest.mark.integration
async def test_one_off_and_weekly_commitments_land_on_the_right_days(db):
    pattern = _pattern([("off", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern,
        "anchor_date": "2026-01-05",
        "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_commitment", {
        "name": "Dentist appointment",
        "type": "personal",
        "start_date": "2026-01-06",
        "end_date": "2026-01-06",
        "time_start": "15:00",
        "time_end": "16:00",
    })
    await _apply(db, user_id, "add_commitment", {
        "name": "Tuesday class",
        "type": "education",
        "start_date": "2026-01-06",
        "end_date": "2026-01-20",
        "days_of_week": ["tuesday"],
        "time_start": "18:00",
        "time_end": "19:00",
    })
    dentist = _day(db, user_id, "2026-01-06")
    assert any(chip.get("name") == "Dentist appointment" for chip in dentist["commitments"])
    # 6 Jan 2026 is a Tuesday, 13 Jan is the next Tuesday, 7 Jan is Wednesday.
    assert any(chip.get("name") == "Tuesday class" for chip in dentist["commitments"])
    assert any(
        chip.get("name") == "Tuesday class"
        for chip in _day(db, user_id, "2026-01-13")["commitments"]
    )
    assert not any(
        chip.get("name") == "Tuesday class"
        for chip in _day(db, user_id, "2026-01-07")["commitments"]
    )


@pytest.mark.integration
async def test_multi_day_event_covers_each_day_in_the_span(db):
    pattern = _pattern([("off", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-02-01")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-02-01", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_commitment", {
        "name": "Wedding trip",
        "type": "personal",
        "start_date": "2026-02-14",
        "end_date": "2026-02-16",
    })
    for day in ("2026-02-14", "2026-02-15", "2026-02-16"):
        names = [chip.get("name") for chip in _day(db, user_id, day)["commitments"]]
        assert "Wedding trip" in names
    assert "Wedding trip" not in [
        chip.get("name") for chip in _day(db, user_id, "2026-02-17")["commitments"]
    ]


@pytest.mark.integration
async def test_removing_a_commitment_clears_its_chip(db):
    pattern = _pattern([("off", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_commitment", {
        "name": "Haircut",
        "type": "personal",
        "start_date": "2026-01-08",
        "end_date": "2026-01-08",
        "time_start": "16:00",
        "time_end": "17:00",
    })
    commitment_id = db.execute(
        "SELECT id::text FROM commitments WHERE user_id = %s AND name = 'Haircut'",
        (user_id,),
    ).fetchone()[0]
    await _apply(db, user_id, "remove_commitment", {"id": commitment_id})
    assert _day(db, user_id, "2026-01-08")["commitments"] == []


@pytest.mark.integration
async def test_leave_marks_the_range_and_a_commitment_stays(db):
    pattern = _pattern([("work_day", 5), ("off", 2)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_commitment", {
        "name": "Dentist appointment",
        "type": "personal",
        "start_date": "2026-01-06",
        "end_date": "2026-01-06",
    })
    await _apply(db, user_id, "add_leave", {
        "name": "Annual leave",
        "type": "annual",
        "start_date": "2026-01-05",
        "end_date": "2026-01-07",
    })
    for day in ("2026-01-05", "2026-01-06", "2026-01-07"):
        assert _day(db, user_id, day)["is_leave"] is True
    assert _day(db, user_id, "2026-01-08")["is_leave"] is False
    assert any(
        chip.get("name") == "Dentist appointment"
        for chip in _day(db, user_id, "2026-01-06")["commitments"]
    )


@pytest.mark.integration
async def test_sick_day_in_the_past_is_leave(db):
    pattern = _pattern([("work_day", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-01")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-01", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_leave", {
        "name": "Sick",
        "type": "sick",
        "date": "2026-01-03",
    })
    assert _day(db, user_id, "2026-01-03")["is_leave"] is True
    assert _day(db, user_id, "2026-01-04")["is_leave"] is False


@pytest.mark.integration
async def test_removing_leave_clears_the_flag(db):
    pattern = _pattern([("work_day", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-01")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-01", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_leave", {
        "name": "Annual leave",
        "type": "annual",
        "start_date": "2026-01-10",
        "end_date": "2026-01-12",
    })
    leave_id = db.execute(
        "SELECT id::text FROM leave_blocks WHERE user_id = %s",
        (user_id,),
    ).fetchone()[0]
    await _apply(db, user_id, "remove_leave", {"id": leave_id})
    assert _day(db, user_id, "2026-01-11")["is_leave"] is False


@pytest.mark.integration
async def test_override_flips_a_work_day_and_an_off_day(db):
    pattern = _pattern([("work_day", 5), ("off", 2)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "override_days", {
        "start_date": "2026-01-05",
        "end_date": "2026-01-05",
        "work_type": "off",
        "preserve_off_days": False,
    })
    await _apply(db, user_id, "override_days", {
        "start_date": "2026-01-10",
        "end_date": "2026-01-10",
        "work_type": "work_day",
        "preserve_off_days": False,
    })
    assert _day(db, user_id, "2026-01-05")["work_type"] == "off"
    assert _day(db, user_id, "2026-01-10")["work_type"] == "work_day"
    assert _day(db, user_id, "2026-01-10")["manual_override"] is True


@pytest.mark.integration
async def test_undo_restores_the_previous_rotation(db):
    pattern = _pattern([("work_day", 14), ("off", 14)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "update_cycle", {
        "pattern": _pattern([("off", 1)]),
        "anchor_date": "2026-01-05",
        "anchor_cycle_day": 1,
    })
    assert _day(db, user_id, "2026-01-05")["work_type"] == "off"
    db.execute(
        "UPDATE command_log SET created_at = created_at - interval '1 hour' WHERE user_id = %s",
        (user_id,),
    )
    db.execute(
        """
        UPDATE command_log
        SET created_at = now()
        WHERE user_id = %s AND payload->'pattern'->0->>'duration' = '1'
        """,
        (user_id,),
    )
    await _apply(db, user_id, "undo", {})
    assert _day(db, user_id, "2026-01-05")["work_type"] == "work_day"
    assert _day(db, user_id, "2026-01-19")["work_type"] == "off"


@pytest.mark.integration
async def test_a_declined_proposal_does_not_change_the_calendar(db):
    pattern = _pattern([("work_day", 5), ("off", 2)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    before = _day(db, user_id, "2026-01-06")["work_type"]
    executor = CommandExecutor(_db(db), user_id)
    proposal = await executor.create_proposal({
        "action": "add_leave",
        "payload": {"name": "Leave", "type": "annual", "start_date": "2026-01-06", "end_date": "2026-01-06"},
        "explanation": "Mark leave",
    })
    db.execute(
        "UPDATE proposals SET status = 'declined', reviewed_at = now() WHERE id = %s",
        (proposal["id"],),
    )
    assert _day(db, user_id, "2026-01-06")["is_leave"] is False
    assert _day(db, user_id, "2026-01-06")["work_type"] == before


@pytest.mark.integration
async def test_two_pending_proposals_can_both_sit_unapplied(db):
    pattern = _pattern([("off", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    executor = CommandExecutor(_db(db), user_id)
    first = await executor.create_proposal({
        "action": "add_commitment",
        "payload": {"name": "Dentist", "type": "personal", "start_date": "2026-01-06", "end_date": "2026-01-06"},
        "explanation": "Add dentist",
    })
    second = await executor.create_proposal({
        "action": "add_leave",
        "payload": {"name": "Leave", "type": "annual", "start_date": "2026-01-08", "end_date": "2026-01-08"},
        "explanation": "Add leave",
    })
    rows = db.execute(
        "SELECT status::text FROM proposals WHERE user_id = %s ORDER BY created_at",
        (user_id,),
    ).fetchall()
    assert [row[0] for row in rows] == ["pending", "pending"]
    assert first["id"] != second["id"]
    count = db.execute(
        "SELECT count(*) FROM calendar_days WHERE user_id = %s",
        (user_id,),
    ).fetchone()[0]
    assert count == 0


@pytest.mark.integration
async def test_christmas_on_a_14_14_anchored_21_oct_is_a_day_shift(db):
    pattern = _pattern([("work_day", 14), ("off", 14)])
    user_id, _cycle_id = _seed(db, pattern, "2026-10-21")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-10-21", "anchor_cycle_day": 1,
    })
    assert _day(db, user_id, "2026-12-25")["work_type"] == "work_day"
    assert _day(db, user_id, "2026-10-20")["work_type"] == "off"


@pytest.mark.integration
async def test_a_side_gig_does_not_change_the_work_type(db):
    pattern = _pattern([("off", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_commitment", {
        "name": "Side gig",
        "type": "work",
        "start_date": "2026-01-07",
        "end_date": "2026-01-07",
        "time_start": "18:00",
        "time_end": "21:00",
    })
    day = _day(db, user_id, "2026-01-07")
    assert day["work_type"] == "off"
    assert any(chip.get("name") == "Side gig" for chip in day["commitments"])


@pytest.mark.integration
async def test_leave_can_cross_a_weekend_in_a_5_2(db):
    pattern = _pattern([("work_day", 5), ("off", 2)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_leave", {
        "name": "Long weekend",
        "type": "annual",
        "start_date": "2026-01-09",
        "end_date": "2026-01-11",
    })
    assert _day(db, user_id, "2026-01-09")["work_type"] == "work_day"
    assert _day(db, user_id, "2026-01-09")["is_leave"] is True
    assert _day(db, user_id, "2026-01-10")["work_type"] == "off"
    assert _day(db, user_id, "2026-01-10")["is_leave"] is True


@pytest.mark.integration
async def test_a_commitment_survives_a_pattern_change(db):
    pattern = _pattern([("work_day", 14), ("off", 14)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_commitment", {
        "name": "Dentist appointment",
        "type": "personal",
        "start_date": "2026-02-03",
        "end_date": "2026-02-03",
    })
    await _apply(db, user_id, "update_cycle", {
        "pattern": _pattern([("work_night", 7), ("off", 7)]),
        "anchor_date": "2026-01-05",
        "anchor_cycle_day": 1,
    })
    day = _day(db, user_id, "2026-02-03")
    assert any(chip.get("name") == "Dentist appointment" for chip in day["commitments"])
    assert day["work_type"] == "work_night"


@pytest.mark.integration
async def test_an_expired_proposal_is_not_applied(db):
    pattern = _pattern([("off", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    executor = CommandExecutor(_db(db), user_id)
    proposal = await executor.create_proposal({
        "action": "add_leave",
        "payload": {"name": "Leave", "type": "annual", "start_date": "2026-04-01", "end_date": "2026-04-01"},
        "explanation": "Add leave",
    })
    db.execute(
        "UPDATE proposals SET expires_at = now() - interval '1 day' WHERE id = %s",
        (proposal["id"],),
    )
    assert _day(db, user_id, "2026-04-01")["is_leave"] is False


@pytest.mark.integration
async def test_overtime_on_an_off_day_is_an_override(db):
    pattern = _pattern([("work_day", 5), ("off", 2)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "override_days", {
        "start_date": "2026-01-10",
        "end_date": "2026-01-11",
        "work_type": "work_day",
        "preserve_off_days": False,
    })
    assert _day(db, user_id, "2026-01-10")["work_type"] == "work_day"
    assert _day(db, user_id, "2026-01-11")["work_type"] == "work_day"
    assert _day(db, user_id, "2026-01-09")["work_type"] == "work_day"


@pytest.mark.integration
async def test_a_night_override_replaces_a_day_shift(db):
    pattern = _pattern([("work_day", 7), ("off", 7)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "override_days", {
        "start_date": "2026-01-06",
        "end_date": "2026-01-06",
        "work_type": "work_night",
        "preserve_off_days": False,
    })
    assert _day(db, user_id, "2026-01-06")["work_type"] == "work_night"
    assert _day(db, user_id, "2026-01-07")["work_type"] == "work_day"


@pytest.mark.integration
async def test_a_weekly_commitment_stops_on_its_end_date(db):
    pattern = _pattern([("off", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-01")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-01", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_commitment", {
        "name": "Tuesday class",
        "type": "education",
        "start_date": "2026-01-06",
        "end_date": "2026-01-13",
        "days_of_week": ["tuesday"],
    })
    assert any(chip.get("name") == "Tuesday class" for chip in _day(db, user_id, "2026-01-13")["commitments"])
    assert not any(chip.get("name") == "Tuesday class" for chip in _day(db, user_id, "2026-01-20")["commitments"])


@pytest.mark.integration
async def test_two_commitments_can_share_a_day(db):
    pattern = _pattern([("off", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_commitment", {
        "name": "Dentist appointment", "type": "personal",
        "start_date": "2026-01-06", "end_date": "2026-01-06",
    })
    await _apply(db, user_id, "add_commitment", {
        "name": "Haircut", "type": "personal",
        "start_date": "2026-01-06", "end_date": "2026-01-06",
        "time_start": "16:00", "time_end": "17:00",
    })
    names = {chip.get("name") for chip in _day(db, user_id, "2026-01-06")["commitments"]}
    assert names == {"Dentist appointment", "Haircut"}


@pytest.mark.integration
async def test_a_blank_before_the_anchor_survives_regeneration(db):
    pattern = _pattern([("work_day", 14), ("off", 14)])
    user_id, _cycle_id = _seed(db, pattern, "2026-03-01")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-03-01", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "override_days", {
        "start_date": "2026-01-15",
        "end_date": "2026-01-15",
        "work_type": "blank",
        "preserve_off_days": False,
    })
    await _apply(db, user_id, "add_leave", {
        "name": "Sick", "type": "sick", "date": "2026-03-02",
    })
    assert _day(db, user_id, "2026-01-15")["work_type"] == "blank"
    assert _day(db, user_id, "2026-01-15")["manual_override"] is True
    assert _day(db, user_id, "2026-03-02")["is_leave"] is True


@pytest.mark.integration
async def test_other_leave_is_stored_as_leave_not_a_commitment(db):
    pattern = _pattern([("work_day", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-10-21")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-10-21", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_leave", {
        "type": "other", "date": "2026-10-31",
    })
    assert _day(db, user_id, "2026-10-31")["is_leave"] is True
    count = db.execute(
        "SELECT count(*) FROM commitments WHERE user_id = %s",
        (user_id,),
    ).fetchone()[0]
    assert count == 0


@pytest.mark.integration
async def test_extending_a_hitch_by_overriding_the_following_off_days(db):
    pattern = _pattern([("work_day", 14), ("off", 14)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "override_days", {
        "start_date": "2026-01-19",
        "end_date": "2026-01-22",
        "work_type": "work_day",
        "preserve_off_days": False,
    })
    for day in ("2026-01-19", "2026-01-20", "2026-01-21", "2026-01-22"):
        assert _day(db, user_id, day)["work_type"] == "work_day"
    assert _day(db, user_id, "2026-01-23")["work_type"] == "off"


@pytest.mark.integration
async def test_shortening_leave_is_a_remove_and_a_shorter_add(db):
    pattern = _pattern([("work_day", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-01")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-01", "anchor_cycle_day": 1,
    })
    await _apply(db, user_id, "add_leave", {
        "name": "Annual leave", "type": "annual",
        "start_date": "2026-02-01", "end_date": "2026-02-05",
    })
    leave_id = db.execute(
        "SELECT id::text FROM leave_blocks WHERE user_id = %s", (user_id,),
    ).fetchone()[0]
    await _apply(db, user_id, "remove_leave", {"id": leave_id})
    await _apply(db, user_id, "add_leave", {
        "name": "Annual leave", "type": "annual",
        "start_date": "2026-02-01", "end_date": "2026-02-03",
    })
    assert _day(db, user_id, "2026-02-03")["is_leave"] is True
    assert _day(db, user_id, "2026-02-04")["is_leave"] is False


@pytest.mark.integration
async def test_a_second_pending_proposal_stays_pending_after_the_first_is_applied(db):
    pattern = _pattern([("off", 1)])
    user_id, _cycle_id = _seed(db, pattern, "2026-01-05")
    await _apply(db, user_id, "update_cycle", {
        "pattern": pattern, "anchor_date": "2026-01-05", "anchor_cycle_day": 1,
    })
    executor = CommandExecutor(_db(db), user_id)
    dentist = await executor.create_proposal({
        "action": "add_commitment",
        "payload": {"name": "Dentist", "type": "personal", "start_date": "2026-01-06", "end_date": "2026-01-06"},
        "explanation": "Add dentist",
    })
    leave = await executor.create_proposal({
        "action": "add_leave",
        "payload": {"name": "Leave", "type": "annual", "start_date": "2026-01-08", "end_date": "2026-01-08"},
        "explanation": "Add leave",
    })
    await executor.execute(dentist["command"], source="api", proposal_id=dentist["id"])
    status = db.execute(
        "SELECT status::text FROM proposals WHERE id = %s",
        (leave["id"],),
    ).fetchone()[0]
    assert status == "pending"
    assert _day(db, user_id, "2026-01-08")["is_leave"] is False
    assert any(
        chip.get("name") == "Dentist"
        for chip in _day(db, user_id, "2026-01-06")["commitments"]
    )
