"""Persist an approved chat command and describe it in plain language.

The chat tool payload is not the row the calendar reads. Approving
add_commitment has to insert public.commitments and paint that row onto
calendar_days. Approving add_leave inserts public.leave_blocks. A leave
type such as "other" stays on the leave row; it is not a commitments.type.
"""

from datetime import date, datetime, timedelta
from typing import Any, Optional

COMMITMENT_TYPES = {"work", "education", "personal", "leave", "study", "sleep", "other"}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_GENERIC_LEAVE_NAMES = {"", "leave", "other"}


def commitment_record(
    user_id: str,
    payload: Optional[dict],
    commitment_id: str,
    proposal_id: Optional[str] = None,
) -> dict:
    """Row for public.commitments. Raises if the type is not a real enum label."""
    payload = payload or {}
    kind = str(payload.get("type") or "personal").strip().lower()
    if kind not in COMMITMENT_TYPES:
        raise ValueError(f"Commitment type {kind!r} is not supported")
    start = _as_date(payload.get("start_date") or payload.get("date"))
    end = _as_date(payload.get("end_date")) or start
    days = _weekdays(payload.get("days_of_week"))
    time_start = _clock_value(payload.get("time_start"))
    time_end = _clock_value(payload.get("time_end"))
    hours = _hours(time_start, time_end)
    name = str(payload.get("name") or "Commitment").strip() or "Commitment"
    return {
        "id": commitment_id,
        "user_id": user_id,
        "name": name,
        "type": kind,
        "status": "active",
        "priority": int(payload.get("priority") or 1),
        "constraints_json": {
            "days_of_week": days,
            "time_start": time_start,
            "time_end": time_end,
            "duration_hours": hours,
        },
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "recurrence": {
            "type": "weekly" if days else "once",
            "days": days,
            "time_start": time_start,
            "time_end": time_end,
        },
        "source": "chat",
        "source_text": proposal_id,
        "color": payload.get("color") or "#2979FF",
    }


def leave_record(
    user_id: str,
    payload: Optional[dict],
    leave_id: str,
    proposal_id: Optional[str] = None,
) -> dict:
    """Row for public.leave_blocks. The payload type is stored in effects, not commitments.type."""
    payload = payload or {}
    start = _as_date(payload.get("start_date") or payload.get("date"))
    end = _as_date(payload.get("end_date")) or start
    if start is None or end is None:
        raise ValueError("Leave needs a date")
    if end < start:
        raise ValueError("Leave end date is before the start date")
    name = str(payload.get("name") or "Leave").strip() or "Leave"
    return {
        "id": leave_id,
        "user_id": user_id,
        "name": name,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "effects": {
            "work": "suspended",
            "available_time": "increased",
            "type": payload.get("type") or "other",
            "proposal_id": proposal_id,
        },
        "notes": payload.get("notes") or "",
    }


def chip_for_day(commitment: dict, day: date) -> Optional[dict]:
    """Calendar chip when this commitment occurs on day, otherwise None."""
    start = _as_date(commitment.get("start_date") or commitment.get("date"))
    end = _as_date(commitment.get("end_date")) or start
    if start is None or end is None or day < start or day > end:
        return None
    constraints = commitment.get("constraints_json") or {}
    recurrence = commitment.get("recurrence") or {}
    days = _weekdays(
        constraints.get("days_of_week")
        or recurrence.get("days")
        or commitment.get("days_of_week")
    )
    if not _day_matches(days, day):
        return None
    time_start = (constraints.get("time_start") or recurrence.get("time_start") or commitment.get("time_start"))
    time_end = (constraints.get("time_end") or recurrence.get("time_end") or commitment.get("time_end"))
    hours = constraints.get("duration_hours")
    if hours is None:
        hours = _hours(time_start, time_end)
    return {
        "commitment_id": commitment.get("id"),
        "name": commitment.get("name"),
        "type": commitment.get("type"),
        "hours": hours,
        "time_start": time_start,
        "time_end": time_end,
    }


def leave_covers(leave: dict, day: date) -> bool:
    start = _as_date(leave.get("start_date") or leave.get("date"))
    end = _as_date(leave.get("end_date")) or start
    return start is not None and end is not None and start <= day <= end


def commitment_is_stampable(commitment: dict) -> bool:
    """Active rows are stamped. A settings record with no status is too.

    Paused, completed, and any other status are not painted onto the day.
    """
    if not isinstance(commitment, dict):
        return False
    status = commitment.get("status")
    if status is None or str(status).strip() == "":
        return True
    return str(status).strip().lower() == "active"


def stamp_day_state(state: Optional[dict], day: date, commitments: list, leaves: list) -> dict:
    """Return the day state with these commitments and leave blocks applied once."""
    base = dict(state or {})
    rows = [item for item in (commitments or []) if isinstance(item, dict)]
    known_ids = {item.get("id") for item in rows if item.get("id")}
    kept = [
        chip for chip in (base.get("commitments") or [])
        if chip.get("commitment_id") not in known_ids
    ]
    added = []
    for commitment in rows:
        if not commitment_is_stampable(commitment):
            continue
        chip = chip_for_day(commitment, day)
        if chip:
            added.append(chip)
    base["commitments"] = kept + added
    base["used_hours"] = sum(float(chip.get("hours") or 0) for chip in base["commitments"])
    on_leave = any(leave_covers(leave, day) for leave in leaves)
    if on_leave:
        base["is_leave"] = True
        base["available_hours"] = 16.0
        tags = list(base.get("tags") or [])
        if "leave" not in tags:
            tags.append("leave")
        base["tags"] = tags
    available = float(base.get("available_hours") or 0)
    base["is_overloaded"] = base["used_hours"] > available if available else False
    return base


