"""Master Settings is the calendar source.

Relational cycle, leave, and constraint rows are read only when the
document does not already have that section. They are never copied over
a document the user or the agent already filled in.
"""

from datetime import date, timedelta
from typing import Optional

from loguru import logger

from app.engines.master_settings_service import MasterSettingsService


STUDY_ACTIVITIES = {"study", "education"}
MAX_RULE_SPAN_DAYS = 366


def constraint_is_active(constraint: dict) -> bool:
    if not isinstance(constraint, dict):
        return False
    if constraint.get("is_active") is False or constraint.get("active") is False:
        return False
    return True


def _study_work_types(rule) -> list:
    """Work types a study ban names. Accepts the client object and older shapes."""
    if rule == "no_study_on_night_shift":
        return ["work_night"]
    if isinstance(rule, str) and rule.startswith("no_study_on:"):
        value = rule.split(":", 1)[1].strip()
        return [value] if value else []
    if not isinstance(rule, dict):
        return []
    rule_type = rule.get("type")
    if rule_type == "no_study_on":
        values = []
        if rule.get("value") not in (None, ""):
            values.append(rule.get("value"))
        values.extend(rule.get("values") or [])
        return [str(value) for value in values if value not in (None, "")]
    if rule_type == "no_activity_on":
        activity = str(rule.get("activity") or "study").lower()
        if activity in STUDY_ACTIVITIES:
            return [str(value) for value in (rule.get("work_types") or [])]
    return []


def normalized_rule(constraint: dict) -> Optional[dict]:
    """Canonical study ban, or the original rule object when it is something else."""
    if not isinstance(constraint, dict):
        return None
    work_types = _study_work_types(constraint.get("rule"))
    if work_types:
        return {
            "type": "no_activity_on",
            "activity": "study",
            "work_types": work_types,
        }
    rule = constraint.get("rule")
    if isinstance(rule, dict) and rule.get("type"):
        return rule
    return None


def is_night_study_rule(constraint: dict) -> bool:
    """True for an active ban on study during night shifts."""
    if not constraint_is_active(constraint):
        return False
    rule = normalized_rule(constraint)
    if not rule or rule.get("type") != "no_activity_on":
        return False
    activity = str(rule.get("activity") or "study").lower()
    if activity not in STUDY_ACTIVITIES:
        return False
    return "work_night" in (rule.get("work_types") or [])


def _rule_signature(constraint: dict) -> tuple:
    rule = normalized_rule(constraint) or {}
    work_types = tuple(rule.get("work_types") or [])
    return (rule.get("type"), str(rule.get("activity") or ""), work_types)


def merge_constraints(document: Optional[list], relational: Optional[list]) -> list:
    """Document rules win. Relational rules are added only when not already present."""
    merged = [item for item in (document or []) if isinstance(item, dict)]
    seen = {_rule_signature(item) for item in merged}
    for item in relational or []:
        if not isinstance(item, dict) or not constraint_is_active(item):
            continue
        signature = _rule_signature(item)
        if signature in seen or signature == (None, "", tuple()):
            continue
        merged.append(item)
        seen.add(signature)
    return merged


def cycle_is_present(cycle: Optional[dict]) -> bool:
    if not isinstance(cycle, dict):
        return False
    return bool(cycle.get("pattern") or cycle.get("anchor_date") or cycle.get("anchor"))


def may_replace_cycle(existing: Optional[dict], incoming: Optional[dict]) -> bool:
    """A relational cycle may fill an empty document, or update the same cycle id."""
    if not cycle_is_present(existing):
        return True
    existing_id = existing.get("id") if isinstance(existing, dict) else None
    incoming_id = incoming.get("id") if isinstance(incoming, dict) else None
    return bool(existing_id and incoming_id and str(existing_id) == str(incoming_id))


