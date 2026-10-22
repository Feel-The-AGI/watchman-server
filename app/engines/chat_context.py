"""Chat prompt context that is not the model call itself.

Onboarding sends a private parse prompt through the chat route. Those rows
must not appear in the panel or be fed back to the model as if the user had
said them. The prompt also has to see a rotation that was saved by the cycles
API, not only a master-settings document that is still empty.
"""

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.engines.calendar_authority import cycle_is_present
from app.engines.proposal_effects import describe_command


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

# The parse is a short JSON extraction. The 15-20s came from sending it
# through the full tool-using agent with an 8192-token thinking budget.
# Keep adaptive thinking (this model is called that way) but cap the budget.
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


def rotation_status(cycle: Optional[dict], calendar: Optional[dict], pending: bool = False) -> str:
    """Tell the model the schedule it can already see is applied."""
    summary = calendar or {}
    count = int(summary.get("count") or 0)
    counts = summary.get("counts") or {}
    cycle_text = describe_cycle(cycle)
    if count:
        span = (
            f"Sample of stored days around today, not the end of the schedule: {count} "
            f"from {summary.get('start')} to {summary.get('end')} "
            f"(day {counts.get('work_day', 0)}, night {counts.get('work_night', 0)}, "
            f"off {counts.get('off', 0)}, blank {counts.get('blank', 0)})."
        )
    else:
        span = "No calendar days are stored in the recent window."

    if cycle_text and pending:
        return (
            f"CHOSEN. {cycle_text} {span} "
            "The user already picked this pattern during onboarding. It is not saved yet. "
            "Do not say there is no cycle. Do not ask for the pattern again. "
            "Do not narrate your steps."
        )
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


_CLOSED_OUTCOMES = {"declined", "approved", "expired"}


def proposal_outcome(proposal: Optional[dict], now: Optional[datetime] = None) -> Optional[str]:
    """Closed outcomes the model must not propose again. Pending stays unmarked."""
    if not isinstance(proposal, dict):
        return None
    status = str(proposal.get("status") or "").strip().lower()
    if status == "rejected":
        status = "declined"
    if status == "pending" and _is_past(proposal.get("expires_at"), now):
        return "expired"
    if status in _CLOSED_OUTCOMES:
        return status
    return None


def closed_proposal_note(proposal: Optional[dict], now: Optional[datetime] = None) -> Optional[str]:
    """One sentence telling the model a previous change is finished."""
    outcome = proposal_outcome(proposal, now)
    if outcome is None or not isinstance(proposal, dict):
        return None
    summary = _change_summary(proposal)
    detail = f" ({summary})" if summary else ""
    if outcome == "declined":
        return f"The user declined this change{detail}. Do not re-propose it."
    if outcome == "approved":
        return (
            f"The user approved this change{detail}. "
            "It is already applied. Do not propose it again."
        )
    return f"This proposal expired{detail}. Do not re-propose it."


def annotate_closed_proposals(
    history: Optional[list],
    proposals: Optional[list],
    now: Optional[datetime] = None,
) -> list:
    """Mark the user turn and the assistant card for a closed proposal.

    ``history`` is newest first, matching get_history. Pending proposals are
    left as written. The original rows are not mutated.
    """
    rows = list(history or [])
    notes = _notes_by_message(proposals, now)
    if not notes:
        return rows
    oldest_first = list(reversed(rows))
    annotated = []
    carry = None
    for row in oldest_first:
        if not isinstance(row, dict):
            annotated.append(row)
            continue
        updated = dict(row)
        content = updated.get("content") or ""
        note = notes.get(str(updated.get("id") or ""))
        if note and updated.get("role") == "user":
            updated["content"] = f"{content}\n\n[{note}]"
            carry = note
        elif carry and updated.get("role") == "assistant":
            updated["content"] = f"{content}\n\n[{carry}]"
            carry = None
        annotated.append(updated)
    return list(reversed(annotated))


