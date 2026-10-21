"""A declined proposal stored in Postgres is the same guard the chat uses."""

import pytest
from psycopg.types.json import Json

from app.engines.chat_context import repeats_declined_proposal


@pytest.mark.integration
def test_stored_declined_leave_is_not_reproposed_until_asked(db):
    user_id = db.execute(
        """
        INSERT INTO users (auth_id, email, name)
        VALUES (gen_random_uuid(), 'declined-guard@example.com', 'Guard')
        RETURNING id
        """
    ).fetchone()[0]
    command = {
        "action": "add_leave",
        "payload": {"type": "other", "date": "2026-10-27"},
        "explanation": "Mark Tue 27 Oct as leave",
    }
    db.execute(
        """
        INSERT INTO proposals (user_id, command, status)
        VALUES (%s, %s, 'declined')
        """,
        (user_id, Json(command)),
    )
    row = db.execute(
        """
        SELECT status::text, command, created_at
        FROM proposals
        WHERE user_id = %s
        """,
        (user_id,),
    ).fetchone()
    proposal = {"status": row[0], "command": row[1], "reviewed_at": row[2]}
    tool = {"type": "other", "start_date": "2026-10-27", "end_date": "2026-10-27"}

    assert repeats_declined_proposal(
        "Add a dentist appointment next Wednesday",
        "add_leave",
        tool,
        [proposal],
    )
    assert not repeats_declined_proposal(
        "Please mark 27 Oct as leave again",
        "add_leave",
        tool,
        [proposal],
    )
