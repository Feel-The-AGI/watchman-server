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
    start, end = _written_span(payload)
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
    start, end = _written_span(payload)
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
    """Card text built only from the payload, for every action a proposal can carry."""
    payload = payload or {}
    writers = {
        "add_commitment": _describe_add_commitment,
        "remove_commitment": _describe_remove_commitment,
        "add_leave": _describe_add_leave,
        "remove_leave": _describe_remove_leave,
        "override_days": _describe_override,
        "update_cycle": _describe_cycle,
        "create_daily_log": _describe_daily_log,
        "create_incident": _describe_incident,
        "copy_incident": _describe_copy_incident,
        "update_constraint": _describe_update_constraint,
        "remove_constraint": _describe_remove_constraint,
        "undo": lambda _payload: "Undo your last change",
        "redo": lambda _payload: "Redo your last undone change",
    }
    writer = writers.get(action)
    if writer is None:
        return "Update the calendar"
    return writer(payload)


def _describe_add_commitment(payload: dict) -> str:
    name = _label(payload.get("name"), "commitment")
    clock = _clock_phrase(payload.get("time_start"), payload.get("time_end"))
    weekdays = _weekdays(payload.get("days_of_week"))
    # A single day is that day, even when the payload also names a weekday.
    if weekdays and not _is_one_day(payload):
        text = f"Add {name} {_every_phrase(weekdays)}"
        span = _compact_range(payload)
        if span:
            text += f", {span}"
        if clock:
            text += f", {clock}"
        return text
    when = _when_phrase(payload)
    text = f"Add {name}"
    if when:
        text += f" on {when}" if _is_one_day(payload) else f" from {when}"
    if clock:
        text += f", {clock}"
    return text


def _describe_remove_commitment(payload: dict) -> str:
    return _describe_removal("commitment", payload)


def _describe_add_leave(payload: dict) -> str:
    when = _when_phrase(payload)
    name = str(payload.get("name") or "").strip()
    if name.lower() in _GENERIC_LEAVE_NAMES:
        return f"Mark {when} as leave" if when else "Mark leave"
    if when:
        return f"Add {name} on {when}" if _is_one_day(payload) else f"Add {name} from {when}"
    return f"Add {name}"


def _describe_remove_leave(payload: dict) -> str:
    return _describe_removal("leave", payload)


def _describe_removal(kind: str, payload: dict) -> str:
    name = str(payload.get("name") or "").strip()
    when = _when_phrase(payload)
    if name and when:
        return f"Remove {name} on {when}" if _is_one_day(payload) else f"Remove {name}, {when}"
    if name:
        return f"Remove {name}"
    if when:
        return f"Remove {kind} on {when}" if _is_one_day(payload) else f"Remove {kind}, {when}"
    item_id = str(payload.get("id") or "").strip()
    if item_id:
        return f"Remove {kind} {item_id}"
    return f"Remove that {kind}"


def _describe_override(payload: dict) -> str:
    start = _as_date(payload.get("start_date") or payload.get("date"))
    end = _as_date(payload.get("end_date")) or start
    work = str(payload.get("work_type") or "").strip().lower()
    if work in {"off", "rest"}:
        return f"Mark {_full_span(start, end)} as Off"
    if start and end and start != end:
        span = f"{_format_day(start)} \u2013 {_format_day(end)}"
    elif start:
        span = _format_day(start)
    else:
        span = "those days"
    kind = _SHIFT_NAMES.get(work, "that shift")
    text = f"Set {span} to {kind}"
    if _keeps_off_days(payload):
        text += ", keeping off days"
    return text


def no_change_message(start: date, end: date, work_type: str) -> str:
    """Honest review text when approving the command would not change a day."""
    label = _ALREADY_LABEL.get(str(work_type or "").strip().lower(), "that shift")
    if start == end:
        return f"No change: {_format_full(start)} was already {label}"
    return f"No change: {_format_full(start)} \u2013 {_format_full(end)} were already {label}"


def _full_span(start: Optional[date], end: Optional[date]) -> str:
    if start and end and start != end:
        return f"{_format_full(start)} \u2013 {_format_full(end)}"
    if start:
        return _format_full(start)
    return "those days"


def _format_full(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]} {day.year}"


_ALREADY_LABEL = {
    "off": "Off",
    "rest": "Off",
    "work_day": "Day",
    "day": "Day",
    "day_shift": "Day",
    "work_night": "Night",
    "night": "Night",
    "night_shift": "Night",
    "blank": "Blank",
    "untracked": "Blank",
    "unknown": "Blank",
}


