"""Work or off for any date, from the cycle rather than stored calendar rows.

calendar_days are a projection. A new rotation is stored from the anchor
through 31 Dec of that year, and the chat sample is only the 60 days after
today (21 Oct 2026 ends on 20 Dec). A question past that sample, or before
the anchor, is still determined by the anchor and the pattern, plus leave
and commitments.
"""

import re
from datetime import date, datetime, timedelta
from typing import Optional

from app.engines.calendar_authority import cycle_is_present, engine_cycle
from app.engines.calendar_engine import CalendarEngine
from app.engines.proposal_effects import chip_for_day, leave_covers


# The prompt shows this many days after today. It is not the stored horizon.
CHAT_SAMPLE_DAYS_FORWARD = 60

_SHIFT = {
    "work_day": "a day shift",
    "work_night": "a night shift",
    "off": "off",
    "blank": "untracked",
}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_NUMBER = {
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
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_DAY_NUMBER = r"(\d{1,2})(?:st|nd|rd|th)?"
_NAMED_DAY_FIRST = re.compile(
    rf"\b{_DAY_NUMBER}\s+({_MONTH_NAME})(?:\s+(\d{{4}}))?\b",
    re.IGNORECASE,
)
_NAMED_MONTH_FIRST = re.compile(
    rf"\b({_MONTH_NAME})\s+{_DAY_NUMBER}(?:,)?(?:\s+(\d{{4}}))?\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_CHRISTMAS = re.compile(r"\bchristmas(?:\s+day)?\b", re.IGNORECASE)
_ASKING = re.compile(
    r"\b(am i|do i work|am i off|working|on shift|day off|have work)\b",
    re.IGNORECASE,
)
# A genuine question, not a mention of work inside a note or a log.
_QUESTION = re.compile(
    r"(?:\?\s*$)|^\s*(am i|do i|can i|could i|will i)\b",
    re.IGNORECASE,
)
_CHANGING = re.compile(
    r"\b(add|mark|book|cancel|change|switch|set|move|delete|remove|undo|clear|"
    r"log|note|record|report|incident|overtime)\b",
    re.IGNORECASE,
)


def chat_sample_end(today: date) -> date:
    """Last day included in the chat prompt's calendar sample."""
    return today + timedelta(days=CHAT_SAMPLE_DAYS_FORWARD)


def project_day(
    cycle: Optional[dict],
    day: date,
    leaves: Optional[list] = None,
    commitments: Optional[list] = None,
) -> dict:
    """Shift, leave, and commitments for one date. Does not read calendar_days."""
    label = _format_day(day)
    if not cycle_is_present(cycle):
        return {
            "date": day.isoformat(),
            "label": label,
            "work_type": None,
            "cycle_day": None,
            "on_leave": False,
            "commitment_names": [],
        }
    prepared = engine_cycle(cycle)
    anchor = date.fromisoformat(str(prepared["anchor_date"])[:10])
    engine = CalendarEngine("day-status")
    cycle_day = engine.calculate_cycle_day(
        day,
        anchor,
        int(prepared.get("anchor_cycle_day") or 1),
        int(prepared.get("cycle_length") or 1),
    )
    work_type = engine.get_work_type_for_cycle_day(cycle_day, prepared["pattern"]).value
    names = []
    for commitment in commitments or []:
        chip = chip_for_day(commitment, day) if isinstance(commitment, dict) else None
        if chip and chip.get("name"):
            names.append(str(chip["name"]))
    return {
        "date": day.isoformat(),
        "label": label,
        "work_type": work_type,
        "cycle_day": cycle_day,
        "on_leave": any(
            leave_covers(leave, day) for leave in (leaves or []) if isinstance(leave, dict)
        ),
        "commitment_names": names,
    }


def answer_sentence(status: dict) -> str:
    """One sentence. No second guess about a stored horizon."""
    when = status.get("label") or status.get("date") or "That day"
    work_type = status.get("work_type")
    if not work_type:
        return f"No rotation is saved, so {when} has no work day or off day yet."
    kind = _SHIFT.get(work_type, str(work_type).replace("_", " "))
    if status.get("on_leave"):
        text = f"No. {when} is leave, so you are not working."
    elif work_type in {"work_day", "work_night"}:
        text = f"Yes. {when} is {kind}."
    else:
        text = f"No. {when} is {kind}."
    names = [name for name in (status.get("commitment_names") or []) if name]
    if not names:
        return text
    if len(names) == 1:
        extra = f"{names[0]} is on that day"
    else:
        extra = f"{', '.join(names[:-1])} and {names[-1]} are on that day"
    return text[:-1] + f", and {extra}."


def schedule_question_date(text: str, today: date) -> Optional[date]:
    """The single date in a real work/off question, or None otherwise.

    Statements and change requests, including a log, note, record, report,
    incident, or overtime entry, fall through to the model and query_calendar.
    """
    if not text or _CHANGING.search(text) or not _QUESTION.search(text):
        return None
    if not _ASKING.search(text):
        return None
    found = dates_in(text, today)
    if len(found) != 1:
        return None
    return found[0]


def status_from_stored(row: Optional[dict]) -> dict:
    """Answer from a stored calendar day, including a manual override."""
    row = row or {}
    raw = row.get("date")
    if isinstance(raw, datetime):
        day = raw.date()
    elif isinstance(raw, date):
        day = raw
    else:
        day = date.fromisoformat(str(raw)[:10])
    state = row.get("state_json") or {}
    if not isinstance(state, dict):
        state = {}
    names = []
    for chip in state.get("commitments") or []:
        if isinstance(chip, dict) and chip.get("name"):
            names.append(str(chip["name"]))
    return {
        "date": day.isoformat(),
        "label": _format_day(day),
        "work_type": row.get("work_type"),
        "cycle_day": row.get("cycle_day"),
        "on_leave": bool(state.get("is_leave")),
        "commitment_names": names,
    }


def dates_in(text: str, today: date) -> list:
    """Dates named in text. Christmas uses the upcoming 25 Dec."""
    found = []
    if _CHRISTMAS.search(text or ""):
        holiday = _upcoming(date(today.year, 12, 25), today)
        found.append(holiday)
    for match in _ISO_DATE.finditer(text or ""):
        parsed = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if parsed:
            found.append(parsed)
    for match in _NAMED_DAY_FIRST.finditer(text or ""):
        parsed = _named_date(match.group(2), int(match.group(1)), match.group(3), today)
        if parsed:
            found.append(parsed)
    for match in _NAMED_MONTH_FIRST.finditer(text or ""):
        parsed = _named_date(match.group(1), int(match.group(2)), match.group(3), today)
        if parsed:
            found.append(parsed)
    unique = []
    for day in found:
        if day not in unique:
            unique.append(day)
    return unique


def date_from_query(payload: Optional[dict], today: date) -> Optional[date]:
    """The date argument of query_calendar."""
    raw = ""
    if isinstance(payload, dict):
        raw = str(payload.get("date") or payload.get("day") or "")
    found = dates_in(raw, today)
    if len(found) == 1:
        return found[0]
    return None


def combine_events(document_leaves, document_commitments, table_leaves, table_commitments):
    """Document rows win when the same id is also in the relational table."""
    return (
        _merge(document_leaves, table_leaves),
        _merge(document_commitments, table_commitments),
    )


def _merge(primary, extra) -> list:
    merged = []
    seen = set()
    for item in list(primary or []) + list(extra or []):
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("id") or ""),
            str(item.get("name") or ""),
            str(item.get("start_date") or item.get("date") or ""),
            str(item.get("end_date") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _format_day(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}"


def _named_date(month_text: str, day_number: int, year_text: Optional[str], today: date) -> Optional[date]:
    month = _MONTH_NUMBER.get(month_text.strip().lower())
    if month is None:
        return None
    if year_text:
        return _safe_date(int(year_text), month, day_number)
    candidate = _safe_date(today.year, month, day_number)
    if candidate is None:
        return None
    return _upcoming(candidate, today)


def _upcoming(day: date, today: date) -> date:
    if day < today:
        try:
            return day.replace(year=day.year + 1)
        except ValueError:
            return day
    return day


def _safe_date(year: int, month: int, day_number: int) -> Optional[date]:
    try:
        return date(year, month, day_number)
    except ValueError:
        return None
