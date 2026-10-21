"""Decline must store proposal_status 'declined'.

Production's enum is pending, approved, declined, expired. Writing
'rejected' is what made POST /api/commands/proposals/{id}/review return
500 with Postgres 22P02.
"""

import os

import pytest

from app.routes.commands import proposal_status_for_decision


@pytest.mark.parametrize(
    ("decision", "stored"),
    [
        ("reject", "declined"),
        ("rejected", "declined"),
        ("decline", "declined"),
        ("declined", "declined"),
        ("approve", "approved"),
        ("approved", "approved"),
        ("  Reject  ", "declined"),
    ],
)
def test_review_decisions_map_onto_the_production_enum(decision, stored):
    assert proposal_status_for_decision(decision) == stored


def test_unknown_review_decision_is_rejected():
    with pytest.raises(ValueError):
        proposal_status_for_decision("nope")


def _database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("DATABASE_URL is required so the proposal_status enum regression runs")
    pytest.skip("DATABASE_URL is not set")


@pytest.mark.integration
def test_declined_is_valid_and_rejected_is_not():
    """The live enum, not the label migration 002 happens to create."""
    import psycopg

    database_url = _database_url()
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS decline_regression CASCADE")
        connection.execute("CREATE SCHEMA decline_regression")
        connection.execute(
            """
            CREATE TYPE decline_regression.proposal_status AS ENUM (
                'pending', 'approved', 'declined', 'expired'
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE decline_regression.proposals (
                id uuid PRIMARY KEY,
                status decline_regression.proposal_status NOT NULL DEFAULT 'pending'
            )
            """
        )
        labels = {
            row[0]
            for row in connection.execute(
                """
                SELECT e.enumlabel
                FROM pg_type t
                JOIN pg_enum e ON e.enumtypid = t.oid
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname = 'decline_regression'
                  AND t.typname = 'proposal_status'
                """
            ).fetchall()
        }
        assert labels == {"pending", "approved", "declined", "expired"}
        assert "rejected" not in labels

        proposal_id = connection.execute(
            """
            INSERT INTO decline_regression.proposals (id, status)
            VALUES (gen_random_uuid(), 'pending')
            RETURNING id
            """
        ).fetchone()[0]

        stored = proposal_status_for_decision("reject")
        updated = connection.execute(
            """
            UPDATE decline_regression.proposals
            SET status = %s
            WHERE id = %s AND status = 'pending'
            RETURNING status::text
            """,
            (stored, proposal_id),
        ).fetchone()
        assert stored == "declined"
        assert updated[0] == "declined"

        with pytest.raises(psycopg.errors.InvalidTextRepresentation) as raised:
            connection.execute(
                """
                UPDATE decline_regression.proposals
                SET status = 'rejected'
                WHERE id = %s
                """,
                (proposal_id,),
            )
        assert "proposal_status" in str(raised.value)
        assert "rejected" in str(raised.value)
        still = connection.execute(
            "SELECT status::text FROM decline_regression.proposals WHERE id = %s",
            (proposal_id,),
        ).fetchone()
        assert still[0] == "declined"
        connection.execute("DROP SCHEMA IF EXISTS decline_regression CASCADE")
