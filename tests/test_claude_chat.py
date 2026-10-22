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
    assert request["max_tokens"] == 2048
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


def _message(stop_reason, tool_name=None):
    content = []
    if tool_name:
        content.append(SimpleNamespace(type="tool_use", name=tool_name, input={"name": "Partial"}))
    else:
        content.append(SimpleNamespace(type="text", text="cut off"))
    return SimpleNamespace(stop_reason=stop_reason, content=content)


@pytest.mark.asyncio
async def test_max_tokens_retries_once_and_does_not_return_a_cutoff_tool(monkeypatch):
    captured = []

    class Messages:
        async def create(self, **kwargs):
            captured.append(kwargs)
            if len(captured) == 1:
                return _message("max_tokens", "add_leave")
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[SimpleNamespace(type="tool_use", name="add_leave", input={"name": "Annual"})],
            )

    class Client:
        def __init__(self, **kwargs):
            self.messages = Messages()

    monkeypatch.setattr(
        "app.engines.claude_chat.get_settings",
        lambda: SimpleNamespace(anthropic_api_key="sk-test", anthropic_model="claude-haiku-5-5"),
    )
    monkeypatch.setattr("app.engines.claude_chat.anthropic.AsyncAnthropic", Client)
    text, tool = await complete_chat(system="sys", messages=[{"role": "user", "content": "Hi"}], tools=[])
    assert text == ""
    assert tool == {"name": "add_leave", "input": {"name": "Annual"}}
    assert captured[0]["max_tokens"] == 2048
    assert captured[1]["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_max_tokens_after_retry_is_a_model_error(monkeypatch):
    class Messages:
        async def create(self, **kwargs):
            return _message("max_tokens", "add_commitment")

    class Client:
        def __init__(self, **kwargs):
            self.messages = Messages()

    monkeypatch.setattr(
        "app.engines.claude_chat.get_settings",
        lambda: SimpleNamespace(anthropic_api_key="sk-test", anthropic_model="claude-haiku-5-5"),
    )
    monkeypatch.setattr("app.engines.claude_chat.anthropic.AsyncAnthropic", Client)
    with pytest.raises(ModelRequestError):
        await complete_chat(system="sys", messages=[{"role": "user", "content": "Hi"}], tools=[])


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
    service.command_executor.create_proposal.assert_not_awaited()

    monkeypatch.setattr(
        chat_service,
        "complete_chat",
        AsyncMock(side_effect=ModelRequestError("cut off")),
    )
    cutoff = await service.send_message("Add leave", auto_execute=False)
    assert cutoff["is_command"] is False
    assert cutoff["proposal"] is None
    assert cutoff["response"] == USER_FACING_MODEL_ERROR
    service.command_executor.create_proposal.assert_not_awaited()

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
    assert ok["command"]["action"] == "add_leave"
    assert ok["command"]["payload"] == {
        "name": "Annual", "start_date": "2026-11-01", "end_date": "2026-11-05",
    }
    assert ok["command"]["explanation"] == "Add Annual from Sun 1 Nov to Thu 5 Nov"
    assert ok["command"]["model_text"] == "I'll add leave."
    assert ok["response"] == ok["command"]["explanation"]
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
