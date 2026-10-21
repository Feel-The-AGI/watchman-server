"""Claude Messages API adapter for the Watchman chat agent.

Callers still receive a command dict of {action, payload, explanation}.
Haiku 5.5 uses adaptive thinking. Do not send the deprecated budget_tokens mode.
"""

from typing import Any, Optional

import anthropic
from loguru import logger

from app.config import get_settings


MODEL_TIMEOUT_SECONDS = 30
# Adaptive thinking tokens count toward max_tokens. 8192 lets one reply think
# until it is close to the 30s client timeout, which is why chat turns land
# around 25-30s. The budget stays high so a tool call is not cut off.
CHAT_MAX_TOKENS = 8192
_ADAPTIVE_THINKING = {"type": "adaptive"}
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


async def complete_chat(
    *,
    system: str,
    messages: list,
    tools: Optional[list] = None,
    max_tokens: int = CHAT_MAX_TOKENS,
    thinking: Optional[dict] = _ADAPTIVE_THINKING,
) -> tuple[str, Optional[dict]]:
    """Call Claude. Raises ModelRequestError or anthropic.APIError on failure.

    ``thinking`` defaults to adaptive mode. Pass None for a short extraction
    that should not spend the token budget on hidden reasoning. Omit ``tools``
    when the caller wants JSON instead of a tool call.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise ModelRequestError("ANTHROPIC_API_KEY is not set")

    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=MODEL_TIMEOUT_SECONDS,
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
    response = await client.messages.create(**request)
    text, tool = parse_model_response(response)
    if tool:
        logger.info(f"[CLAUDE] Tool called: {tool['name']}")
    else:
        logger.info(f"[CLAUDE] No tool called: {text[:200]}")
    return text, tool
