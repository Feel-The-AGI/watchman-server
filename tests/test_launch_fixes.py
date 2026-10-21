"""Launch fixes: Paystack channels, chat leaks, onboarding history, applied cycle."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.engines import chat_service
from app.engines.chat_context import (
    describe_cycle,
    is_hidden_chat_message,
    is_internal_chat_content,
    rotation_status,
    settings_for_prompt,
)
from app.engines.chat_service import ChatService
from app.engines.claude_chat import USER_FACING_MODEL_ERROR, complete_chat
from app.middleware.auth import get_current_user
from app.routes.cron import completed_week_range


ONBOARDING_PROMPT = (
    'The user is describing their work shift rotation pattern: "14 on, 14 off"\n'
    "Please understand this and respond in this EXACT JSON format:\n"
    '{"understood": true, "pattern": [], "description": "summary", "message": "reply"}'
)

ONBOARDING_JSON = (
    '{"understood": true, "pattern": [{"label": "work_day", "duration": 14}, '
    '{"label": "off", "duration": 14}], "description": "14 on, 14 off", "message": "Got it"}'
)

FOURTEEN = {
    "name": "My Rotation",
    "pattern": [
        {"label": "work_day", "duration": 14},
        {"label": "off", "duration": 14},
    ],
    "anchor_date": "2026-10-01",
    "anchor_cycle_day": 1,
    "cycle_length": 28,
}


def test_checkout_source_does_not_send_a_channels_list():
    source = open("app/routes/payments.py", encoding="utf-8").read()
    assert '"channels"' not in source


def test_proposal_expiry_migration_is_idempotent():
    sql = open("migrations/012_proposal_expiry.sql", encoding="utf-8").read().lower()
    assert "add column if not exists expires_at timestamptz" in sql
    assert "idx_proposals_user_status_expires" in sql
    assert "on public.proposals (user_id, status, expires_at)" in sql


def test_onboarding_prompt_and_raw_json_are_hidden():
    assert is_internal_chat_content(ONBOARDING_PROMPT)
    assert is_internal_chat_content(ONBOARDING_JSON)
    assert is_internal_chat_content("```json\n" + ONBOARDING_JSON + "\n```")
    assert not is_internal_chat_content("I work 14 on and 14 off")
    assert is_hidden_chat_message({"content": "hello", "metadata": {"internal": True}})
    assert not is_hidden_chat_message({"content": "Add leave next week", "metadata": {}})


def test_sunday_summary_includes_the_week_that_ends_today():
    # 2026-10-25 is a Sunday. The old weekday()+7 formula reported Oct 12–18.
    assert completed_week_range(date(2026, 10, 25)) == (date(2026, 10, 19), date(2026, 10, 25))
    assert completed_week_range(date(2026, 10, 26)) == (date(2026, 10, 19), date(2026, 10, 25))
    assert completed_week_range(date(2026, 10, 21)) == (date(2026, 10, 12), date(2026, 10, 18))


def test_applied_cycle_is_described_even_when_the_document_is_empty():
    merged = settings_for_prompt({"cycle": None, "constraints": []}, {
        "cycle": FOURTEEN,
        "constraints": [{"id": "night"}],
        "leave_blocks": [],
    })
    assert merged["cycle"]["pattern"][0]["duration"] == 14
    text = describe_cycle(merged["cycle"])
    assert "14 work day" in text
    assert "14 off" in text
    status = rotation_status(merged["cycle"], {
        "count": 28,
        "start": "2026-10-01",
        "end": "2026-10-28",
        "counts": {"work_day": 14, "off": 14},
    })
    assert status.startswith("APPLIED.")
    assert "has not been applied" in status
    assert "14 work day" in status


def test_authenticated_profile_update_cannot_set_tier_or_role(app, client, monkeypatch):
    saved = {}

    class DB:
        def __init__(self, **kwargs):
            pass

        async def update_user(self, user_id, data):
            saved["data"] = data
            return {"id": user_id, "tier": "free", "role": "user", **data}

    monkeypatch.setattr("app.routes.auth.Database", DB)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "user-1",
        "email": "a@example.com",
        "name": "A",
        "timezone": "UTC",
        "tier": "free",
        "role": "user",
        "onboarding_completed": True,
        "settings": {},
    }
    response = client.patch(
        "/api/auth/me",
        json={"name": "A", "tier": "admin", "role": "admin", "email": "evil@example.com"},
    )
    assert response.status_code == 200
    assert saved["data"] == {"name": "A"}
    assert "tier" not in saved["data"]
    assert "role" not in saved["data"]


def test_profile_with_null_fields_returns_200(app, client):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "user-1",
        "email": None,
        "name": None,
        "timezone": None,
        "tier": None,
        "role": None,
        "onboarding_completed": None,
        "settings": None,
    }
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "UTC"
    assert body["email"] == ""
    assert body["name"] == ""
    assert body["settings"] == {}
    assert body["tier"] == "free"
    assert "Traceback" not in response.text


def test_settings_update_survives_a_null_settings_blob(app, client, monkeypatch):
    saved = {}

    class DB:
        def __init__(self, **kwargs):
            pass

        async def update_user(self, user_id, data):
            saved.update(data)
            return {"id": user_id, **data}

    monkeypatch.setattr("app.routes.settings.Database", DB)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "user-1",
        "tier": "pro",
        "role": "user",
        "settings": None,
        "created_at": "2020-01-01T00:00:00+00:00",
    }
    response = client.patch("/api/settings", json={"theme": "dark"})
    assert response.status_code == 200
    assert saved["settings"]["theme"] == "dark"


def test_chat_database_error_is_not_returned_to_the_user(app, monkeypatch):
    monkeypatch.setattr("app.routes.chat.Database", lambda **kwargs: MagicMock())

    class Boom:
        async def send_message(self, content, auto_execute=False):
            raise RuntimeError(
                "{'code': 'PGRST204', 'message': \"Could not find the 'expires_at' column of 'proposals'\"}"
            )

    monkeypatch.setattr("app.routes.chat.create_chat_service", lambda db, user_id: Boom())
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "user-1",
        "tier": "pro",
        "role": "user",
        "created_at": "2020-01-01T00:00:00+00:00",
    }
    response = TestClient(app).post("/api/chat/message", json={"content": "hello"})
    assert response.status_code == 500
    assert response.json()["detail"] == USER_FACING_MODEL_ERROR
    body = response.text.lower()
    assert "pgrst204" not in body
    assert "expires_at" not in body
    assert "traceback" not in body


@pytest.mark.asyncio
async def test_onboarding_parse_is_not_saved_and_skips_tools(monkeypatch):
    service = ChatService.__new__(ChatService)
    service.user_id = "user"
    service.db = SimpleNamespace()
    saved = []

    async def save(role, content, command_id=None):
        saved.append((role, content))
        return {"id": "m", "role": role, "content": content}

    service._save_message = save
    captured = {}

    async def fake_complete(**kwargs):
        captured.update(kwargs)
        return (ONBOARDING_JSON, None)

    monkeypatch.setattr(chat_service, "complete_chat", fake_complete)
    result = await service.send_message(ONBOARDING_PROMPT, auto_execute=False)
    assert saved == []
    assert result["internal"] is True
    assert result["user_message"] is None
    assert result["assistant_message"] is None
    assert result["response"] == ONBOARDING_JSON
    assert captured["tools"] is None
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["max_tokens"] == 2048
    assert "WATCHMAN_TOOLS" not in str(captured.get("tools"))


@pytest.mark.asyncio
async def test_chat_prompt_says_a_generated_rotation_is_applied(monkeypatch):
    service = ChatService.__new__(ChatService)
    service.user_id = "user"
    service.db = SimpleNamespace()
    service.settings_service = SimpleNamespace(get_snapshot=AsyncMock(return_value={"cycle": None}))
    service.command_executor = SimpleNamespace(create_proposal=AsyncMock(), execute=AsyncMock())

    async def save(role, content, command_id=None):
        return {"id": "m", "role": role, "content": content}

    service._save_message = save
    service._get_calendar_snapshot = AsyncMock(return_value={
        "text": "Stored calendar: 28 days",
        "count": 28,
        "start": "2026-10-01",
        "end": "2026-10-28",
        "counts": {"work_day": 14, "off": 14},
    })
    service._get_logs_and_incidents = AsyncMock(return_value="none")
    service.get_history = AsyncMock(return_value=[{"role": "user", "content": "What is my rotation?"}])
    monkeypatch.setattr(chat_service, "projection_for_user", AsyncMock(return_value={
        "cycle": FOURTEEN,
        "constraints": [],
        "leave_blocks": [],
    }))
    captured = {}

    async def fake_complete(**kwargs):
        captured.update(kwargs)
        return ("Your 14 on / 14 off rotation is on the calendar.", None)

    monkeypatch.setattr(chat_service, "complete_chat", fake_complete)
    await service.send_message("What is my rotation?", auto_execute=False)
    system = captured["system"]
    assert "APPLIED." in system
    assert "14 work day" in system
    assert "14 off" in system
    assert '"duration": 14' in system
    assert captured["tools"]
    assert captured["thinking"] == {"type": "adaptive"}


@pytest.mark.asyncio
async def test_visible_history_drops_onboarding_rows():
    rows = [
        {"role": "assistant", "content": ONBOARDING_JSON, "metadata": {}},
        {"role": "user", "content": ONBOARDING_PROMPT, "metadata": {}},
        {"role": "assistant", "content": "Your calendar is ready.", "metadata": {}},
        {"role": "user", "content": "Thanks", "metadata": {}},
    ]

    class Query:
        def select(self, *args, **kwargs):
            return self

        def eq(self, *args, **kwargs):
            return self

        def order(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def execute(self):
            return SimpleNamespace(data=rows)

    service = ChatService.__new__(ChatService)
    service.user_id = "user"
    service.db = SimpleNamespace(client=SimpleNamespace(table=lambda name: Query()))
    history = await service.get_history(limit=10)
    assert [row["content"] for row in history] == ["Your calendar is ready.", "Thanks"]


@pytest.mark.asyncio
async def test_complete_chat_can_omit_tools_and_thinking(monkeypatch):
    captured = {}

    class Messages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")])

    class Client:
        def __init__(self, **kwargs):
            self.messages = Messages()

    monkeypatch.setattr(
        "app.engines.claude_chat.get_settings",
        lambda: SimpleNamespace(anthropic_api_key="sk-test", anthropic_model="claude-haiku-5-5"),
    )
    monkeypatch.setattr("app.engines.claude_chat.anthropic.AsyncAnthropic", Client)
    text, tool = await complete_chat(
        system="extract",
        messages=[{"role": "user", "content": "14 on 14 off"}],
        tools=None,
        max_tokens=2048,
        thinking=None,
    )
    assert text == "{}"
    assert tool is None
    assert captured["max_tokens"] == 2048
    assert "tools" not in captured
    assert "thinking" not in captured
    assert "budget_tokens" not in captured
