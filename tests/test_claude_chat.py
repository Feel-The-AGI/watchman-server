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


@pytest.mark.asyncio
async def test_complete_chat_requests_adaptive_thinking(monkeypatch):
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

    monkeypatch.setattr(
        "app.engines.claude_chat.get_settings",
        lambda: SimpleNamespace(anthropic_api_key="sk-test", anthropic_model="claude-haiku-5-5"),
    )
    monkeypatch.setattr("app.engines.claude_chat.anthropic.AsyncAnthropic", Client)
    text, tool = await complete_chat(system="sys", messages=[{"role": "user", "content": "Hi"}], tools=[])
    assert text == "Done."
    assert tool is None
    assert captured["create"]["model"] == "claude-haiku-5-5"
    assert captured["create"]["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in captured["create"]
    assert captured["create"]["thinking"].get("type") != "enabled"
    assert captured["client"]["timeout"] == 30


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
    service._get_calendar_snapshot = AsyncMock(return_value="calendar")
    service._get_logs_and_incidents = AsyncMock(return_value="logs")
    service.get_history = AsyncMock(return_value=[{"role": "user", "content": "Hi"}])
    monkeypatch.setattr(chat_service, "effective_constraints", AsyncMock(return_value=[]))

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