def _describe_cycle(payload: dict) -> str:
    from app.engines.command_executor import cycle_shift_days

    count = cycle_shift_days(payload)
    if count is not None:
        unit = "day" if abs(count) == 1 else "days"
        direction = "later" if count > 0 else "earlier"
        return f"Move your rotation {abs(count)} {unit} {direction}"
    pattern = _pattern_phrase(payload.get("pattern"))
    anchor = _as_date(payload.get("anchor_date"))
    cycle_day = payload.get("anchor_cycle_day")
    anchor_block = payload.get("anchor")
    if isinstance(anchor_block, dict):
        if anchor is None:
            anchor = _as_date(anchor_block.get("date"))
        if cycle_day in (None, ""):
            cycle_day = anchor_block.get("cycle_day")
    pieces = []
    if pattern:
        pieces.append(pattern)
    if anchor is not None:
        anchor_text = f"anchored on {_format_day(anchor)}"
        if cycle_day not in (None, ""):
            anchor_text += f" as day {cycle_day}"
        pieces.append(anchor_text)
    elif cycle_day not in (None, ""):
        pieces.append(f"anchor day {cycle_day}")
    if not pieces:
        return "Update the rotation"
    return "Set rotation to " + ", ".join(pieces)


def _describe_daily_log(payload: dict) -> str:
    day = _as_date(payload.get("date"))
    when = _format_day(day) if day else "that day"
    text = f"Log a note for {when}"
    hours = _hour_phrase(payload.get("actual_hours"))
    if hours:
        text += f", {hours}"
    overtime = payload.get("overtime_hours")
    if overtime not in (None, "", 0, 0.0):
        extra = _hour_phrase(overtime)
        if extra:
            text += f", {extra} overtime"
    return text


def _describe_incident(payload: dict) -> str:
    title = _label(payload.get("title"), "incident")
    day = _as_date(payload.get("date"))
    when = _format_day(day) if day else "that day"
    return f"Log incident: {title} on {when}"


def _describe_copy_incident(payload: dict) -> str:
    source = _as_date(payload.get("source_date"))
    target = _as_date(payload.get("target_date"))
    left = _format_day(source) if source else "that day"
    right = _format_day(target) if target else "that day"
    return f"Copy the {left} incident to {right}"


def _describe_update_constraint(payload: dict) -> str:
    label = _constraint_label(payload)
    if label:
        return f"Update constraint: {label}"
    return "Update that constraint"


def _describe_remove_constraint(payload: dict) -> str:
    label = _constraint_label(payload)
    if label:
        return f"Remove constraint: {label}"
    return "Remove that constraint"


_SHIFT_NAMES = {
    "work_day": "day shift",
    "day_shift": "day shift",
    "day": "day shift",
    "work_night": "night shift",
    "night_shift": "night shift",
    "night": "night shift",
    "off": "off",
    "rest": "off",
    "blank": "blank",
    "untracked": "blank",
    "unknown": "blank",
}

_WEEKDAY_SHORT = {
    "monday": "Mon",
    "mon": "Mon",
    "tuesday": "Tue",
    "tue": "Tue",
    "tues": "Tue",
    "wednesday": "Wed",
    "wed": "Wed",
    "thursday": "Thu",
    "thu": "Thu",
    "thur": "Thu",
    "thurs": "Thu",
    "friday": "Fri",
    "fri": "Fri",
    "saturday": "Sat",
    "sat": "Sat",
    "sunday": "Sun",
    "sun": "Sun",
}


def explanation_for(action: str, payload: Optional[dict], model_text: Optional[str] = None) -> str:
    """Card text the user approves. Always built from the payload.

    The model's sentence is not used here. A date in that sentence can
    disagree with the dates Apply writes.
    """
    return describe_command(action, payload or {})


def card_command(command: Optional[dict]) -> dict:
    """Proposal command whose explanation is the payload summary.

    ``payload`` is copied unchanged. A useful model sentence is kept on
    ``model_text``, separate from the text the user approves.
    """
    source = dict(command or {})
    action = str(source.get("action") or "")
    payload = source.get("payload") if isinstance(source.get("payload"), dict) else {}
    payload = dict(payload)
    summary = describe_command(action, payload)
    stored = {
        key: value
        for key, value in source.items()
        if key != "model_text"
    }
    stored["action"] = action
    stored["payload"] = payload
    stored["explanation"] = summary
    note = _model_note(
        action,
        summary,
        source.get("model_text"),
        source.get("explanation"),
    )
    if note:
        stored["model_text"] = note
    return stored