def _notes_by_message(proposals: Optional[list], now: Optional[datetime]) -> dict:
    notes = {}
    ordered = sorted(
        [item for item in (proposals or []) if isinstance(item, dict)],
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    for proposal in ordered:
        message_id = str(proposal.get("message_id") or "")
        if not message_id or message_id in notes:
            continue
        note = closed_proposal_note(proposal, now)
        if note:
            notes[message_id] = note
    return notes


def _change_summary(proposal: dict) -> str:
    command = proposal.get("command") if isinstance(proposal.get("command"), dict) else {}
    action = str(command.get("action") or "")
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    return describe_command(action, payload) if action else ""


def _is_past(value: Any, now: Optional[datetime]) -> bool:
    expires = _as_datetime(value)
    if expires is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return expires <= current.astimezone(timezone.utc)


_ACTION_CUES = {
    "add_leave": ("leave", "leaves", "vacation", "sick", "day off", "days off", "time off"),
    "remove_leave": ("leave", "leaves", "vacation", "sick", "day off", "days off", "time off"),
    "update_leave": ("leave", "leaves", "vacation", "sick", "day off", "days off", "time off"),
    "add_commitment": ("commitment", "appointment", "event"),
    "remove_commitment": ("commitment", "appointment", "event"),
    "update_commitment": ("commitment", "appointment", "event"),
    "override_days": ("shift", "override", "work day", "night", "blank"),
    "update_cycle": ("rotation", "cycle", "pattern", "anchor"),
}
# A decline older than this is no longer blocked. The user can be offered it again.
DECLINE_GUARD = timedelta(hours=48)
_SHORT_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def repeats_declined_proposal(
    message: str,
    action: str,
    payload: Optional[dict],
    proposals: Optional[list],
    now: Optional[datetime] = None,
) -> bool:
    """True when this tool call repeats a decline from the last 48 hours.

    Same action and the same start/end dates. The tool stands when the latest
    message contains that change's cue ('leave', 'day off', and so on), including
    when the date is relative ('next Tuesday', 'the 27th', '27/10').
    """
    span = command_span(payload)
    if span is None or explicitly_requests(message, action, payload):
        return False
    for proposal in proposals or []:
        if not _declined_within_guard(proposal, now):
            continue
        command = _command(proposal)
        if str(command.get("action") or "") != action:
            continue
        body = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        if command_span(body) == span:
            return True
    return False


def explicitly_requests(message: str, action: str, payload: Optional[dict]) -> bool:
    """True when the latest message uses this change's cue word.

    The cue is enough on its own. 'mark next Tuesday as leave' and 'day off
    27/10' ask for leave again even though those dates are not absolute.
    'leave' is a whole word, so 'I leave work at 5' does not ask for leave.
    """
    lowered = (message or "").lower()
    return any(_cue_matches(lowered, cue) for cue in _cues(action, payload))


def declined_again_reply(action: str, payload: Optional[dict]) -> str:
    """A warm note that names the change and how to ask for it again."""
    when = _compact_span(payload)
    kind, phrase = _decline_kind_and_phrase(action, payload, when)
    held = "added" if action.startswith("add") or action == "update_leave" else "changed"
    return (
        f"You declined {kind} earlier, so I haven't {held} it. "
        f"If you'd like it after all, just say '{phrase}' and I'll set it up."
    )


def command_span(payload: Optional[dict]) -> Optional[tuple]:
    """Inclusive start and end for a tool payload. None when it has no date."""
    if not isinstance(payload, dict):
        return None
    start = _day(payload.get("start_date") or payload.get("date"))
    end = _day(payload.get("end_date")) or start
    if start is None or end is None:
        return None
    if end < start:
        end = start
    return (start.isoformat(), end.isoformat())


def _declined_within_guard(proposal: dict, now: Optional[datetime]) -> bool:
    """A decline counts only for about 48 hours after it was reviewed."""
    if proposal_outcome(proposal) != "declined":
        return False
    when = _as_datetime(proposal.get("reviewed_at") or proposal.get("created_at"))
    if when is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) - when <= DECLINE_GUARD


