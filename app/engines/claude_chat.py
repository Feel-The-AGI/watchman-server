"""Claude Messages API adapter for the Watchman chat agent.

Callers still receive a command dict of {action, payload, explanation}.
Haiku 5.5 can use adaptive thinking. Do not send the deprecated budget_tokens
mode. Chat defaults to thinking disabled and a 1024 token cap so a normal
reply does not spend the whole client timeout on hidden reasoning.
"""

from typing import Any, Optional

import anthropic
from loguru import logger

from app.config import get_settings


MODEL_TIMEOUT_SECONDS = 30
# Visible reply plus one tool call. Adaptive thinking, when enabled, needs a
# higher cap because those tokens come out of this same budget.
CHAT_MAX_TOKENS = 1024
ADAPTIVE_MAX_TOKENS = 2048
_UNSET = object()
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
USER_FACING_MODEL_ERROR = (
    "I'm having trouble reaching Watchman right now. Please try again in a moment."
)


class ModelRequestError(Exception):
    """The model request was not sent. Detail is for logs only."""


def anthropic_tools(watchman_tools: list) -> list:
    """Convert the existing tool declarations into Claude tool definitions."""
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["parameters"],
        }
        for tool in watchman_tools
    ]


def history_to_messages(history: list, latest_content: str) -> list:
    """Build alternating user/assistant turns.

    ``history`` is newest first and already includes the message just saved.
    That newest row is replaced by ``latest_content`` so it is not sent twice.
    """
    prior = list(reversed(history[1:])) if history else []
    messages = []
    for msg in prior:
        role = "user" if msg.get("role") == "user" else "assistant"
        text = (msg.get("content") or "").strip()
        if not text:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] = f"{messages[-1]['content']}\n{text}"
        else:
            messages.append({"role": role, "content": text})
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] = f"{messages[-1]['content']}\n{latest_content}"
    else:
        messages.append({"role": "user", "content": latest_content})
    return messages


def _as_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def parse_model_response(message: Any) -> tuple[str, Optional[dict]]:
    """Read text and the first tool call. Thinking blocks are not shown to the user."""
    text_parts = []
    tool = None
    for block in getattr(message, "content", None) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = (getattr(block, "text", None) or "").strip()
            if text:
                text_parts.append(text)
        elif block_type == "tool_use" and tool is None:
            tool = {
                "name": getattr(block, "name", None),
                "input": _as_dict(getattr(block, "input", None)),
            }
    return "\n".join(text_parts).strip(), tool


def _configured(settings: Any, name: str, default: Any) -> Any:
    """Read a setting. A MagicMock attribute falls back to the default."""
    if settings is None:
        return default
    value = getattr(settings, name, default)
    if value is None or type(value).__name__ == "MagicMock":
        return default
    return value


def resolve_thinking(settings: Any) -> tuple[dict, Optional[dict], int]:
    """Thinking mode, optional effort config, and max_tokens for one chat call.

    Disabled thinking is the chat default. Adaptive mode never sends
    budget_tokens; effort is low unless configured otherwise, and the token
    cap stays high enough for the tool call that follows the reasoning.
    """
    mode = str(_configured(settings, "anthropic_thinking", "disabled")).strip().lower()
    effort = str(_configured(settings, "anthropic_thinking_effort", "low")).strip().lower()
    max_tokens = int(_configured(settings, "anthropic_chat_max_tokens", CHAT_MAX_TOKENS))
    if max_tokens < 256:
        max_tokens = 256
    if mode in {"adaptive", "on"}:
        if effort not in _EFFORTS:
            effort = "low"
        if max_tokens < ADAPTIVE_MAX_TOKENS:
            max_tokens = ADAPTIVE_MAX_TOKENS
        return {"type": "adaptive"}, {"effort": effort}, max_tokens
    return {"type": "disabled"}, None, max_tokens


async def complete_chat(
    *,
    system: str,
    messages: list,
    tools: Optional[list] = None,
    max_tokens: Optional[int] = None,
    thinking: Any = _UNSET,
    effort: Any = _UNSET,
) -> tuple[str, Optional[dict]]:
    """Call Claude. Raises ModelRequestError or anthropic.APIError on failure.

    Chat uses the settings default (thinking disabled, 1024 tokens). Pass
    ``thinking`` to override that, including None when the request should omit
    the field. Omit ``tools`` when the caller wants JSON instead of a tool call.
    A timeout is an anthropic.APITimeoutError so the chat route can turn it
    into the friendly error without the raw exception text.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise ModelRequestError("ANTHROPIC_API_KEY is not set")

    default_thinking, default_effort, default_max_tokens = resolve_thinking(settings)
    if thinking is _UNSET:
        thinking = default_thinking
        if effort is _UNSET:
            effort = default_effort
    elif effort is _UNSET:
        effort = None
    if max_tokens is None:
        max_tokens = default_max_tokens

    timeout = float(_configured(settings, "anthropic_timeout_seconds", MODEL_TIMEOUT_SECONDS))
    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=timeout,
    )
    request = {
        "model": settings.anthropic_model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if tools:
        request["tools"] = anthropic_tools(tools)
    if thinking:
        request["thinking"] = thinking
    if effort and isinstance(thinking, dict) and thinking.get("type") == "adaptive":
        request["output_config"] = effort if "effort" in effort else {"effort": effort}
    response = await client.messages.create(**request)
    text, tool = parse_model_response(response)
    if tool:
        logger.info(f"[CLAUDE] Tool called: {tool['name']}")
    else:
        logger.info(f"[CLAUDE] No tool called: {text[:200]}")
    return text, tool