def event_dates(commitments: list, leaves: list) -> list:
    """Dates an approved commitment or leave should be visible on."""
    found = []
    seen = set()

    def add(day: Optional[date]):
        if day is None or day in seen:
            return
        seen.add(day)
        found.append(day)

    for commitment in commitments:
        span = _span(commitment)
        if span is None:
            continue
        start, end = span
        current = start
        while current <= end:
            if chip_for_day(commitment, current):
                add(current)
            current += timedelta(days=1)
    for leave in leaves:
        span = _span(leave)
        if span is None:
            continue
        start, end = span
        current = start
        while current <= end:
            add(current)
            current += timedelta(days=1)
    return found


def describe_command(action: str, payload: Optional[dict]) -> str:
    """Short card text such as 'Add Dentist appointment on Tue 27 Oct, 3–4pm'."""
    payload = payload or {}
    if action == "add_commitment":
        name = str(payload.get("name") or "commitment").strip() or "commitment"
        when = _when_phrase(payload)
        clock = _clock_phrase(payload.get("time_start"), payload.get("time_end"))
        text = f"Add {name}"
        if when:
            text += f" on {when}" if _is_one_day(payload) else f" from {when}"
        if clock:
            text += f", {clock}"
        return text
    if action == "add_leave":
        when = _when_phrase(payload)
        name = str(payload.get("name") or "").strip()
        if name.lower() in _GENERIC_LEAVE_NAMES:
            return f"Mark {when} as leave" if when else "Mark leave"
        if when:
            return f"Add {name} on {when}" if _is_one_day(payload) else f"Add {name} from {when}"
        return f"Add {name}"
    label = str(payload.get("name") or action or "change").replace("_", " ")
    return label[:1].upper() + label[1:] if label else "Update the calendar"


def explanation_for(action: str, payload: Optional[dict], model_text: Optional[str]) -> str:
    """Use the model's sentence only when it actually described the change."""
    text = (model_text or "").strip()
    if text and not _is_generic_model_text(text, action):
        return text
    return describe_command(action, payload)


def _is_generic_model_text(text: str, action: str) -> bool:
    lowered = text.lower()
    if lowered.startswith("executing "):
        return True
    return lowered == f"executing {action}".lower()


def _as_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


def _span(record: dict):
    start = _as_date(record.get("start_date") or record.get("date"))
    end = _as_date(record.get("end_date")) or start
    if start is None or end is None:
        return None
    if end < start:
        end = start
    if (end - start).days > 366:
        end = start + timedelta(days=366)
    return start, end


def _day_matches(days: list, day: date) -> bool:
    if not days:
        return True
    full = day.strftime("%A").lower()
    short = _WEEKDAYS[day.weekday()].lower()
    for item in days:
        if item in {full, short} or item.startswith(short) or full.startswith(item):
            return True
    return False


def _weekdays(value: Any) -> list:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    days = []
    for item in value:
        name = str(item).strip().lower()
        if name:
            days.append(name)
    return days


def _clock_value(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value).strip()


def _hours(start: Optional[str], end: Optional[str]) -> float:
    if not start or not end:
        return 1.0
    try:
        start_minutes = _minutes(start)
        end_minutes = _minutes(end)
    except ValueError:
        return 1.0
    if end_minutes <= start_minutes:
        return 1.0
    return round((end_minutes - start_minutes) / 60, 2)


def _minutes(value: str) -> int:
    hour_text, minute_text = str(value).split(":")[:2]
    return int(hour_text) * 60 + int(minute_text)


def _format_day(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}"


def _is_one_day(payload: dict) -> bool:
    start = _as_date(payload.get("start_date") or payload.get("date"))
    end = _as_date(payload.get("end_date")) or start
    return start is not None and start == end


def _when_phrase(payload: dict) -> str:
    start = _as_date(payload.get("start_date") or payload.get("date"))
    end = _as_date(payload.get("end_date")) or start
    if start is None:
        return ""
    if end is None or end == start:
        return _format_day(start)
    return f"{_format_day(start)} to {_format_day(end)}"


def _clock_phrase(start: Any, end: Any) -> str:
    if not start:
        return ""
    try:
        left, left_suffix = _ampm(str(start))
    except ValueError:
        return ""
    if not end:
        return f"{left}{left_suffix}"
    try:
        right, right_suffix = _ampm(str(end))
    except ValueError:
        return f"{left}{left_suffix}"
    if left_suffix == right_suffix:
        return f"{left}\u2013{right}{right_suffix}"
    return f"{left}{left_suffix}\u2013{right}{right_suffix}"


def _ampm(value: str):
    hour_text, minute_text = value.strip().split(":")[:2]
    hour = int(hour_text)
    minute = int(minute_text)
    suffix = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    label = str(hour12) if minute == 0 else f"{hour12}:{minute:02d}"
    return label, suffix