def _cues(action: str, payload: Optional[dict]) -> list:
    cues = list(_ACTION_CUES.get(action, ()))
    name = str((payload or {}).get("name") or "").strip().lower()
    if len(name) >= 3 and name not in {"leave", "other"}:
        cues.append(name)
    return cues


def _cue_matches(text: str, cue: str) -> bool:
    """Match a cue as a whole word or phrase. 'leave work' is not a leave request."""
    if cue == "leave":
        text = re.sub(r"\bleave\s+work\b", " ", text)
    return re.search(rf"\b{re.escape(cue)}\b", text) is not None


def _decline_kind_and_phrase(action: str, payload: Optional[dict], when: str) -> tuple:
    name = str((payload or {}).get("name") or "").strip()
    if action in {"add_leave", "update_leave"}:
        kind = f"the leave for {when}" if when else "that leave"
        phrase = f"add leave {when}" if when else "add leave"
        return kind, phrase
    if action == "remove_leave":
        kind = f"removing the leave for {when}" if when else "removing that leave"
        phrase = f"remove leave {when}" if when else "remove that leave"
        return kind, phrase
    if action in {"add_commitment", "update_commitment"}:
        label = name or "that event"
        kind = f"{label} for {when}" if when else label
        phrase = f"add {label} on {when}" if when else f"add {label}"
        return kind, phrase
    if action == "remove_commitment":
        label = name or "that event"
        kind = f"removing {label}" + (f" on {when}" if when else "")
        phrase = f"remove {label}" + (f" on {when}" if when else "")
        return kind, phrase
    if action == "override_days":
        shift = str((payload or {}).get("work_type") or "that shift").replace("_", " ")
        kind = f"the {shift} change for {when}" if when else f"the {shift} change"
        phrase = f"set {when} to {shift}" if when else f"set those days to {shift}"
        return kind, phrase
    if action == "update_cycle":
        return "the rotation change", "update the rotation"
    text = (action or "that change").replace("_", " ")
    kind = f"the {text} for {when}" if when else f"the {text}"
    phrase = f"{text} on {when}" if when else text
    return kind, phrase


def _compact_span(payload: Optional[dict]) -> str:
    """'27 Oct', or '24-26 Nov' when the range stays in one month."""
    span = command_span(payload)
    if span is None:
        return ""
    start = date.fromisoformat(span[0])
    end = date.fromisoformat(span[1])
    if start == end:
        return _spoken_day(start)
    if start.month == end.month and start.year == end.year:
        return f"{start.day}-{end.day} {_SHORT_MONTHS[start.month - 1]}"
    return f"{_spoken_day(start)}-{_spoken_day(end)}"


def _spoken_day(day: date) -> str:
    return f"{day.day} {_SHORT_MONTHS[day.month - 1]}"


def _command(proposal: dict) -> dict:
    command = proposal.get("command")
    if isinstance(command, str):
        try:
            command = json.loads(command)
        except json.JSONDecodeError:
            return {}
    return command if isinstance(command, dict) else {}


