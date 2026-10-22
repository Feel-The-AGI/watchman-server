"""Turn "I didn't work that day" into an Off override for those dates only.

The rotation stays as it is. A date with no year is resolved from the user's
local today: past tense uses the most recent past date, and cancelling a
shift uses the next one.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.engines.spoken_dates import parse_spoken_span, speak

_QUESTION = re.compile(
    r"(?:\?\s*$)|^\s*(?:what|when|which|who|why|how|am|are|do|does|did|will|"
    r"would|can|could|should|is|was|were)\b",
    re.IGNORECASE,
)
_OTHER = re.compile(
    r"\b(?:leave|vacation|appointment|commitment|incident|notes?|logs?|overtime)\b",
    re.IGNORECASE,
)
_ABSENCE = re.compile(
    r"\b(?:didn'?t|did\s+not|was\s+off|were\s+off|was\s+sick|were\s+sick|"
    r"i\s+was\s+sick|called\s+in\s+sick)\b",
    re.IGNORECASE,
)
_PAST = re.compile(
    r"\b(?:didn'?t|did\s+not|was\s+off|were\s+off|was\s+sick|were\s+sick|"
    r"i\s+was\s+sick|yesterday|\blast\b)\b",
    re.IGNORECASE,
)
_FUTURE = re.compile(
    r"\b(?:cancel|next|tomorrow|i'?m\s+off|i\s+am\s+off|im\s+off)\b",
    re.IGNORECASE,
)
_OFF = re.compile(r"\boff\b", re.IGNORECASE)
_REMOVE = re.compile(r"\b(?:remove|clear|delete|cancel)\b", re.IGNORECASE)
_CHANGE = re.compile(r"\b(?:change|switch|make|turn|set)\b", re.IGNORECASE)
_TO_OFF = re.compile(r"\b(?:to|as|into)\s+off\b", re.IGNORECASE)
_TO_NIGHT = re.compile(
    r"\b(?:night\s+shifts?|(?:to|as|into)\s+(?:a\s+)?nights?)\b",
    re.IGNORECASE,
)
_TO_DAY = re.compile(
    r"\b(?:day\s+shifts?|(?:to|as|into)\s+(?:a\s+)?days?)\b",
    re.IGNORECASE,
)
_DAY_OFF = re.compile(r"\bday\s+off\b", re.IGNORECASE)
_FILLER = re.compile(
    r"\b(?:please|mark|set|the|on|off|my|shift|shifts|a|an|me|as|to|for|day|days|"
    r"was|were|i|im|i'm|am)\b",
    re.IGNORECASE,
)


@dataclass
class CalendarRequest:
    payload: Optional[dict] = None
    clarify: Optional[str] = None


def calendar_request(text: str, today: date) -> Optional[CalendarRequest]:
    """An override_days payload, a clarify sentence, or None when this is not ours.

    Questions stay with the schedule answer. Leave, notes, and appointments
    stay with their own tools.
    """
    raw = (text or "").strip()
    if not raw or today is None or _QUESTION.search(raw):
        return None
    if _OTHER.search(raw) and not _ABSENCE.search(raw):
        return None
    target = _target(raw)
    edit = target is not None or _wants_off(raw) or _REMOVE.search(raw) or _date_only(raw, today)
    if not edit:
        return None
    direction = _direction(raw)
    span = parse_spoken_span(raw, today, direction)
    if span.problem == "invalid":
        return CalendarRequest(clarify=span.detail)
    if span.problem == "ambiguous":
        return CalendarRequest(clarify=span.detail or "Which day did you mean?")
    if not span.ok:
        if target in {"work_night", "work_day"}:
            return CalendarRequest(clarify="Which day should I change?")
        return CalendarRequest(clarify="Which day should I clear?")
    work_type = target or "off"
    return CalendarRequest(payload={
        "start_date": span.start.isoformat(),
        "end_date": (span.end or span.start).isoformat(),
        "work_type": work_type,
        "preserve_off_days": False,
    })


def request_span(payload: Optional[dict]):
    """Start and end dates on an override payload, or None."""
    if not isinstance(payload, dict):
        return None
    start = _iso(payload.get("start_date") or payload.get("date"))
    end = _iso(payload.get("end_date")) or start
    if start is None or end is None:
        return None
    if end < start:
        start, end = end, start
    return start, end


def _target(text: str) -> Optional[str]:
    if _ABSENCE.search(text) or _TO_OFF.search(text):
        return "off"
    if _CHANGE.search(text) and _TO_NIGHT.search(text):
        return "work_night"
    if _CHANGE.search(text) and _TO_DAY.search(text) and not _DAY_OFF.search(text):
        return "work_day"
    return None


def _wants_off(text: str) -> bool:
    return bool(_ABSENCE.search(text) or _OFF.search(text))


def _direction(text: str) -> str:
    if _PAST.search(text) and not re.search(r"\bnext\b", text, re.IGNORECASE):
        return "past"
    if _FUTURE.search(text) or _OFF.search(text):
        return "future"
    return "nearest"


def _date_only(text: str, today: date) -> bool:
    """True when the message is a date, optionally with words like 'mark' or 'off'."""
    span = parse_spoken_span(text, today, "nearest")
    leftover = _without_date_words(text)
    if span.problem in {"invalid", "ambiguous"}:
        return leftover == ""
    if not span.ok:
        return False
    return leftover == ""


def _without_date_words(text: str) -> str:
    leftover = text
    for token in re.findall(
        rf"\b(?:\d{{1,2}}(?:st|nd|rd|th)?|{_month_pattern()}|\d{{4}})\b",
        text,
        flags=re.IGNORECASE,
    ):
        leftover = re.sub(rf"\b{re.escape(token)}\b", " ", leftover, count=1, flags=re.IGNORECASE)
    leftover = re.sub(r"[0-9/.,\-–—]+", " ", leftover)
    leftover = _FILLER.sub(" ", leftover)
    leftover = re.sub(
        r"\b(?:yesterday|today|tomorrow|last|this|next|monday|tuesday|"
        r"wednesday|thursday|friday|saturday|sunday)\b",
        " ",
        leftover,
        flags=re.IGNORECASE,
    )
    return leftover.strip(" ?.!")


def _month_pattern() -> str:
    return (
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    )


def _iso(value) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def explain_dates(start: date, end: date) -> str:
    """Card text for a single off day or a range. Kept for tests and prompts."""
    if start == end:
        return f"Mark {speak(start)} as Off"
    return f"Mark {speak(start)} – {speak(end)} as Off"