def cycle_from_row(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "pattern": row.get("pattern"),
        "anchor_date": row.get("anchor_date"),
        "anchor_cycle_day": row.get("anchor_cycle_day"),
        "total_days": row.get("total_days") or row.get("cycle_length"),
    }


def projection_inputs(
    settings: Optional[dict],
    table_cycle: Optional[dict],
    table_leave: Optional[list],
    table_constraints: Optional[list],
) -> dict:
    """Choose calendar inputs without writing. Master Settings wins when present."""
    document = settings or {}
    cycle = document.get("cycle") if cycle_is_present(document.get("cycle")) else None
    if cycle is None and table_cycle:
        cycle = cycle_from_row(table_cycle)
    # An explicit empty list is the document's choice. Missing or null falls back.
    if document.get("leave_blocks") is None:
        leave = list(table_leave or [])
    else:
        leave = document.get("leave_blocks")
    constraints = merge_constraints(document.get("constraints"), table_constraints)
    return {"cycle": cycle, "leave_blocks": leave, "constraints": constraints}


def engine_cycle(cycle: dict) -> dict:
    """Flatten a Master Settings cycle into the calendar engine's shape."""
    anchor_date = None
    anchor_cycle_day = 1
    anchor = cycle.get("anchor")
    if isinstance(anchor, dict):
        anchor_date = anchor.get("date")
        anchor_cycle_day = anchor.get("cycle_day") or 1
    if cycle.get("anchor_date"):
        anchor_date = cycle.get("anchor_date")
        anchor_cycle_day = cycle.get("anchor_cycle_day") or anchor_cycle_day
    raw_pattern = cycle.get("pattern") or []
    pattern = []
    for block in raw_pattern:
        if not isinstance(block, dict):
            continue
        if "label" in block:
            pattern.append({"label": block["label"], "duration": block.get("duration") or 0})
        elif "type" in block:
            pattern.append({
                "label": block["type"],
                "duration": block.get("days", block.get("duration", 0)),
            })
    length = cycle.get("cycle_length") or cycle.get("total_days") or sum(
        block["duration"] for block in pattern
    )
    return {
        "id": cycle.get("id"),
        "anchor_date": anchor_date,
        "anchor_cycle_day": anchor_cycle_day or 1,
        "cycle_length": length or 1,
        "pattern": pattern,
    }


def annotate_night_study(state: dict, work_type: str, constraints: Optional[list]) -> dict:
    """Record study bans on a generated day. Other state is preserved."""
    updated = dict(state or {})
    blocked_types = set()
    for item in constraints or []:
        if not constraint_is_active(item):
            continue
        rule = normalized_rule(item) or {}
        if rule.get("type") != "no_activity_on":
            continue
        if str(rule.get("activity") or "study").lower() not in STUDY_ACTIVITIES:
            continue
        blocked_types.update(rule.get("work_types") or [])
    if not blocked_types:
        return updated
    blocked = work_type in blocked_types
    updated["study_allowed"] = not blocked
    if blocked:
        activities = set(updated.get("blocked_activities") or [])
        activities.add("study")
        updated["blocked_activities"] = sorted(activities)
    return updated


