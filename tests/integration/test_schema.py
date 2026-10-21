"""Migrations against a real Postgres, and the columns the app reads and writes.

These checks fail when a fresh database is missing a column or enum label the
running code already uses. That is the gap that shipped proposals without
expires_at, and calendar days whose work_type could not be 'blank'.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"

# Columns named in inserts, updates, filters, and explicit selects.
# select("*") does not list columns, so those writes are recorded here.
CODE_COLUMNS = {
    "users": {
        "id", "auth_id", "email", "name", "timezone", "tier", "role",
        "onboarding_completed", "settings", "created_at", "updated_at",
        "paystack_customer_code", "paystack_subscription_code",
        "stripe_customer_id", "stripe_subscription_id", "country",
        "country_code", "region", "city", "last_active", "signup_source",
        "last_ip", "pro_expires_at",
    },
    "payments": {
        "id", "user_id", "amount", "currency", "status", "description",
        "created_at", "paystack_reference", "amount_local", "currency_local",
        "exchange_rate", "access_until", "stripe_invoice_id",
    },
    "subscriptions": {
        "id", "user_id", "tier", "status", "current_period_start",
        "current_period_end", "cancel_at_period_end", "created_at", "updated_at",
    },
    "cycles": {
        "id", "user_id", "name", "is_active", "pattern", "cycle_length",
        "anchor_date", "anchor_cycle_day", "crew", "description",
        "created_at", "updated_at",
    },
    "constraints": {
        "id", "user_id", "name", "description", "is_active", "rule", "weight",
        "is_system", "created_at", "updated_at",
    },
    "commitments": {
        "id", "user_id", "name", "type", "status", "priority", "constraints_json",
        "start_date", "end_date", "recurrence", "total_sessions",
        "completed_sessions", "source", "source_text", "color", "icon", "notes",
        "created_at", "updated_at",
    },
    "leave_blocks": {
        "id", "user_id", "name", "start_date", "end_date", "effects", "notes",
        "created_at", "updated_at",
    },
    "calendar_days": {
        "id", "user_id", "date", "cycle_id", "cycle_day", "work_type",
        "state_json", "created_at", "updated_at",
    },
    "mutations_log": {
        "id", "user_id", "status", "intent", "proposed_diff", "explanation",
        "proposed_at", "reviewed_at", "created_at",
    },
    "calendar_snapshots": {
        "id", "user_id", "state_hash", "snapshot", "created_at",
    },
    "daily_logs": {
        "id", "user_id", "date", "note", "actual_hours", "overtime_hours",
        "created_at", "updated_at",
    },
    "incidents": {
        "id", "user_id", "date", "type", "severity", "title", "description",
        "reported_to", "witnesses", "outcome", "created_at", "updated_at",
    },
    "calendar_shares": {
        "id", "user_id", "share_code", "name", "show_commitments",
        "show_work_types", "is_active", "view_count", "created_at", "updated_at",
    },
    "master_settings": {
        "id", "user_id", "settings", "version", "created_at", "updated_at",
    },
    "command_log": {
        "id", "user_id", "action", "payload", "before_state", "after_state",
        "status", "source", "message_id", "explanation", "created_at",
    },
    "chat_messages": {
        "id", "user_id", "role", "content", "command_id", "metadata", "created_at",
    },
    "proposals": {
        "id", "user_id", "command", "validation", "status", "message_id",
        "created_at", "reviewed_at", "expires_at",
    },
    "admin_stats_cache": {
        "id", "stat_key", "stat_value", "calculated_at", "expires_at",
    },
}

ENUMS = {
    "work_type": {"work_day", "work_night", "off", "blank"},
    "proposal_status": {"pending", "approved", "rejected", "declined", "expired"},
    "commitment_type": {"work", "education", "personal", "leave", "study", "sleep", "other"},
    "commitment_status": {"active", "queued", "completed", "paused"},
    "user_tier": {"free", "pro", "admin"},
    "command_status": {"applied", "undone", "redone"},
    "message_role": {"user", "assistant", "system"},
}

SELECT_RE = re.compile(
    r"""\.table\(\s*['"]([a-z_]+)['"]\)(?:\s*\.[a-z_]+\([^;]{0,400}?\))*?\s*\.select\(\s*['"]([^'"]+)['"]""",
    re.DOTALL,
)


def _selected_columns():
    found = {}
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for table, raw in SELECT_RE.findall(text):
            columns = found.setdefault(table, set())
            for part in raw.split(","):
                name = part.strip().split(":")[-1].strip()
                if not name or name == "*" or "(" in name or ")" in name:
                    continue
                if re.fullmatch(r"[a-z_][a-z0-9_]*", name):
                    columns.add(name)
    return found


@pytest.mark.integration
def test_migrations_apply_on_an_empty_database(migrated_database):
    applied = migrated_database["applied"]
    assert applied[0].startswith("001_")
    assert any(name.startswith("012_") for name in applied)
    assert any(name.startswith("013_") for name in applied)
    assert applied == sorted(applied)


@pytest.mark.integration
def test_blank_work_type_migration_is_idempotent(migrated_database):
    import psycopg

    from tests.integration.sql_script import execute_sql_file

    path = ROOT / "migrations" / "013_work_type_blank.sql"
    with psycopg.connect(migrated_database["url"]) as connection:
        execute_sql_file(connection, path)
        execute_sql_file(connection, path)
        labels = connection.execute(
            """
            SELECT e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = 'public' AND t.typname = 'work_type'
            ORDER BY e.enumsortorder
            """
        ).fetchall()
    assert "blank" in {row[0] for row in labels}


@pytest.mark.integration
def test_code_columns_and_enum_labels_exist(db):
    rows = db.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
        """
    ).fetchall()
    present = {}
    for table, column in rows:
        present.setdefault(table, set()).add(column)

    required = {table: set(columns) for table, columns in CODE_COLUMNS.items()}
    for table, columns in _selected_columns().items():
        required.setdefault(table, set()).update(columns)

    missing = []
    for table, columns in sorted(required.items()):
        if table not in present:
            missing.append(f"table {table}")
            continue
        for column in sorted(columns - present[table]):
            missing.append(f"{table}.{column}")
    assert not missing, "schema is missing columns the code uses:\n" + "\n".join(missing)

    # The two production misses, called out on their own so a failure is obvious.
    assert "expires_at" in present["proposals"]
    enums = _enum_labels(db)
    assert "blank" in enums["work_type"]
    for name, labels in ENUMS.items():
        assert labels <= enums[name], f"{name} missing {labels - enums[name]}"


@pytest.mark.integration
def test_work_type_blank_can_be_stored(db):
    row = db.execute(
        """
        INSERT INTO users (auth_id, email, name)
        VALUES (gen_random_uuid(), 'blank@example.com', 'Blank')
        RETURNING id
        """
    ).fetchone()
    db.execute(
        """
        INSERT INTO calendar_days (user_id, date, work_type)
        VALUES (%s, '2026-01-01', 'blank')
        """,
        (row[0],),
    )
    stored = db.execute(
        "SELECT work_type::text FROM calendar_days WHERE user_id = %s",
        (row[0],),
    ).fetchone()
    assert stored[0] == "blank"


@pytest.mark.integration
def test_authenticated_user_cannot_raise_their_own_tier(db):
    """Migration 009 revokes direct profile writes.

    An own-row UPDATE policy would let the signed-in user set tier = admin.
    After 009, the authenticated role has SELECT only. service_role, which
    the API uses, can still change tier.
    """
    auth_id = db.execute("SELECT gen_random_uuid()").fetchone()[0]
    user_id = db.execute(
        """
        INSERT INTO users (auth_id, email, name, tier)
        VALUES (%s, 'tier@example.com', 'Tier', 'free')
        RETURNING id
        """,
        (auth_id,),
    ).fetchone()[0]
    db.commit()

    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.transaction():
            db.execute("SET LOCAL ROLE authenticated")
            db.execute(
                "SELECT set_config('request.jwt.claim.sub', %s, true)",
                (str(auth_id),),
            )
            db.execute(
                "UPDATE users SET tier = 'admin' WHERE id = %s",
                (user_id,),
            )

    with db.transaction():
        db.execute("SET LOCAL ROLE service_role")
        db.execute(
            "UPDATE users SET tier = 'pro' WHERE id = %s",
            (user_id,),
        )
        updated = db.execute(
            "SELECT tier::text FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
    assert updated[0] == "pro"


def _enum_labels(db):
    rows = db.execute(
        """
        SELECT t.typname, e.enumlabel
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public'
        """
    ).fetchall()
    labels = {}
    for name, label in rows:
        labels.setdefault(name, set()).add(label)
    return labels
