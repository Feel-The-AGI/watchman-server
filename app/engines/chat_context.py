"""Chat prompt context that is not the model call itself.

Onboarding sends a private parse prompt through the chat route. Those rows
must not appear in the panel or be fed back to the model as if the user had
said them. The prompt also has to see a rotation that was saved by the cycles
API, not only a master-settings document that is still empty.
"""

import json
from typing import Any, Optional

from app.engines.calendar_authority import cycle_is_present


_PROMPT_MARKERS = (
    "EXACT JSON format",
    "The user is describing their work shift rotation pattern",
)

INTERNAL_PARSE_SYSTEM = (
    "Extract the shift rotation described in the user message. "
    "Reply with one JSON object and no markdown. "
    "Keys: understood (boolean), pattern (list of {label, duration}), "
    "description (short string), message (short string for the user). "
    "label is work_day, work_night, or off."
)

# The parse is a short JSON extraction. Adaptive thinking and the full tool
# list are what make the onboarding call take 15-20s.
INTERNAL_PARSE_MAX_TOKENS = 2048


def is_internal_chat_content(content: Optional[str]) -> bool:
    """True for the onboarding parse prompt and the raw JSON it asks for."""
    text = (content or "").strip()
    if not text:
        return False
    if any(marker in text for marker in _PROMPT_MARKERS):
        return True
    return _looks_like_onboarding_json(text)


def is_hidden_chat_message(row: Optional[dict]) -> bool:
    """Rows that must not be shown in history or sent back to the model."""
    if not isinstance(row, dict):
        return False
    metadata = row.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("internal") is True:
        return True
    return is_internal_chat_content(row.get("content"))


def _looks_like_onboarding_json(text: str) -> bool:
    candidate = text.strip()
    if candidate.startswith("```"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            return False
        candidate = candidate[start:end + 1]
    if not candidate.startswith("{"):
        return False
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return (
            '"understood"' in candidate
            and '"pattern"' in candidate
            and '"description"' in candidate
        )
    if not isinstance(data, dict):
        return False
    return "understood" in data and "pattern" in data and "description" in data


def settings_for_prompt(document: Optional[dict], projection: Optional[dict]) -> dict:
    """Show the active cycle even when the stored document still has cycle null."""
    merged = dict(document or {})
    chosen = projection or {}
    if cycle_is_present(chosen.get("cycle")):
        merged["cycle"] = chosen["cycle"]
    if chosen.get("constraints") is not None:
        merged["constraints"] = chosen["constraints"]
    if chosen.get("leave_blocks") is not None:
        merged["leave_blocks"] = chosen["leave_blocks"]
    return merged


def describe_cycle(cycle: Optional[dict]) -> str:
    if not cycle_is_present(cycle):
        return ""
    parts = []
    total = 0
    for block in cycle.get("pattern") or []:
        if not isinstance(block, dict):
            continue
        label = str(block.get("label") or block.get("type") or "block").replace("_", " ")
        raw_duration = block.get("duration")
        if raw_duration is None:
            raw_duration = block.get("days") or 0
        try:
            duration = int(raw_duration)
        except (TypeError, ValueError):
            duration = 0
        total += duration
        parts.append(f"{duration} {label}")
    pattern_text = ", then ".join(parts) if parts else "a pattern with no blocks"
    anchor = cycle.get("anchor") if isinstance(cycle.get("anchor"), dict) else {}
    anchor_date = cycle.get("anchor_date") or anchor.get("date") or "unknown date"
    anchor_day = cycle.get("anchor_cycle_day") or anchor.get("cycle_day") or 1
    length = cycle.get("cycle_length") or cycle.get("total_days") or total or 0
    name = cycle.get("name") or "Rotation"
    return (
        f"{name}: {pattern_text} ({length}-day cycle). "
        f"Anchor {anchor_date} is cycle day {anchor_day}."
    )


def rotation_status(cycle: Optional[dict], calendar: Optional[dict]) -> str:
    """Tell the model the schedule it can already see is applied."""
    summary = calendar or {}
    count = int(summary.get("count") or 0)
    counts = summary.get("counts") or {}
    cycle_text = describe_cycle(cycle)
    if count:
        span = (
            f"Calendar days stored: {count} from {summary.get('start')} to {summary.get('end')} "
            f"(day {counts.get('work_day', 0)}, night {counts.get('work_night', 0)}, "
            f"off {counts.get('off', 0)}, blank {counts.get('blank', 0)})."
        )
    else:
        span = "No calendar days are stored in the recent window."

    if cycle_text and count:
        return (
            f"APPLIED. {cycle_text} {span} "
            "This rotation is already on the calendar. "
            "Do not tell the user it has not been applied. "
            "Do not call update_cycle unless they ask to change the pattern, the anchor, or to regenerate."
        )
    if cycle_text:
        return (
            f"STORED. {cycle_text} {span} "
            "The rotation is already saved. Do not say it has not been applied. "
            "Offer to generate calendar days only if the user wants dates filled in."
        )
    if count:
        return (
            f"APPLIED. {span} "
            "Those stored days are the user's schedule. "
            "Do not say a rotation discussed earlier has not been applied."
        )
    return (
        "No rotation is stored and no calendar days were found. "
        "The user still needs a cycle before dates can be generated."
    )


def empty_calendar() -> dict[str, Any]:
    return {
        "text": "No calendar days are stored yet.",
        "count": 0,
        "start": None,
        "end": None,
        "counts": {},
    }
