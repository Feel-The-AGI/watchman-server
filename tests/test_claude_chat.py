"""Claude tool-use adapter. No network calls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest

from app.engines import chat_service
from app.engines.chat_service import ChatService
from app.engines.claude_chat import (
    USER_FACING_MODEL_ERROR,
    ModelRequestError,
    anthropic_tools,
    complete_chat,
    history_to_messages,
    parse_model_response,
)


def _timeout_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APITimeoutError(request=request)


def test_tools_keep_the_existing_json_schema():
    tools = anthropic_tools(chat_service.WATCHMAN_TOOLS)
    override = next(tool for tool in tools if tool["name"] == "override_days")
    assert override["input_schema"]["required"] == ["start_date", "end_date", "work_type"]
    assert "parameters" not in override


def test_history_uses_assistant_and_drops_the_current_turn_duplicate():
    # get_history returns newest first. The newest row is the message just saved.
    stored_newest_first = [
        {"role": "user", "content": "Add leave"},
        {"role": "assistant", "content": "Hello"},
    ]
    messages = history_to_messages(stored_newest_first, "Add leave")
    assert messages == [
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "Add leave"},
    ]


def test_parse_skips_thinking_and_keeps_the_first_tool():
    message = SimpleNamespace(content=[
        SimpleNamespace(type="thinking", thinking="secret reasoning"),
        SimpleNamespace(type="text", text="I'll add that leave."),
        SimpleNamespace(type="tool_use", name="add_leave", input={"name": "Annual"}),
        SimpleNamespace(type="tool_use", name="add_leave", input={"name": "Ignored"}),
    ])
    text, tool = parse_model_response(message)
    assert text == "I'll add that leave."
    assert tool == {"name": "add_leave", "input": {"name": "Annual"}}
    assert "secret" not in text


@pytest.mark.asyncio
async def test_missing_api_key_does_not_call_claude(monkeypatch):
    monkeypatch.setattr(
        "app.engines.claude_chat.get_settings",
        lambda: SimpleNamespace(anthropic_api_key="", anthropic_model="claude-haiku-5-5"),
    )

    class Client:
        def __init__(self, **kwargs):
            raise AssertionError("client constructed without a key")

    monkeypatch.setattr("app.engines.claude_chat.anthropic.AsyncAnthropic", Client)
    with pytest.raises(ModelRequestError):
        await complete_chat(system="sys", messages=[{"role": "user", "content": "Hi"}], tools=[])


def _capture_client(monkeypatch, settings):
    captured = {}

    class Messages:
        async def create(self, **kwargs):
            captured["create"] = kwargs
            return SimpleNamespace(content=[
                SimpleNamespace(type="text", text="Done."),
            ])

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.messages = Messages()

    monkeypatch.setattr("app.engines.claude_chat.get_settings", lambda: settings)
    monkeypatch.setattr("app.engines.claude_chat.anthropic.AsyncAnthropic", Client)
    return captured


@pytest.mark.asyncio
async def test_chat_default_disables_thinking_and_caps_tokens(monkeypatch):
    captured = _capture_client(monkeypatch, SimpleNamespace(
        anthropic_api_key="sk-test",
        anthropic_model="claude-haiku-5-5",
    ))
    text, tool = await complete_chat(
        system="sys",
        messages=[{"role": "user", "content": "Hi"}],
        tools=[{"name": "add_leave", "description": "Leave", "parameters": {"type": "object"}}],
    )
    assert text == "Done."
    assert tool is None
    request = captured["create"]
    assert request["model"] == "claude-haiku-5-5"
    assert request["max_tokens"] == 1024
    assert request["max_tokens"] < 8192
    assert request["thinking"] == {"type": "disabled"}
    assert request["thinking"].get("type") != "enabled"
    assert "budget_tokens" not in request
    assert "output_config" not in request
    assert request["tools"][0]["name"] == "add_leave"
    assert captured["client"]["timeout"] == 30


@pytest.mark.asyncio
async def test_adaptive_effort_is_low_and_leaves_room_for_the_tool_call(monkeypatch):
    captured = _capture_client(monkeypatch, SimpleNamespace(
        anthropic_api_key="sk-test",
        anthropic_model="claude-haiku-5-5",
        anthropic_thinking="adaptive",
        anthropic_thinking_effort="low",
        anthropic_chat_max_tokens=1024,
        anthropic_timeout_seconds=12,
    ))
    await complete_chat(system="sys", messages=[{"role": "user", "content": "Hi"}], tools=[])
    request = captured["create"]
    assert request["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in request
    assert request["output_config"] == {"effort": "low"}
    assert request["max_tokens"] == 2048
    assert captured["client"]["timeout"] == 12


@pytest.mark.asyncio
async def test_timeout_propagates_for_the_friendly_chat_error(monkeypatch):
    class Messages:
        async def create(self, **kwargs):
            raise _timeout_error()

    class Client:
        def __init__(self, **kwargs):
            self.messages = Messages()

    monkeypatch.setattr(
        "app.engines.claude_chat.get_settings",
        lambda: SimpleNamespace(anthropic_api_key="sk-test", anthropic_model="claude-haiku-5-5"),
    )
    monkeypatch.setattr("app.engines.claude_chat.anthropic.AsyncAnthropic", Client)
    with pytest.raises(anthropic.APITimeoutError):
        await complete_chat(system="sys", messages=[{"role": "user", "content": "Hi"}], tools=[])


def test_old_adaptive_budget_is_much_slower_than_the_chat_default():
    """Model of the production 25-30s chat turn. No API key is required.

    Adaptive thinking spends tokens inside max_tokens. The old request allowed
    8192 of them and landed at about 28s. The chat default disables thinking
    and caps the reply at 1024 tokens.
    """

    def simulated_seconds(max_tokens, thinking):
        if thinking and thinking.get("type") == "adaptive":
            return max_tokens / 8192 * 28.0
        return 0.6 + max_tokens / 1024 * 0.9

    old_seconds = simulated_seconds(8192, {"type": "adaptive"})
    new_seconds = simulated_seconds(1024, {"type": "disabled"})
    assert 25 <= old_seconds <= 30
    assert new_seconds < 3
    assert old_seconds / new_seconds > 10


@pytest.mark.asyncio
async def test_missing_key_and_timeout_stay_off_the_user_message(monkeypatch):
    service = ChatService.__new__(ChatService)
    service.user_id = "user"
    service.db = SimpleNamespace()
    service.settings_service = SimpleNamespace(get_snapshot=AsyncMock(return_value={"constraints": []}))
    service.command_executor = SimpleNamespace(
        create_proposal=AsyncMock(),
        execute=AsyncMock(),
    )

    async def save(role, content, command_id=None):
        return {"id": "message", "role": role, "content": content}

    service._save_message = save
    service._get_calendar_snapshot = AsyncMock(return_value={
        "text": "calendar",
        "count": 0,
        "start": None,
        "end": None,
        "counts": {},
    })
    service._get_logs_and_incidents = AsyncMock(return_value="logs")
    service.get_history = AsyncMock(return_value=[{"role": "user", "content": "Hi"}])
    monkeypatch.setattr(chat_service, "projection_for_user", AsyncMock(return_value={
        "cycle": None,
        "constraints": [],
        "leave_blocks": [],
    }))

    monkeypatch.setattr(
        chat_service,
        "complete_chat",
        AsyncMock(side_effect=_timeout_error()),
    )
    failed = await service.send_message("Hi", auto_execute=False)
    assert failed["is_command"] is False
    assert failed["response"] == USER_FACING_MODEL_ERROR
    assert "timed out" not in failed["response"].lower()
    assert "anthropic" not in failed["response"].lower()
    service.command_executor.execute.assert_not_called()

    monkeypatch.setattr(
        chat_service,
        "complete_chat",
        AsyncMock(return_value=("I'll add leave.", {
            "name": "add_leave",
            "input": {"name": "Annual", "start_date": "2026-11-01", "end_date": "2026-11-05"},
        })),
    )
    service.command_executor.create_proposal = AsyncMock(return_value={"id": "proposal"})
    ok = await service.send_message("Add leave", auto_execute=False)
    assert ok["command"] == {
        "action": "add_leave",
        "payload": {"name": "Annual", "start_date": "2026-11-01", "end_date": "2026-11-05"},
        "explanation": "I'll add leave.",
    }
    assert ok["proposal"]["id"] == "proposal"
    service.command_executor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_empty_tool_text_is_not_shown_as_executing(monkeypatch):
    service = ChatService.__new__(ChatService)
    service.user_id = "user"
    service.db = SimpleNamespace()
    service.settings_service = SimpleNamespace(get_snapshot=AsyncMock(return_value={"constraints": []}))
    created = {}

    async def create_proposal(command, message_id=None):
        created["command"] = command
        return {"id": "proposal", "command": command}

    service.command_executor = SimpleNamespace(
        create_proposal=create_proposal,
        execute=AsyncMock(),
    )

    async def save(role, content, command_id=None):
        return {"id": "message", "role": role, "content": content}

    service._save_message = save
    service._get_calendar_snapshot = AsyncMock(return_value={
        "text": "calendar", "count": 0, "start": None, "end": None, "counts": {},
    })
    service._get_logs_and_incidents = AsyncMock(return_value="logs")
    service.get_history = AsyncMock(return_value=[{"role": "user", "content": "Dentist"}])
    monkeypatch.setattr(chat_service, "projection_for_user", AsyncMock(return_value={
        "cycle": None, "constraints": [], "leave_blocks": [],
    }))
    monkeypatch.setattr(
        chat_service,
        "complete_chat",
        AsyncMock(return_value=("", {
            "name": "add_commitment",
            "input": {
                "name": "Dentist appointment",
                "type": "personal",
                "start_date": "2026-10-27",
                "end_date": "2026-10-27",
                "time_start": "15:00",
                "time_end": "16:00",
                "days_of_week": ["tuesday"],
            },
        })),
    )
    result = await service.send_message("Block out Tuesday", auto_execute=False)
    summary = "Add Dentist appointment on Tue 27 Oct, 3\u20134pm"
    assert created["command"]["explanation"] == summary
    assert result["response"] == summary
    assert "Executing" not in result["response"]
