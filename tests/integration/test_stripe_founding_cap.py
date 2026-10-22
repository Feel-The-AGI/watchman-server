"""The founding trigger rejects the 101st seat and keeps a repeat insert from counting twice."""

import uuid

import pytest


@pytest.mark.integration
def test_founding_cap_rejects_the_101st_seat(db):
    db.execute(
        """
        INSERT INTO stripe_founding_seats (status)
        SELECT 'active' FROM generate_series(1, 100)
        """
    )
    taken = db.execute("SELECT COUNT(*) FROM stripe_founding_seats").fetchone()[0]
    assert taken == 100

    db.execute("SAVEPOINT before_cap")
    with pytest.raises(Exception) as exc:
        db.execute(
            "INSERT INTO stripe_founding_seats (status) VALUES ('active')"
        )
    assert "founding_cap" in str(exc.value)
    db.execute("ROLLBACK TO SAVEPOINT before_cap")
    assert db.execute("SELECT COUNT(*) FROM stripe_founding_seats").fetchone()[0] == 100


@pytest.mark.integration
def test_same_founding_user_does_not_take_a_second_seat(db):
    user_id = db.execute(
        """
        INSERT INTO users (auth_id, email, name)
        VALUES (%s, %s, 'Founder')
        RETURNING id
        """,
        (uuid.uuid4(), "founder@example.com"),
    ).fetchone()[0]
    db.execute(
        """
        INSERT INTO stripe_founding_seats (user_id, stripe_subscription_id, status)
        VALUES (%s, 'sub_one', 'active')
        """,
        (user_id,),
    )
    db.execute("SAVEPOINT before_repeat")
    with pytest.raises(Exception) as exc:
        db.execute(
            """
            INSERT INTO stripe_founding_seats (user_id, stripe_subscription_id, status)
            VALUES (%s, 'sub_two', 'active')
            """,
            (user_id,),
        )
    assert "founding_cap" not in str(exc.value)
    db.execute("ROLLBACK TO SAVEPOINT before_repeat")
    assert db.execute(
        "SELECT COUNT(*) FROM stripe_founding_seats WHERE user_id = %s",
        (user_id,),
    ).fetchone()[0] == 1
