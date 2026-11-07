"""A rotation that changes on a date, without rewriting the days before it.

The Master Settings cycle is the rotation in force now. Earlier rotations
live in ``cycle["history"]``, oldest first. Each entry is a flat rotation
(``pattern``, ``anchor_date``, ``anchor_cycle_day``, ``total_days``) plus
``until``, the last date it governs. Entry N covers the day after entry
N-1's ``until`` through its own ``until``. The first entry has no start, so it
covers every earlier day. The current rotation covers the day after the last
``until`` onward, or every day when there is no history.

A change from a date keeps every day before that date on the rotation that
already governed it. A correction replaces the current rotation and keeps the
history, so a fix to today's rotation never rewrites an earlier one.
"""

from datetime import date, timedelta
from typing import Iterator, List, Optional, Tuple

_ROTATION_KEYS = ("name", "pattern", "anchor_date", "anchor_cycle_day", "total_days")


def parse_day(value) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def flat_rotation(cycle: Optional[dict]) -> dict:
    """Pattern and anchor in the flat shape, whichever shape was stored."""
    cycle = cycle if isinstance(cycle, dict) else {}
    anchor_date = cycle.get("anchor_date")
    anchor_cycle_day = cycle.get("anchor_cycle_day")
    nested = cycle.get("anchor")
    if isinstance(nested, dict):
        anchor_date = anchor_date or nested.get("date")
        if anchor_cycle_day in (None, ""):
            anchor_cycle_day = nested.get("cycle_day")
    pattern = []
    for block in cycle.get("pattern") or []:
        if not isinstance(block, dict):
            continue
        if "label" in block:
            pattern.append({"label": block["label"], "duration": int(block.get("duration") or 0)})
        elif "type" in block:
            label = block["type"]
            if label == "day_shift":
                label = "work_day"
            elif label == "night_shift":
                label = "work_night"
            pattern.append({
                "label": label,
                "duration": int(block.get("days", block.get("duration", 0)) or 0),
            })
    total = sum(block["duration"] for block in pattern) or cycle.get("total_days") or cycle.get("cycle_length")
    flat = {
        "pattern": pattern,
        "anchor_date": str(anchor_date)[:10] if anchor_date else None,
        "anchor_cycle_day": int(anchor_cycle_day or 1),
        "total_days": total,
    }
    if cycle.get("name"):
        flat["name"] = cycle["name"]
    return flat


def history_of(cycle: Optional[dict]) -> List[dict]:
    """Valid history entries, oldest first. Malformed entries are skipped."""
    if not isinstance(cycle, dict):
        return []
    entries = []
    for raw in cycle.get("history") or []:
        if not isinstance(raw, dict):
            continue
        until = parse_day(raw.get("until"))
        flat = flat_rotation(raw)
        if until is None or not flat["pattern"] or not flat["anchor_date"]:
            continue
        entries.append({**flat, "until": until.isoformat()})
    entries.sort(key=lambda entry: entry["until"])
    return entries


def current_start(cycle: Optional[dict]) -> Optional[date]:
    """First date the current rotation governs. None means every date."""
    entries = history_of(cycle)
    if not entries:
        return None
    return parse_day(entries[-1]["until"]) + timedelta(days=1)


def spans(cycle: Optional[dict]) -> List[Tuple[Optional[date], Optional[date], dict]]:
    """(start, end, rotation) for each rotation. None is an open end."""
    result = []
    previous_end: Optional[date] = None
    for entry in history_of(cycle):
        end = parse_day(entry["until"])
        start = previous_end + timedelta(days=1) if previous_end else None
        result.append((start, end, entry))
        previous_end = end
    start = previous_end + timedelta(days=1) if previous_end else None
    result.append((start, None, flat_rotation(cycle)))
    return result


def rotation_on(cycle: Optional[dict], day: date) -> dict:
    """The flat rotation that governs one date."""
    for start, end, rotation in spans(cycle):
        if (start is None or day >= start) and (end is None or day <= end):
            return rotation
    return flat_rotation(cycle)


def split_range(
    cycle: Optional[dict], start: date, end: date
) -> Iterator[Tuple[date, date, dict]]:
    """Pieces of an inclusive range, each with the rotation that governs it."""
    for span_start, span_end, rotation in spans(cycle):
        piece_start = max(start, span_start) if span_start else start
        piece_end = min(end, span_end) if span_end else end
        if piece_start <= piece_end:
            yield piece_start, piece_end, rotation


def earliest_anchor(cycle: Optional[dict]) -> Optional[date]:
    """The earliest anchor across every rotation, for generation bounds."""
    anchors = [parse_day(rotation.get("anchor_date")) for _, _, rotation in spans(cycle)]
    anchors = [day for day in anchors if day is not None]
    return min(anchors) if anchors else None


def _work_type(rotation: dict, day: date) -> Optional[str]:
    anchor = parse_day(rotation.get("anchor_date"))
    pattern = rotation.get("pattern") or []
    length = sum(block.get("duration", 0) for block in pattern)
    if anchor is None or not length:
        return None
    position = (int(rotation.get("anchor_cycle_day") or 1) - 1 + (day - anchor).days) % length
    for block in pattern:
        if position < block["duration"]:
            return block["label"]
        position -= block["duration"]
    return None


def same_schedule(left: dict, right: dict, start: date, days: int = 0) -> bool:
    """True when two rotations put the same shift on every day from start.

    Two rotations agree from any date on once they agree for one full cycle of
    each, so checking the longer cycle twice over is enough.
    """
    lengths = [
        sum(block.get("duration", 0) for block in (rotation.get("pattern") or []))
        for rotation in (left, right)
    ]
    if not all(lengths):
        return False
    span = days or max(lengths) * 2
    return all(
        _work_type(left, start + timedelta(days=offset))
        == _work_type(right, start + timedelta(days=offset))
        for offset in range(span)
    )


def change_from(existing: Optional[dict], new_rotation: dict, effective_from: date) -> dict:
    """A cycle whose current rotation starts on effective_from.

    Every day before effective_from keeps the rotation that governed it. A
    history entry that starts on or after effective_from is dropped, because
    the new rotation now governs those days. When the new rotation puts the
    same shifts on the calendar as the old one, the history is not extended.
    """
    existing = existing if isinstance(existing, dict) else {}
    old = flat_rotation(existing)
    kept = []
    if old["pattern"] and old["anchor_date"]:
        begins = current_start(existing)
        same = same_schedule(old, flat_rotation(new_rotation), effective_from)
        if same and (begins is None or begins <= effective_from):
            # Nothing changes from effective_from on. Keep the history as it is
            # and only store the new description of the same schedule.
            return {**new_rotation, "history": history_of(existing)}
        last_day = effective_from - timedelta(days=1)
        for start, end, rotation in spans(existing):
            if start is not None and start >= effective_from:
                continue
            until = last_day if end is None or end > last_day else end
            kept.append({
                **{key: rotation[key] for key in _ROTATION_KEYS if key in rotation},
                "until": until.isoformat(),
            })
    return {**new_rotation, "history": kept}


def correction(existing: Optional[dict], new_rotation: dict, whole_calendar: bool = False) -> dict:
    """Replace the current rotation from its own start.

    History is kept, so fixing today's rotation never rewrites an earlier
    one. ``whole_calendar`` drops the history too: the user says this was
    always their rotation, so every date follows it.
    """
    return {**new_rotation, "history": [] if whole_calendar else history_of(existing)}