def _parse_day(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def work_type_on(cycle: Optional[dict], day: date) -> Optional[str]:
    """Work type for one date from a Master Settings or relational cycle."""
    if not cycle_is_present(cycle):
        return None
    prepared = engine_cycle(cycle)
    anchor = _parse_day(prepared.get("anchor_date"))
    if anchor is None or not prepared.get("pattern"):
        return None
    from app.engines.calendar_engine import CalendarEngine

    engine = CalendarEngine("rule-check")
    cycle_day = engine.calculate_cycle_day(
        day,
        anchor,
        int(prepared.get("anchor_cycle_day") or 1),
        int(prepared.get("cycle_length") or 1),
    )
    return engine.get_work_type_for_cycle_day(cycle_day, prepared["pattern"]).value


def night_shift_dates(cycle: Optional[dict], payload: dict) -> list:
    """Dates in a commitment span that fall on a night shift. Empty when undated."""
    schedule = payload.get("schedule") if isinstance(payload.get("schedule"), dict) else {}
    start = _parse_day(payload.get("start_date") or schedule.get("start_date"))
    end = _parse_day(payload.get("end_date") or schedule.get("end_date")) or start
    if start is None or end is None or end < start:
        return []
    if (end - start).days > MAX_RULE_SPAN_DAYS:
        end = start + timedelta(days=MAX_RULE_SPAN_DAYS)
    nights = []
    current = start
    while current <= end:
        if work_type_on(cycle, current) == "work_night":
            nights.append(current.isoformat())
        current += timedelta(days=1)
    return nights


def commitment_is_study(payload: dict) -> bool:
    kind = str(payload.get("type") or "").lower()
    return kind in STUDY_ACTIVITIES


async def _active_constraint_rows(db, user_id: str) -> list:
    getter = getattr(db, "get_active_constraints", None)
    if getter is None:
        return []
    try:
        rows = await getter(user_id)
    except Exception as exc:
        logger.warning(f"Could not read relational constraints for {user_id}: {exc}")
        return []
    return rows if isinstance(rows, list) else []


async def effective_constraints(db, user_id: str, document_constraints: Optional[list]) -> list:
    relational = await _active_constraint_rows(db, user_id)
    return merge_constraints(document_constraints, relational)


async def projection_for_user(db, user_id: str) -> dict:
    """Read-through projection. Does not write Master Settings or delete rows."""
    service = MasterSettingsService(db)
    current = await service.get(user_id)
    settings = current.get("settings") or {}
    table_cycle = None
    table_leave = []
    if hasattr(db, "get_active_cycle"):
        try:
            table_cycle = await db.get_active_cycle(user_id)
        except Exception as exc:
            logger.warning(f"Could not read cycles for {user_id}: {exc}")
    if hasattr(db, "get_leave_blocks"):
        try:
            table_leave = await db.get_leave_blocks(user_id) or []
        except Exception as exc:
            logger.warning(f"Could not read leave for {user_id}: {exc}")
            table_leave = []
    constraints = await effective_constraints(db, user_id, settings.get("constraints"))
    chosen = projection_inputs(settings, table_cycle, table_leave, [])
    chosen["constraints"] = constraints
    return chosen


async def remember_cycle(db, user_id: str, cycle_row: dict, service=None, *, replace: bool = False) -> bool:
    """Copy a cycle row into Master Settings.

    replace=True is an explicit choice of the active rotation (create, or
    PATCH is_active=true): the settings cycle is always overwritten. Otherwise
    may_replace_cycle decides. An empty document is filled in, and a different
    cycle id is left alone so a stale row cannot replace it. Calendar generate
    does not call this.
    """
    service = service or MasterSettingsService(db)
    incoming = cycle_from_row(cycle_row)
    if not replace:
        try:
            current = await service.get(user_id)
            existing = (current.get("settings") or {}).get("cycle") if isinstance(current, dict) else None
        except Exception as exc:
            logger.warning(f"Could not read the cycle document for {user_id}: {exc}")
            return False
        if not may_replace_cycle(existing, incoming):
            return False
    try:
        await service.update_section(user_id, "cycle", incoming)
    except Exception as exc:
        logger.error(f"Could not store cycle in Master Settings for {user_id}: {exc}")
        return False
    return True


def pattern_length(pattern) -> Optional[int]:
    """Sum of block lengths. duration wins over days. None when there is nothing to sum."""
    if not isinstance(pattern, list) or not pattern:
        return None
    total = 0
    for block in pattern:
        if not isinstance(block, dict):
            continue
        raw = block.get("duration")
        if raw in (None, ""):
            raw = block.get("days")
        if raw in (None, ""):
            continue
        try:
            total += int(raw)
        except (TypeError, ValueError):
            continue
    return total or None


def _cycle_row_fields(cycle: dict) -> dict:
    """Settings-cycle fields that belong on the cycles table."""
    anchor_date = None
    anchor_cycle_day = None
    anchor = cycle.get("anchor")
    if isinstance(anchor, dict):
        anchor_date = anchor.get("date")
        anchor_cycle_day = anchor.get("cycle_day")
    if cycle.get("anchor_date") not in (None, ""):
        anchor_date = cycle.get("anchor_date")
        if cycle.get("anchor_cycle_day") is not None:
            anchor_cycle_day = cycle.get("anchor_cycle_day")
    if hasattr(anchor_date, "isoformat"):
        anchor_date = anchor_date.isoformat()
    elif isinstance(anchor_date, str):
        anchor_date = anchor_date[:10]
    length = cycle.get("total_days")
    if length is None:
        length = cycle.get("cycle_length")
    # An explicit length is kept even when it disagrees with the pattern.
    if length is None and cycle.get("pattern") is not None:
        length = pattern_length(cycle.get("pattern"))
    fields = {}
    if cycle.get("pattern") is not None:
        fields["pattern"] = cycle.get("pattern")
    if length is not None:
        fields["cycle_length"] = length
    if anchor_date:
        fields["anchor_date"] = anchor_date
    if anchor_cycle_day is not None:
        fields["anchor_cycle_day"] = anchor_cycle_day
    if cycle.get("name"):
        fields["name"] = cycle.get("name")
    return fields


async def sync_cycle_row(db, user_id: str, cycle: Optional[dict], *, require_id: bool = False) -> None:
    """Copy the settings cycle onto the matching active cycles row.

    Does nothing when there is no row. Never creates, deletes, or deactivates.
    require_id is for master-settings saves: sync only when the cycle id matches
    an active row. Chat, undo, redo, and PATCH /api/cycles keep the fallback
    that updates the single active row when the cycle has no id. A failed
    update is logged and does not undo the settings write.
    """
    if not isinstance(cycle, dict):
        return
    try:
        rows = await db.get_cycles(user_id) or []
        active = [row for row in rows if isinstance(row, dict) and row.get("is_active")]
        wanted = cycle.get("id")
        chosen = None
        if wanted in (None, ""):
            if require_id or len(active) != 1:
                if require_id:
                    logger.warning(f"Skipped cycles row sync for {user_id}: settings cycle has no id")
                return
            chosen = active[0]
        else:
            for row in active:
                if str(row.get("id")) == str(wanted):
                    chosen = row
                    break
            if chosen is None:
                if require_id:
                    logger.warning(
                        f"Skipped cycles row sync for {user_id}: no active row matches {wanted}"
                    )
                return
        fields = _cycle_row_fields(cycle)
        if not fields:
            return
        saved = await db.update_cycle(str(chosen.get("id")), fields, user_id)
        if not saved:
            logger.warning(f"Could not sync cycles row {chosen.get('id')} for {user_id}")
    except Exception as exc:
        logger.warning(f"Could not sync cycles row for {user_id}: {exc}")


async def mirror_onboarding_constraints(db, user_id: str, rows: Optional[list] = None) -> None:
    """Copy newly created onboarding rules into an empty or partial document. No deletes."""
    if rows is None:
        rows = await _active_constraint_rows(db, user_id)
    if not rows:
        return
    service = MasterSettingsService(db)
    try:
        current = await service.get(user_id)
    except Exception as exc:
        logger.error(f"Could not mirror constraints for {user_id}: {exc}")
        return
    settings = dict(current.get("settings") or {})
    merged = merge_constraints(settings.get("constraints"), rows)
    if merged == list(settings.get("constraints") or []):
        return
    settings["constraints"] = merged
    try:
        await service.update(user_id, settings, current.get("version"))
    except Exception as exc:
        logger.error(f"Could not store onboarding constraints for {user_id}: {exc}")