def _model_note(action: str, summary: str, model_text: Any, explanation: Any) -> Optional[str]:
    """The model's own sentence, when it is not the card text and not a stub."""
    for candidate in (model_text, explanation):
        text = str(candidate or "").strip()
        if not text or text == summary or _is_generic_model_text(text, action):
            continue
        return text
    return None


def _is_generic_model_text(text: str, action: str) -> bool:
    lowered = text.lower()
    if lowered.startswith("executing "):
        return True
    return lowered == f"executing {action}".lower()


def _written_span(payload: dict):
    """Dates stored on a row. A present value that is not ISO raises.

    Card text uses the lenient parser and can describe a bad date as absent.
    Writing the row must not turn 'next tuesday' into a dateless commitment
    or drop a bad end date back to a single day.
    """
    start_raw = payload.get("start_date")
    if start_raw in (None, "") and payload.get("date") not in (None, ""):
        start_raw = payload.get("date")
    start = _strict_date(start_raw, "start_date")
    end_raw = payload.get("end_date")
    if end_raw in (None, ""):
        return start, start
    return start, _strict_date(end_raw, "end_date")


def _strict_date(value: Any, field: str) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError(f"{field} {text!r} is not a YYYY-MM-DD date") from exc


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
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


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


def _label(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


def _constraint_label(payload: dict) -> str:
    for key in ("description", "name"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    rule = payload.get("rule")
    if isinstance(rule, dict):
        rule = rule.get("type") or rule.get("name") or ""
    text = str(rule or "").strip()
    if text:
        return text.replace("_", " ")
    return str(payload.get("id") or "").strip()


def _keeps_off_days(payload: dict) -> bool:
    if "preserve_off_days" not in payload:
        return True
    value = payload.get("preserve_off_days")
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no"}
    return bool(value)


def _pattern_phrase(pattern: Any) -> str:
    if not isinstance(pattern, list):
        return ""
    parts = []
    for block in pattern:
        if not isinstance(block, dict):
            continue
        raw = str(block.get("label") or block.get("type") or "").strip()
        try:
            duration = int(block.get("duration", block.get("days")))
        except (TypeError, ValueError):
            duration = None
        kind = {
            "work_day": "day",
            "day_shift": "day",
            "work_night": "night",
            "night_shift": "night",
            "off": "off",
        }.get(raw, raw.replace("_", " "))
        if duration is None:
            if kind:
                parts.append(kind)
            continue
        unit = "day" if duration == 1 else "days"
        if kind == "off":
            parts.append(f"{duration} {unit} off")
        elif kind in {"day", "night"}:
            shift = "shift" if duration == 1 else "shifts"
            parts.append(f"{duration} {kind} {shift}")
        else:
            parts.append(f"{duration} {unit} {kind}")
    return ", ".join(parts)


def _every_phrase(days: list) -> str:
    labels = []
    for day in days:
        short = _WEEKDAY_SHORT.get(str(day).strip().lower(), str(day).strip()[:3].title())
        if short and short not in labels:
            labels.append(short)
    if not labels:
        return "every week"
    if len(labels) == 1:
        return f"every {labels[0]}"
    if len(labels) == 2:
        return f"every {labels[0]} and {labels[1]}"
    return "every " + ", ".join(labels[:-1]) + f" and {labels[-1]}"


def _compact_range(payload: dict) -> str:
    start = _as_date(payload.get("start_date") or payload.get("date"))
    end = _as_date(payload.get("end_date")) or start
    if start is None:
        return ""
    if end is None or end == start:
        return _month_day(start)
    return f"{_month_day(start)} \u2013 {_month_day(end)}"


def _month_day(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]}"


def _hour_phrase(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number == int(number):
        shown = int(number)
    else:
        shown = number
    unit = "hour" if shown == 1 else "hours"
    return f"{shown} {unit}"


def _ampm(value: str):
    hour_text, minute_text = value.strip().split(":")[:2]
    hour = int(hour_text)
    minute = int(minute_text)
    suffix = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    label = str(hour12) if minute == 0 else f"{hour12}:{minute:02d}"
    return label, suffix
