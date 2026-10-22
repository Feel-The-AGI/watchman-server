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
# A bare "didn't" is not enough. The words that follow have to be about missing work.
_ABSENCE = re.compile(
    r"\b(?:didn'?t\s+go\s+to\s+work|didn'?t\s+work|did\s+not\s+work|"
    r"was\s+off|was\s+sick|called\s+in\s+sick)\b",
    re.IGNORECASE,
)


@dataclass
class CalendarRequest:
    payload: Optional[dict] = None
    clarify: Optional[str] = None


def calendar_request(text: str, today: date) -> Optional[CalendarRequest]:
    """An Off override, a clarify sentence, or None so the model can answer.

    Only a missed-work phrase with a date is handled here. Anything else,
    including "cancel my subscription" or "I'm off to bed", goes to the model.
    A missed-work phrase with no date also goes to the model.
    """
    raw = (text or "").strip()
    if not raw or today is None or _QUESTION.search(raw) or not _ABSENCE.search(raw):
        return None
    span = parse_spoken_span(raw, today, "past")
    if span.problem in {"invalid", "ambiguous"}:
        return CalendarRequest(clarify=span.detail or "Which day did you mean?")
    if not span.ok:
        return None
    return CalendarRequest(payload={
        "start_date": span.start.isoformat(),
        "end_date": (span.end or span.start).isoformat(),
        "work_type": "off",
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
