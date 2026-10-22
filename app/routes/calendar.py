"""
Watchman Calendar Routes
Endpoints for calendar day management and generation
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from datetime import date

from app.database import Database
from app.leave_access import LEAVE_PRO_REPLY, leave_blocked
from app.middleware.auth import get_current_user, get_effective_tier
from app.engines.calendar_authority import projection_for_user
from app.engines.master_settings_service import MasterSettingsService
from app.engines.calendar_engine import (
    CALENDAR_ENGINE_VERSION,
    present_calendar_day,
    year_bounds,
)
from app.planning_horizon import clip_range, horizon_detail, horizon_end, parse_day, unlimited
from app.engines.command_executor import create_command_executor


from loguru import logger

router = APIRouter()


def _free_view(user: dict, days: list) -> dict:
    """Reads stay available. Free responses omit days after the rolling horizon."""
    tier = get_effective_tier(user)
    shown = list(days or [])
    extra = {}
    if not unlimited(tier):
        cap = horizon_end()
        shown = [
            day for day in shown
            if (parsed := parse_day(day.get("date") if isinstance(day, dict) else None)) is None
            or parsed <= cap
        ]
        extra["free_horizon_end"] = cap.isoformat()
    return {"days": shown, **extra}


class GenerateCalendarRequest(BaseModel):
    year: int = 2026
    regenerate: bool = False


class LeaveBlockRequest(BaseModel):
    name: str = "Leave"
    start_date: date
    end_date: date
    notes: Optional[str] = None
    type: Optional[str] = None


@router.get("")
async def get_calendar_days(
    start_date: date = Query(...),
    end_date: date = Query(...),
    user: dict = Depends(get_current_user)
):
    """Get calendar days for a date range"""
    logger.info(f"[CALENDAR] GET /calendar - user_id: {user['id']}, range: {start_date} to {end_date}")
    db = Database(use_admin=True)

    days = await db.get_calendar_days(
        user["id"],
        start_date.isoformat(),
        end_date.isoformat()
    )
    view = _free_view(user, days)
    logger.info(f"[CALENDAR] Returning {len(view['days'])} days for user {user['id']}")

    return {
        "success": True,
        "data": [present_calendar_day(day) for day in view["days"]],
        "count": len(view["days"]),
        **({"free_horizon_end": view["free_horizon_end"]} if "free_horizon_end" in view else {}),
    }


def _is_calendar_stale(days: list) -> bool:
    """Check if calendar data was generated with an older engine version."""
    if not days:
        return True
    # Check the first day's engine version
    first_day = days[0]
    state_json = first_day.get("state_json", {})
    stored_version = state_json.get("engine_version", 0)
    return stored_version < CALENDAR_ENGINE_VERSION


def _span_complete(days: list, start: date, end: date) -> bool:
    """True when every date from start through end is already stored."""
    expected = (end - start).days + 1
    found = set()
    for day in days or []:
        parsed = parse_day(day.get("date") if isinstance(day, dict) else None)
        if parsed is not None and start <= parsed <= end:
            found.add(parsed)
    return len(found) >= expected


def _rebuild_span(days: list, year: int, tier: str):
    """Inclusive range still worth generating. None when the year is past the cap
    or the allowed dates are already stored and current.
    """
    span = clip_range(*year_bounds(year), tier)
    if span is None:
        return None
    if _is_calendar_stale(days) or not _span_complete(days, *span):
        return span
    return None


@router.get("/year/{year}")
async def get_year(
    year: int,
    user: dict = Depends(get_current_user)
):
    """Get all calendar days for a specific year. Auto-generates if empty or stale."""
    logger.info(f"[CALENDAR] GET /calendar/year/{year} - user_id: {user['id']}")
    db = Database(use_admin=True)

    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"

    days = await db.get_calendar_days(user["id"], start_date, end_date)
    logger.debug(f"[CALENDAR] Found {len(days)} existing days for year {year}")

    # A year that starts after the free horizon is not filled in. A year that
    # overlaps it is complete once every allowed day is stored.
    tier = get_effective_tier(user)
    span = _rebuild_span(days, year, tier)
    # A failed delete or create leaves ids here. Finish that rebuild before
    # serving the year, and leave the ids if it fails again.
    try:
        executor = create_command_executor(db, user["id"])
        if await executor.finish_pending_removals():
            days = await db.get_calendar_days(user["id"], start_date, end_date)
            span = _rebuild_span(days, year, tier)
    except Exception:
        logger.exception(
            f"Pending commitment removal was not finished for user {user['id']}; "
            "the ids stay until the next calendar read"
        )
    if span is not None:
        logger.info(f"[CALENDAR] Calendar needs regeneration (stale or empty) for user {user['id']}, year {year}")

    if span is not None:
        if days:
            logger.info(f"Calendar data for user {user['id']} year {year} is stale (old engine version), regenerating...")
        else:
            logger.info(f"No calendar data for user {user['id']} year {year}, auto-generating...")
        # Same path as an approved chat change: document leave and
        # leave_blocks are merged, commitments are stamped, overrides stay.
        # Nothing is deleted first. If this load fails, return the days already
        # stored and try again on the next request. The span stops at the
        # free horizon, so a later stored day is left where it is.
        try:
            executor = create_command_executor(db, user["id"])
            await executor.rebuild_calendar(*span)
            days = await db.get_calendar_days(user["id"], start_date, end_date)
            logger.info(f"Rebuilt {len(days or [])} days for year {year}")
        except Exception:
            logger.exception(
                f"Calendar rebuild failed for user {user['id']} year {year}; "
                "returning the stored days"
            )
    
    view = _free_view(user, days)
    shown = [present_calendar_day(day) for day in view["days"]]
    body = {
        "success": True,
        "data": shown,
        "year": year,
        "count": len(shown),
    }
    if "free_horizon_end" in view:
        body["free_horizon_end"] = view["free_horizon_end"]
    return body


@router.get("/month/{year}/{month}")
async def get_month(
    year: int,
    month: int,
    user: dict = Depends(get_current_user)
):
    """Get all calendar days for a specific month"""
    db = Database(use_admin=True)
    
    # Calculate start and end dates
    start_date = f"{year}-{month:02d}-01"
    
    if month == 12:
        end_date = f"{year}-12-31"
    else:
        next_month = date(year, month + 1, 1)
        from datetime import timedelta
        last_day = next_month - timedelta(days=1)
        end_date = last_day.isoformat()
    
    days = await db.get_calendar_days(user["id"], start_date, end_date)
    view = _free_view(user, days)
    shown = [present_calendar_day(day) for day in view["days"]]
    body = {
        "success": True,
        "data": shown,
        "year": year,
        "month": month,
        "count": len(shown),
    }
    if "free_horizon_end" in view:
        body["free_horizon_end"] = view["free_horizon_end"]
    return body


@router.get("/day/{date_str}")
async def get_day(
    date_str: str,
    user: dict = Depends(get_current_user)
):
    """Get a specific calendar day with full details"""
    db = Database(use_admin=True)
    
    day = await db.get_calendar_day(user["id"], date_str)
    
    view = _free_view(user, [day] if day else [])
    if not view["days"]:
        body = {
            "success": True,
            "data": None,
            "message": "No calendar data for this date",
        }
        if "free_horizon_end" in view:
            body["free_horizon_end"] = view["free_horizon_end"]
        return body

    body = {
        "success": True,
        "data": present_calendar_day(view["days"][0]),
    }
    if "free_horizon_end" in view:
        body["free_horizon_end"] = view["free_horizon_end"]
    return body


@router.post("/generate")
async def generate_calendar(
    data: GenerateCalendarRequest,
    user: dict = Depends(get_current_user)
):
    """Generate calendar days for a year based on active cycle"""
    tier = get_effective_tier(user)
    start_bound, end_bound = year_bounds(data.year)
    clipped = clip_range(start_bound, end_bound, tier)
    if clipped is None:
        logger.warning(
            f"Free tier user {user['id']} blocked from generating {data.year} past the horizon"
        )
        raise HTTPException(status_code=403, detail=horizon_detail())

    db = Database(use_admin=True)
    logger.info(f"User {user['id']} generating calendar for year {data.year}")

    # Master Settings wins. The cycles table is used only when the document has no cycle.
    projection = await projection_for_user(db, user["id"])
    cycle = projection.get("cycle")

    if not cycle:
        raise HTTPException(
            status_code=400,
            detail="No active cycle found. Please create a cycle first."
        )
    
    # Check if calendar already exists
    existing = await db.get_calendar_days(
        user["id"],
        f"{data.year}-01-01",
        f"{data.year}-12-31"
    )
    
    if (
        existing
        and not data.regenerate
        and not _is_calendar_stale(existing)
        and _span_complete(existing, *clipped)
    ):
        return {
            "success": True,
            "message": f"Calendar for {data.year} already exists. Set regenerate=true to overwrite.",
            "count": len(existing)
        }
    
    start_date, end_date = clipped
    executor = create_command_executor(db, user["id"])
    await executor.rebuild_calendar(start_date, end_date)
    days = await db.get_calendar_days(
        user["id"], f"{data.year}-01-01", f"{data.year}-12-31"
    )
    return {
        "success": True,
        "message": f"Generated {len(days or [])} calendar days for {data.year}",
        "count": len(days or []),
    }


@router.post("/leave")
async def add_leave_block(
    data: LeaveBlockRequest,
    user: dict = Depends(get_current_user)
):
    """Add a leave block. Sick days are logging. Other leave is Pro, trial, or admin."""
    kind = str(data.type or "").strip()
    if leave_blocked(get_effective_tier(user), "add_leave", {"type": kind}):
        raise HTTPException(status_code=403, detail=LEAVE_PRO_REPLY)

    db = Database(use_admin=True)

    if data.end_date < data.start_date:
        raise HTTPException(
            status_code=400,
            detail="End date must be after start date"
        )

    effects = {
        "work": "suspended",
        "available_time": "increased",
    }
    if kind:
        effects["type"] = kind
    leave_data = {
        "user_id": user["id"],
        "name": data.name,
        "start_date": data.start_date.isoformat(),
        "end_date": data.end_date.isoformat(),
        "effects": effects,
        "notes": data.notes
    }
    
    leave_block = await db.create_leave_block(leave_data)
    
    # Update affected calendar days
    calendar_days = await db.get_calendar_days(
        user["id"],
        data.start_date.isoformat(),
        data.end_date.isoformat()
    )
    
    for day in calendar_days:
        state = day.get("state_json", {})
        state["is_leave"] = True
        state["available_hours"] = 16.0
        if "leave" not in state.get("tags", []):
            state.setdefault("tags", []).append("leave")
        
        await db.upsert_calendar_days([{
            "user_id": user["id"],
            "date": day["date"],
            "cycle_id": day.get("cycle_id"),
            "cycle_day": day.get("cycle_day"),
            "work_type": day.get("work_type"),
            "state_json": state
        }])
    
    return {
        "success": True,
        "message": "Leave block added",
        "data": leave_block,
        "affected_days": len(calendar_days)
    }


@router.get("/leave")
async def list_leave_blocks(user: dict = Depends(get_current_user)):
    """Get all leave blocks"""
    db = Database(use_admin=True)
    leave_blocks = await db.get_leave_blocks(user["id"])
    
    return {
        "success": True,
        "data": leave_blocks
    }


def _leave_day(value) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _leave_span(row: dict) -> Optional[tuple]:
    start = _leave_day(row.get("start_date") or row.get("date"))
    end = _leave_day(row.get("end_date")) or start
    if start is None or end is None:
        return None
    if end < start:
        start, end = end, start
    return start, end


@router.delete("/leave/{leave_id}")
async def delete_leave_block(
    leave_id: str,
    user: dict = Depends(get_current_user)
):
    """Delete a leave block and rebuild the days it covered."""
    db = Database(use_admin=True)
    user_id = user["id"]
    leaves = await db.get_leave_blocks(user_id)
    existing = next((item for item in leaves if item.get("id") == leave_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="Leave block not found")
    span = _leave_span(existing)

    service = MasterSettingsService(db)
    try:
        current = await service.get(user_id)
        settings = current.get("settings") if isinstance(current.get("settings"), dict) else {}
        listed = [
            item for item in (settings.get("leave_blocks") or [])
            if isinstance(item, dict) and item.get("id") == leave_id
        ]
        if listed:
            await service.remove_from_list(user_id, "leave_blocks", leave_id)
    except Exception as exc:
        logger.error(f"[CALENDAR] Settings update failed for leave {leave_id}: {exc}")
        raise HTTPException(status_code=500, detail="Leave could not be removed") from exc

    deleted = await db.delete_leave_block(leave_id, user_id=user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Leave block not found")

    if span is not None:
        executor = create_command_executor(db, user_id)
        try:
            wrote = await executor.rebuild_calendar(*span)
        except Exception as exc:
            logger.error(f"[CALENDAR] Rebuild failed for leave {leave_id}: {exc}")
            raise HTTPException(status_code=500, detail="Leave could not be removed") from exc
        if not wrote:
            logger.warning(f"[CALENDAR] No rotation to rebuild after deleting leave {leave_id}")

    return {
        "success": True,
        "message": "Leave block deleted"
    }