def _day(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_USER_VERBS = (
    r"says|said|is saying|is asking|wants|asked|mentioned|described|wrote|typed|"
    r"is describing|is telling"
)
# Only a reply that opens with this narration. A full stop is required so a
# sentence with no ending is left as the model wrote it.
_LEADING_USER = re.compile(
    rf"(?i)^the user (?:{_USER_VERBS})\b[^.!?\n]*[.!?]\s*"
)
_NO_CYCLE_LINE = re.compile(
    r"(?i)(?:^|(?<=[.!?]\s)|(?<=\n))"
    r"(?:"
    r"(?:but )?i need the rotation pattern\b[^.!?\n]*[.!?]"
    r"|the calendar has no cycle stored\b[^.!?\n]*[.!?]"
    r"|could you tell me your (?:full )?rotation pattern\??"
    r")\s*"
)

_ANCHOR_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH_NAME = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_MONTH_DAY = rf"({_MONTH_NAME})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?"
_ANCHOR_TAIL = r"(?:\s+of\s+(?:my|the|your)\s+cycle)?"
_ANCHOR_IS = re.compile(
    rf"^\s*(?:please\s+)?(?:today\s*,?\s+)?{_MONTH_DAY}\s*,?\s+is\s+day\s+(\d+){_ANCHOR_TAIL}\s*\.?\s*$",
    re.IGNORECASE,
)
_ANCHOR_WAS = re.compile(
    rf"^\s*(?:please\s+)?day\s+(\d+)\s+was\s+(?:on\s+)?{_MONTH_DAY}{_ANCHOR_TAIL}\s*\.?\s*$",
    re.IGNORECASE,
)
_INSTRUCTION = re.compile(
    r"\b(?:shift|log|leave|overtime|note|swap|add|remove|delete|change|move|"
    r"clear|blank|mark|set|update|record|report|regenerate)\b",
    re.IGNORECASE,
)
_BLOCK_WORDS = {
    "work_day": "day shifts",
    "work_night": "night shifts",
    "off": "days off",
}
_BLANK_REQUEST = re.compile(
    r"\b(?:blank|clear|untracked|unknown|wipe|erase)\b",
    re.IGNORECASE,
)
_PATTERN_LABELS = frozenset(_BLOCK_WORDS)
_MAX_BLOCKS = 10
_MAX_BLOCK_DAYS = 60
_MAX_CYCLE_DAYS = 120


def strip_model_reasoning(text: Optional[str], cycle_known: bool = False) -> str:
    """Drop a leading 'The user …' sentence, and a missing-cycle claim when one exists.

    A quote or a later sentence is left alone. A reply with no full stop is
    left alone. The rotation question is removed only when a cycle is known.
    """
    cleaned = text or ""
    cleaned = _LEADING_USER.sub("", cleaned, count=1)
    if cycle_known:
        previous = None
        while cleaned and previous != cleaned:
            previous = cleaned
            cleaned = _NO_CYCLE_LINE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip()


def local_today(tz_name: Optional[str], now: Optional[datetime] = None) -> date:
    """Today's date in the user's timezone. Unknown zones use UTC."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(tz_name or "UTC")
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    return moment.astimezone(zone).date()


def _nearest_day(month: int, day: int, today: date) -> Optional[date]:
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda item: (abs((item - today).days), item.toordinal()))


def _anchor_date(month_name: str, day: str, year: Optional[str], today: date) -> Optional[date]:
    month = _ANCHOR_MONTHS.get(month_name.lower())
    if month is None:
        return None
    day_number = int(day)
    if year:
        try:
            return date(int(year), month, day_number)
        except ValueError:
            return None
    return _nearest_day(month, day_number, today)


def sole_anchor_statement(text: str, today: date) -> Optional[dict]:
    """An onboarding message that is only an anchor, with no other instruction.

    A year is used as written. Without one, the date is the occurrence nearest today.
    """
    if not text or not text.strip() or _INSTRUCTION.search(text):
        return None
    was = _ANCHOR_WAS.match(text)
    if was:
        cycle_day = int(was.group(1))
        found = _anchor_date(was.group(2), was.group(3), was.group(4), today)
    else:
        is_anchor = _ANCHOR_IS.match(text)
        if is_anchor is None:
            return None
        found = _anchor_date(is_anchor.group(1), is_anchor.group(2), is_anchor.group(3), today)
        cycle_day = int(is_anchor.group(4))
    if found is None or cycle_day < 1:
        return None
    return {"date": found.isoformat(), "cycle_day": cycle_day}


def parse_typed_anchor(text: str, today: date) -> Optional[dict]:
    """Backward-compatible name for a message that is only an anchor statement."""
    return sole_anchor_statement(text, today)


def validate_onboarding_pattern(pattern: Optional[list]) -> list:
    """Normalize a client pattern or raise ValueError for a 400 response."""
    blocks = list(pattern or [])
    if len(blocks) > _MAX_BLOCKS:
        raise ValueError("A rotation can have at most 10 blocks.")
    normalized = []
    total = 0
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("Each block needs a label and a duration.")
        label = str(block.get("label") or block.get("type") or "").strip()
        raw = block.get("duration")
        if raw is None:
            raw = block.get("days")
        try:
            duration = int(raw)
        except (TypeError, ValueError):
            raise ValueError("Each block must be from 1 to 60 days.")
        if label not in _PATTERN_LABELS:
            raise ValueError("Each block must be work_day, work_night, or off.")
        if duration < 1 or duration > _MAX_BLOCK_DAYS:
            raise ValueError("Each block must be from 1 to 60 days.")
        total += duration
        normalized.append({"label": label, "duration": duration})
    if total > _MAX_CYCLE_DAYS:
        raise ValueError("A rotation can be at most 120 days.")
    return normalized


def chosen_cycle(pattern: Optional[list]) -> Optional[dict]:
    """The rotation the user picked in onboarding, before it is saved."""
    blocks = []
    for block in pattern or []:
        if not isinstance(block, dict):
            continue
        label = str(block.get("label") or block.get("type") or "").strip()
        raw = block.get("duration")
        if raw is None:
            raw = block.get("days")
        try:
            duration = int(raw)
        except (TypeError, ValueError):
            continue
        if label not in _BLOCK_WORDS or duration < 1:
            continue
        blocks.append({"label": label, "duration": duration})
    if not blocks:
        return None
    return {
        "name": "Rotation",
        "pattern": blocks,
        "cycle_length": sum(block["duration"] for block in blocks),
    }


def pattern_phrase(cycle: Optional[dict]) -> str:
    parts = []
    for block in (cycle or {}).get("pattern") or []:
        if not isinstance(block, dict):
            continue
        words = _BLOCK_WORDS.get(str(block.get("label") or ""), "days")
        parts.append(f"{int(block.get('duration') or 0)} {words}")
    return ", ".join(part for part in parts if not part.startswith("0 "))


def with_unsaved_pattern(settings: Optional[dict], pattern: Optional[list]) -> tuple:
    """Use a picked pattern when the account does not have a cycle yet."""
    base = dict(settings or {})
    picked = chosen_cycle(pattern)
    if not picked or cycle_is_present(base.get("cycle")):
        return base, False
    base["cycle"] = picked
    return base, True


def cycle_length(cycle: Optional[dict]) -> Optional[int]:
    if not isinstance(cycle, dict):
        return None
    raw = cycle.get("cycle_length")
    if raw is None:
        raw = cycle.get("total_days")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    total = 0
    counted = False
    for block in cycle.get("pattern") or []:
        if not isinstance(block, dict):
            continue
        try:
            total += int(block.get("duration") if block.get("duration") is not None else block.get("days") or 0)
            counted = True
        except (TypeError, ValueError):
            continue
    return total if counted and total > 0 else None


def anchor_day_correction(anchor: dict, length: int) -> str:
    return (
        f"Day {anchor['cycle_day']} is outside this {length}-day rotation. "
        f"Choose a day from 1 to {length}."
    )


def anchor_acknowledgement(anchor: dict, cycle: Optional[dict] = None) -> str:
    """A proposal the client applies from response.anchor. Nothing is saved yet."""
    day = date.fromisoformat(anchor["date"])
    label = f"{day.strftime('%b')} {day.day}, {day.year}"
    text = f"Use {label} as day {anchor['cycle_day']}"
    phrase = pattern_phrase(cycle)
    if phrase:
        text += f" ({phrase})"
    return f"{text}. Apply that anchor, then choose the rules to keep."


def unsolicited_blank_override(user_text: str, tool_name: str, payload: Optional[dict]) -> bool:
    """True when the model wants to blank days the user did not ask to clear."""
    if tool_name != "override_days":
        return False
    work_type = str((payload or {}).get("work_type") or "").strip().lower()
    if work_type != "blank":
        return False
    return _BLANK_REQUEST.search(user_text or "") is None
