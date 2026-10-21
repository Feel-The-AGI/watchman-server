"""The approve and decline writes, against Postgres.

Decline stores proposal_status declined. Production does not have rejected. Approve
used to report success without a commitments row or a calendar chip.
"""

from datetime import date
from pathlib import Path

import pytest

from app.engines.proposal_effects import commitment_record, leave_record, stamp_day_state
from app.routes.commands import proposal_status_for_decision
from tests.integration.sql_script import execute_sql_file, migration_paths

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
BOOTSTRAP = Path(__file__).resolve().parent / "bootstrap.sql"

DENTIST = {
    "name": "Dentist appointment",
    "type": "personal",
    "start_date": "2026-10-27",
    "end_date": "2026-10-27",
    "time_start": "15:00",
    "time_end": "16:00",
    "days_of_week": ["tuesday"],
}


def _enum_labels(connection, name):
    rows = connection.execute(
        """
        SELECT e.enumlabel
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public' AND t.typname = %s
        """,
        (name,),
    ).fetchall()
    return {row[0] for row in rows}


@pytest.mark.integration
def test_production_enum_accepts_declined_and_rejects_rejected(database_url):
    """A database whose proposal_status matches production, then migration 014."""
    import psycopg

    drifted = (MIGRATIONS / "002_overhaul_architecture.sql").read_text(encoding="utf-8")
    original = "CREATE TYPE proposal_status AS ENUM ('pending', 'approved', 'rejected', 'expired');"
    without_rejected = "CREATE TYPE proposal_status AS ENUM ('pending', 'approved', 'expired');"
    assert original in drifted
    drifted_path = Path("/tmp/002_overhaul_architecture_drift.sql")
    drifted_path.write_text(drifted.replace(original, without_rejected), encoding="utf-8")

    admin = psycopg.connect(database_url, autocommit=True)
    admin.execute("DROP DATABASE IF EXISTS watchman_drift")
    admin.execute("CREATE DATABASE watchman_drift")
    admin.close()

    drift_url = database_url.rsplit("/", 1)[0] + "/watchman_drift"
    try:
        with psycopg.connect(drift_url, autocommit=True) as connection:
            execute_sql_file(connection, BOOTSTRAP)
            for path in migration_paths(MIGRATIONS):
                if path.name == "002_overhaul_architecture.sql":
                    execute_sql_file(connection, drifted_path)
                elif path.name == "014_review_enum_gaps.sql":
                    break
                else:
                    execute_sql_file(connection, path)
            assert "rejected" not in _enum_labels(connection, "proposal_status")
            assert "other" not in _enum_labels(connection, "commitment_type")

            user_id = connection.execute(
                """
                INSERT INTO users (auth_id, email, name)
                VALUES (gen_random_uuid(), 'drift@example.com', 'Drift')
                RETURNING id
                """
            ).fetchone()[0]
            proposal_id = connection.execute(
                """
                INSERT INTO proposals (user_id, command, status)
                VALUES (%s, '{"action":"add_leave"}'::jsonb, 'pending')
                RETURNING id
                """,
                (user_id,),
            ).fetchone()[0]
            with pytest.raises(psycopg.errors.InvalidTextRepresentation):
                connection.execute(
                    "UPDATE proposals SET status = 'rejected' WHERE id = %s",
                    (proposal_id,),
                )

            execute_sql_file(connection, MIGRATIONS / "014_review_enum_gaps.sql")
            execute_sql_file(connection, MIGRATIONS / "014_review_enum_gaps.sql")
            stored = proposal_status_for_decision("reject")
            updated = connection.execute(
                """
                UPDATE proposals SET status = %s, reviewed_at = now()
                WHERE id = %s AND status = 'pending'
                RETURNING status::text
                """,
                (stored, proposal_id),
            ).fetchone()
            assert stored == "declined"
            assert updated[0] == "declined"
            with pytest.raises(psycopg.errors.InvalidTextRepresentation):
                connection.execute(
                    "UPDATE proposals SET status = 'rejected' WHERE id = %s",
                    (proposal_id,),
                )
            assert "other" in _enum_labels(connection, "commitment_type")
            assert "declined" in _enum_labels(connection, "proposal_status")
    finally:
        admin = psycopg.connect(database_url, autocommit=True)
        admin.execute("DROP DATABASE IF EXISTS watchman_drift")
        admin.close()


@pytest.mark.integration
def test_dentist_commitment_and_leave_rows_match_the_live_schema(db):
    from psycopg.types.json import Json

    user_id = db.execute(
        """
        INSERT INTO users (auth_id, email, name)
        VALUES (gen_random_uuid(), 'dentist@example.com', 'Dentist')
        RETURNING id
        """
    ).fetchone()[0]
    db.execute(
        """
        INSERT INTO calendar_days (user_id, date, work_type, state_json)
        VALUES (%s, '2026-10-27', 'off', '{"commitments": [], "tags": []}'::jsonb)
        """,
        (user_id,),
    )
    record = commitment_record(str(user_id), DENTIST, "11111111-1111-1111-1111-111111111111")
    db.execute(
        """
        INSERT INTO commitments (
            id, user_id, name, type, status, priority, constraints_json,
            start_date, end_date, recurrence, source, source_text, color
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            record["id"], record["user_id"], record["name"], record["type"],
            record["status"], record["priority"], Json(record["constraints_json"]),
            record["start_date"], record["end_date"], Json(record["recurrence"]),
            record["source"], record["source_text"], record["color"],
        ),
    )
    state = stamp_day_state(
        {"commitments": [], "tags": [], "available_hours": 12, "used_hours": 0},
        date(2026, 10, 27),
        [record],
        [],
    )
    db.execute(
        """
        UPDATE calendar_days SET state_json = %s
        WHERE user_id = %s AND date = '2026-10-27'
        """,
        (Json(state), user_id),
    )
    stored = db.execute(
        """
        SELECT name, type::text, start_date::text
        FROM commitments WHERE user_id = %s
        """,
        (user_id,),
    ).fetchone()
    assert stored == ("Dentist appointment", "personal", "2026-10-27")
    painted = db.execute(
        """
        SELECT state_json->'commitments'->0->>'name'
        FROM calendar_days WHERE user_id = %s AND date = '2026-10-27'
        """,
        (user_id,),
    ).fetchone()
    assert painted[0] == "Dentist appointment"

    leave = leave_record(
        str(user_id),
        {"type": "other", "date": "2026-10-31"},
        "22222222-2222-2222-2222-222222222222",
    )
    db.execute(
        """
        INSERT INTO leave_blocks (id, user_id, name, start_date, end_date, effects, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            leave["id"], leave["user_id"], leave["name"], leave["start_date"],
            leave["end_date"], Json(leave["effects"]), leave["notes"],
        ),
    )
    other = commitment_record(
        str(user_id),
        {"name": "Something else", "type": "other", "start_date": "2026-11-01", "end_date": "2026-11-01"},
        "33333333-3333-3333-3333-333333333333",
    )
    db.execute(
        """
        INSERT INTO commitments (id, user_id, name, type, start_date, end_date)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (other["id"], other["user_id"], other["name"], other["type"], other["start_date"], other["end_date"]),
    )
    kinds = db.execute(
        "SELECT type::text FROM commitments WHERE user_id = %s ORDER BY name",
        (user_id,),
    ).fetchall()
    assert [row[0] for row in kinds] == ["personal", "other"]
