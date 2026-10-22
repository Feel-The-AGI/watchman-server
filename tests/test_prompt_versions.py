"""Prompt versions: v1 is the shipped prompt, and the env can select another."""

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from postgrest.exceptions import APIError

from app.config import Settings
from app.engines.chat_service import SYSTEM_PROMPT, WATCHMAN_TOOLS, ChatService, _tools_for
from app.engines.command_executor import CommandExecutor
from app.prompts.registry import VERSIONS, latest_stable, load_version, version_for
from app.engines import chat_service


CHAT_V1_SHA256 = "c597b54f88776fb4b1bd9ecf5ece1ae6c7ed0e17b2f09f3ea89b875145b82510"
ONBOARDING_V1_SHA256 = "c2d2bb3d2e72ee0469c24b3ef334d010cf18b7d28bc487e9ed7a3350ce831dcc"
ROOT = Path(__file__).resolve().parents[1]


def _settings(**overrides):
    values = {
        "chat_prompt_version": "",
        "onboarding_prompt_version": "",
        "chat_prompt_allowlist": "",
        "onboarding_prompt_allowlist": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _placeholders(text: str) -> set:
    return set(re.findall(r"(?<!\{)\{([a-z_]+)\}", text))


def test_v1_matches_the_shipped_prompt_byte_for_byte():
    chat = (ROOT / "app/prompts/chat/v1.md").read_bytes()
    onboarding = (ROOT / "app/prompts/onboarding/v1.md").read_bytes()
    assert hashlib.sha256(chat).hexdigest() == CHAT_V1_SHA256
    assert hashlib.sha256(onboarding).hexdigest() == ONBOARDING_V1_SHA256
    assert load_version("chat-v1").text.encode() == chat
    assert SYSTEM_PROMPT.encode() == chat
    assert load_version("onboarding-v1").text.encode() == onboarding
    assert "Do not narrate your steps." in SYSTEM_PROMPT
    assert "stay filled from the pattern" in SYSTEM_PROMPT


def test_config_defaults_to_the_latest_stable_version(monkeypatch):
    assert Settings.model_fields["chat_prompt_version"].default == ""
    assert Settings.model_fields["onboarding_prompt_version"].default == ""
    assert latest_stable("chat").id == "chat-v1"
    assert load_version("chat-v2").stable is False
    monkeypatch.setattr("app.prompts.registry.get_settings", lambda: _settings())
    assert version_for("chat").id == "chat-v1"
    assert version_for("onboarding").id == "onboarding-v1"


def test_env_switch_selects_the_requested_version(monkeypatch):
    monkeypatch.setattr(
        "app.prompts.registry.get_settings",
        lambda: _settings(chat_prompt_version="chat-v1"),
    )
    selected = version_for("chat")
    assert selected.id == "chat-v1"
    assert selected.text == load_version("chat-v1").text
    assert "stay filled from the pattern" in selected.text
    assert _tools_for(selected) is WATCHMAN_TOOLS


def test_chat_v1_tools_match_main_and_chat_v2_carries_the_shift_wording():
    v1_tools = _tools_for(load_version("chat-v1"))
    assert v1_tools is WATCHMAN_TOOLS
    v1_update = next(item for item in v1_tools if item["name"] == "update_cycle")
    assert "by adjusting anchor_cycle_day" in v1_update["description"]
    assert v1_update["parameters"]["properties"]["shift_by_days"]["description"] == (
        "Alternative way to shift the rotation: positive = forward, negative = backward. "
        "Use this for 'off by one day' fixes."
    )
    assert "each shift happens that many days later" not in v1_update["description"]
    assert "each shift happens that many days later" not in (
        v1_update["parameters"]["properties"]["shift_by_days"]["description"]
    )

    v2_tools = _tools_for(load_version("chat-v2"))
    assert v2_tools is not WATCHMAN_TOOLS
    v2_update = next(item for item in v2_tools if item["name"] == "update_cycle")
    assert "each shift happens that many days later" in v2_update["description"]
    assert v2_update["parameters"]["properties"]["shift_by_days"]["description"] == (
        "Move the rotation without editing history. A positive number means each shift "
        "happens that many days later. A negative number means each shift happens that "
        "many days earlier."
    )


def test_chat_v2_states_that_a_positive_shift_moves_each_shift_later():
    v1 = load_version("chat-v1").text
    v2 = load_version("chat-v2").text
    assert v2 != v1
    assert "each shift happens that many days later" in v2
    assert "do not propose a shift yet" in v2
    assert "what shift it is" in v2
    assert 'User: "I\'m a day behind."' in v2
    assert "No concrete date" in v2
    assert "They confirmed the date" in v2
    assert "shift_by_days=1" in v2
    assert 'User: "I\'m a day ahead."' in v2
    assert "shift_by_days=-1" in v2
    assert "I'm a day behind" not in v1


def test_every_later_version_differs_from_v1_and_has_a_changelog():
    by_family = {}
    for item in VERSIONS.values():
        by_family.setdefault(item.family, []).append(item)
    for versions in by_family.values():
        base = next(item for item in versions if item.number == 1)
        for item in versions:
            if item.number == 1:
                continue
            assert item.text != base.text
            assert item.changelog.strip()


def test_unknown_version_falls_back_and_warns(monkeypatch):
    monkeypatch.setattr(
        "app.prompts.registry.get_settings",
        lambda: _settings(chat_prompt_version="chat-v9"),
    )
    records = []

    def sink(message):
        records.append(message.record)

    handle = logger.add(sink, level="WARNING")
    try:
        selected = version_for("chat")
    finally:
        logger.remove(handle)
    assert selected.id == "chat-v1"
    assert any("chat-v9" in record["message"] for record in records)


def test_allowlist_selects_a_version_for_one_account(monkeypatch):
    monkeypatch.setattr(
        "app.prompts.registry.get_settings",
        lambda: _settings(chat_prompt_allowlist="user-test:chat-v1, user-other:chat-v9"),
    )
    assert version_for("chat", "user-test").id == "chat-v1"
    assert version_for("chat", "user-live").id == "chat-v1"
    assert version_for("chat", "user-other").id == "chat-v1"


class _Table:
    def __init__(self, client, name):
        self.client = client
        self.name = name
        self.payload = None

    def insert(self, data):
        self.payload = data
        return self

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        if self.payload is None:
            return SimpleNamespace(data=[])
        if self.client.missing_column and "prompt_version" in self.payload:
            self.client.missing_column = False
            raise APIError({
                "message": "Could not find the 'prompt_version' column of 'proposals' in the schema cache",
                "code": "PGRST204",
                "details": None,
                "hint": None,
            })
        saved = dict(self.payload)
        self.client.saved.append((self.name, saved))
        return SimpleNamespace(data=[saved])


class _Client:
    def __init__(self, missing_column=False):
        self.saved = []
        self.missing_column = missing_column

    def table(self, name):
        return _Table(self, name)


def _executor(client):
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.user_id = "user-1"
    executor.db = SimpleNamespace(client=client)

    async def validate(command):
        return {"valid": True, "violations": [], "warnings": [], "alternatives": []}

    executor.validate_command = validate
    return executor


@pytest.mark.asyncio
async def test_prompt_version_is_stored_on_a_proposal():
    client = _Client()
    proposal = await _executor(client).create_proposal(
        {"action": "add_leave", "payload": {}},
        prompt_version="chat-v1",
    )
    saved = client.saved[0][1]
    assert saved["prompt_version"] == "chat-v1"
    assert saved["validation"]["prompt_version"] == "chat-v1"
    assert proposal["prompt_version"] == "chat-v1"


@pytest.mark.asyncio
async def test_a_missing_prompt_column_still_stores_the_version_in_json():
    client = _Client(missing_column=True)
    records = []
    handle = logger.add(lambda message: records.append(message.record), level="WARNING")
    try:
        proposal = await _executor(client).create_proposal(
            {"action": "add_leave", "payload": {}},
            prompt_version="chat-v1",
        )
    finally:
        logger.remove(handle)
    saved = client.saved[0][1]
    assert "prompt_version" not in saved
    assert saved["validation"]["prompt_version"] == "chat-v1"
    assert proposal["prompt_version"] == "chat-v1"
    assert any("not in the database yet" in record["message"] for record in records)


@pytest.mark.asyncio
async def test_chat_response_names_the_prompt_version(monkeypatch):
    monkeypatch.setattr("app.prompts.registry.get_settings", lambda: _settings())
    client = _Client()
    service = ChatService.__new__(ChatService)
    service.user_id = "user-1"
    service.db = SimpleNamespace(client=client)
    service.settings_service = SimpleNamespace(get_snapshot=AsyncMock(return_value={}))
    service.command_executor = _executor(client)
    service._get_calendar_snapshot = AsyncMock(return_value={
        "text": "none", "count": 0, "start": None, "end": None, "counts": {},
    })
    service._get_logs_and_incidents = AsyncMock(return_value="none")
    service.get_history = AsyncMock(return_value=[])
    monkeypatch.setattr(chat_service, "projection_for_user", AsyncMock(return_value={}))
    monkeypatch.setattr(
        chat_service,
        "complete_chat",
        AsyncMock(return_value=("I'll add leave.", {
            "name": "add_leave",
            "input": {"name": "Annual", "start_date": "2026-11-01", "end_date": "2026-11-05"},
        })),
    )

    result = await service.send_message("Add leave from 1 Nov", auto_execute=False)

    assert result["prompt_version"] == "chat-v1"
    assert result["proposal"]["prompt_version"] == "chat-v1"
    assert result["proposal"]["validation"]["prompt_version"] == "chat-v1"
    assistant = next(row for name, row in client.saved if name == "chat_messages" and row["role"] == "assistant")
    assert assistant["metadata"]["prompt_version"] == "chat-v1"
    assert assistant["prompt_version"] == "chat-v1"
    sent = chat_service.complete_chat.await_args.kwargs
    assert sent["system"].startswith("You are Watchman")
    assert sent["tools"] is WATCHMAN_TOOLS
